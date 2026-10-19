//! Explicit, durable completion callbacks for a loaded local root session.
use std::collections::{HashMap, HashSet};
use std::ffi::OsString;
use std::fs::{File, OpenOptions};
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};

use anyhow::{Result, anyhow, bail};
use clap::{ArgGroup, Parser};
use serde_json::{Value, json};
use uuid::Uuid;

use crate::UPGRADE_EXIT;
use crate::evidence::terminal_event;
use crate::ledger::{
    MAX_SECONDS, ReconcileEvidence, RegistrationError, ResolvedJob, Store, TERMINAL,
};
use crate::local::{now, state_directory, state_home};
use crate::rpc::Client;
use crate::upgrade::{exec_package, package_command, select_package};

const MAX_PAGES: usize = 32;
const HEARTBEAT_SECONDS: u64 = 5;
const GOAL_SETTLEMENT_SECONDS: f64 = 5.0;
const SOURCES: &[&str] = &[
    "subAgent",
    "subAgentThreadSpawn",
    "subAgentOther",
    "subAgentReview",
    "subAgentCompact",
];

#[derive(Debug)]
struct HistoryLimitError;
impl std::fmt::Display for HistoryLimitError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "history exceeds {MAX_PAGES} pages")
    }
}
impl std::error::Error for HistoryLimitError {}

pub(crate) trait Calls {
    fn call(&mut self, method: &str, params: Value) -> Result<Value>;
}
impl Calls for Client {
    fn call(&mut self, method: &str, params: Value) -> Result<Value> {
        Client::call(self, method, params)
    }
}

fn text<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("missing or invalid {key}"))
}

fn flag(value: &Value, key: &str) -> bool {
    value.get(key).is_some_and(|value| match value {
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_i64() != Some(0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
        Value::Null => false,
    })
}

// Stop at the selected row, as the reference generator does; later pages are unrelated.
fn scan_pages(
    client: &mut impl Calls,
    method: &str,
    mut params: Value,
    mut visit: impl FnMut(&Value) -> bool,
) -> Result<()> {
    params["limit"] = json!(100);
    let mut seen = HashSet::new();
    for _ in 0..MAX_PAGES {
        let result = client.call(method, params.clone())?;
        let data = result
            .get("data")
            .and_then(Value::as_array)
            .ok_or_else(|| anyhow!("{method}: malformed data"))?;
        if data.iter().any(|row| !row.is_object()) {
            bail!("{method}: malformed data");
        }
        for row in data {
            if visit(row) {
                return Ok(());
            }
        }
        let Some(cursor) = result.get("nextCursor").filter(|value| !value.is_null()) else {
            return Ok(());
        };
        let cursor = cursor
            .as_str()
            .ok_or_else(|| anyhow!("{method}: invalid cursor"))?;
        if !seen.insert(cursor.to_owned()) {
            bail!("{method}: invalid cursor");
        }
        params["cursor"] = json!(cursor);
    }
    Err(HistoryLimitError.into())
}

pub(crate) fn pages(client: &mut impl Calls, method: &str, params: Value) -> Result<Vec<Value>> {
    let mut rows = Vec::new();
    scan_pages(client, method, params, |row| {
        rows.push(row.clone());
        false
    })?;
    Ok(rows)
}

fn latest(client: &mut impl Calls, thread: &str) -> Result<Value> {
    let result = client.call(
        "thread/turns/list",
        json!({"threadId":thread,"limit":1,"sortDirection":"desc","itemsView":"notLoaded"}),
    )?;
    let current = result
        .get("data")
        .and_then(Value::as_array)
        .and_then(|rows| rows.first())
        .filter(|row| row.is_object() && flag(row, "id"))
        .ok_or_else(|| anyhow!("thread has no current turn"))?;
    Ok(current.clone())
}

pub(crate) fn goal_current_turn(client: &mut Client, thread: &str) -> Result<Value> {
    latest(client, thread)
}

pub(crate) fn goal_known_turn(client: &mut Client, thread: &str, ident: &str) -> Result<bool> {
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        match turn(client, thread, ident) {
            Ok(Some(_)) => return Ok(true),
            Ok(None) => {
                if Instant::now() >= deadline {
                    return Ok(false);
                }
            }
            Err(error) => {
                let temporary = error
                    .downcast_ref::<crate::rpc::RpcError>()
                    .is_some_and(|rpc| {
                        rpc.method == "thread/turns/list"
                            && rpc.error["code"].as_i64() == Some(-32601)
                            && rpc.error["message"].as_str()
                                == Some("list_turns is not supported yet")
                    });
                if !temporary || Instant::now() >= deadline {
                    return Err(error);
                }
            }
        }
        // Turn history can briefly omit a turn after its admission succeeds.
        std::thread::sleep(
            Duration::from_millis(100).min(deadline.saturating_duration_since(Instant::now())),
        );
    }
}

fn turn(client: &mut impl Calls, thread: &str, ident: &str) -> Result<Option<Value>> {
    let mut found = None;
    scan_pages(
        client,
        "thread/turns/list",
        json!({"threadId":thread,"itemsView":"notLoaded","sortDirection":"desc"}),
        |row| {
            if row["id"] == ident {
                found = Some(row.clone());
                true
            } else {
                false
            }
        },
    )?;
    Ok(found)
}

fn find_item(
    client: &mut impl Calls,
    thread: &str,
    turn: Option<&str>,
    mut predicate: impl FnMut(&Value) -> bool,
) -> Result<Option<Value>> {
    let mut params = json!({"threadId":thread,"sortDirection":"desc"});
    if let Some(turn) = turn.filter(|turn| !turn.is_empty()) {
        params["turnId"] = json!(turn);
    }
    let mut found = None;
    scan_pages(client, "thread/items/list", params, |row| {
        if predicate(row) {
            found = Some(row.clone());
            true
        } else {
            false
        }
    })?;
    Ok(found)
}

fn command_status(item: &Value) -> &'static str {
    match item["status"].as_str() {
        Some("completed")
            if item["exitCode"].as_f64() == Some(0.0) || item["exitCode"] == false =>
        {
            "completed"
        }
        Some("completed") if exit_code(&item["exitCode"]).is_some() => "failed",
        Some("failed") => "failed",
        Some("declined") => "interrupted",
        _ => "armed",
    }
}

fn process_item(
    client: &mut impl Calls,
    parent: &str,
    origin: &str,
    process: &str,
    current: &mut Option<Vec<Value>>,
    history: &mut Option<Vec<Value>>,
) -> Result<Value> {
    if current.is_none() {
        let entries = pages(
            client,
            "thread/items/list",
            json!({"threadId":parent,"turnId":origin,"sortDirection":"desc"}),
        )?;
        if entries.iter().any(|entry| entry["turnId"] != origin) {
            bail!("thread/items/list: mismatched current turn");
        }
        *current = Some(entries);
    }
    let matches_process = |entry: &&Value| {
        entry["item"]["type"] == "commandExecution" && entry["item"]["processId"] == process
    };
    // Current-turn identity wins even when an older turn reused the process ID.
    let mut matches: Vec<_> = current
        .as_ref()
        .unwrap()
        .iter()
        .filter(matches_process)
        .collect();
    if matches.is_empty() {
        if history.is_none() {
            *history = Some(pages(
                client,
                "thread/items/list",
                json!({"threadId":parent,"sortDirection":"desc"}),
            )?);
        }
        matches = history
            .as_ref()
            .unwrap()
            .iter()
            .filter(matches_process)
            .collect();
    }
    if matches.len() != 1 {
        return Err(RegistrationError(format!(
            "process {process:?} must uniquely match a parent command item"
        ))
        .into());
    }
    Ok(matches[0].clone())
}

fn exit_code(value: &Value) -> Option<i64> {
    value.as_i64().or_else(|| value.as_bool().map(i64::from))
}

fn terminal(status: Option<&str>) -> bool {
    status.is_some_and(|status| TERMINAL.contains(&status))
}

fn root_thread(meta: &Value, parent: &str) -> bool {
    meta["id"] == parent
        && !flag(meta, "parentThreadId")
        && meta["source"].get("subAgent").is_none()
}

fn classify_turn(
    client: &mut impl Calls,
    home: &Path,
    thread: &str,
    current: Option<&Value>,
) -> Result<(Option<String>, Option<Value>)> {
    let status = current.and_then(|current| current["status"].as_str());
    if status != Some("interrupted") {
        return Ok((status.map(str::to_owned), None));
    }
    let current = current.unwrap();
    if current["completedAt"]
        .as_f64()
        .is_some_and(|value| value.is_finite() && value > 0.0)
    {
        return Ok((
            Some("interrupted".into()),
            Some(
                json!({"status":"interrupted","completedAt":current["completedAt"],"source":"api"}),
            ),
        ));
    }
    // A projected stock interruption is not positive terminal evidence.
    let evidence = (|| -> Result<Option<Value>> {
        let result = client.call(
            "thread/read",
            json!({"threadId":thread,"includeTurns":false}),
        )?;
        let meta = &result["thread"];
        if meta["id"] != thread || !flag(meta, "path") {
            return Ok(None);
        }
        terminal_event(
            home,
            Path::new(text(meta, "path")?),
            thread,
            text(current, "id")?,
        )
    })();
    let Ok(Some(event)) = evidence else {
        return Ok((None, None));
    };
    let mut evidence = json!({"status":event["status"],"source":"rollout","event":event["event"]});
    if let Some(timestamp) = event["timestamp"].as_str() {
        evidence["timestamp"] = json!(timestamp.chars().take(64).collect::<String>());
    }
    Ok((Some(text(&event, "status")?.to_owned()), Some(evidence)))
}

fn register(
    store: &mut Store,
    client: &mut Client,
    parent: &str,
    workers: &[String],
    processes: &[String],
    executions: &[String],
    reason: &str,
) -> Result<Value> {
    let result = client.call(
        "thread/read",
        json!({"threadId":parent,"includeTurns":false}),
    )?;
    let meta = &result["thread"];
    if !root_thread(meta, parent) {
        return Err(RegistrationError("park requires a root parent thread".into()).into());
    }
    let origin = latest(client, parent)?;
    if meta["status"]["type"] != "active" || origin["status"] != "inProgress" {
        return Err(RegistrationError(
            "register park during the parent active turn, then end that turn".into(),
        )
        .into());
    }
    let mut resolved = Vec::new();
    let mut pending = HashSet::new();
    let mut labels = HashMap::new();
    let children = if workers.is_empty() {
        Vec::new()
    } else {
        pages(
            client,
            "thread/list",
            json!({"parentThreadId":parent,"sourceKinds":SOURCES,"useStateDbOnly":true}),
        )?
    };
    let home = store
        .directory
        .parent()
        .ok_or_else(|| anyhow!("state directory has no home"))?
        .to_path_buf();
    let mut seen = HashSet::new();
    for selector in workers
        .iter()
        .filter(|selector| seen.insert((*selector).clone()))
    {
        let matches: Vec<_> = children
            .iter()
            .filter(|child| {
                child["parentThreadId"] == parent
                    && (child["id"] == selector.as_str()
                        || child["source"]["subAgent"]["thread_spawn"]["agent_path"]
                            == selector.as_str())
            })
            .collect();
        if matches.len() != 1 {
            return Err(RegistrationError(format!(
                "worker {selector:?} must uniquely match a direct child"
            ))
            .into());
        }
        let child = matches[0];
        let thread = text(child, "id")?;
        labels.insert(
            thread.to_owned(),
            child["source"]["subAgent"]["thread_spawn"]["agent_path"]
                .as_str()
                .map(str::to_owned),
        );
        let current = latest(client, thread)?;
        let (status, _) = classify_turn(client, &home, thread, Some(&current))?;
        let turn = text(&current, "id")?;
        let id = format!("{parent}:worker:{thread}:{turn}");
        if current["status"] == "interrupted" && status.is_none() {
            pending.insert(id.clone());
        }
        resolved.push(ResolvedJob {
            id,
            kind: "worker".into(),
            thread: thread.into(),
            turn: Some(turn.into()),
            item: None,
            process: None,
            execution: None,
            status: if terminal(status.as_deref()) {
                status.unwrap()
            } else {
                "armed".into()
            },
            exit_code: None,
        });
    }
    if !processes.is_empty() {
        let mut running = HashMap::new();
        for row in pages(
            client,
            "thread/backgroundTerminals/list",
            json!({"threadId":parent}),
        )? {
            running.insert(
                text(&row, "processId")?.to_owned(),
                text(&row, "itemId")?.to_owned(),
            );
        }
        let mut current = None;
        let mut history = None;
        let mut seen = HashSet::new();
        for process in processes
            .iter()
            .filter(|process| seen.insert((*process).clone()))
        {
            if let Some(item) = running.get(process) {
                resolved.push(ResolvedJob {
                    id: format!("{parent}:process:item:{item}"),
                    kind: "process".into(),
                    thread: parent.into(),
                    turn: None,
                    item: Some(item.clone()),
                    process: Some(process.clone()),
                    execution: None,
                    status: "armed".into(),
                    exit_code: None,
                });
                continue;
            }
            let entry = process_item(
                client,
                parent,
                text(&origin, "id")?,
                process,
                &mut current,
                &mut history,
            )?;
            let item = &entry["item"];
            let id = text(item, "id")?;
            resolved.push(ResolvedJob {
                id: format!("{parent}:process:item:{id}"),
                kind: "process".into(),
                thread: parent.into(),
                turn: Some(text(&entry, "turnId")?.into()),
                item: Some(id.into()),
                process: Some(process.clone()),
                execution: None,
                status: command_status(item).into(),
                exit_code: exit_code(&item["exitCode"]),
            });
        }
    }
    let mut seen = HashSet::new();
    for id in executions.iter().filter(|id| seen.insert((*id).clone())) {
        let id = Uuid::parse_str(id)?.to_string();
        let record = crate::executions::read(&home, parent, &id)?;
        validate_execution_binding(store, parent, &origin, &record)?;
        let (status, code, _) = execution_outcome(&record)?;
        resolved.push(ResolvedJob {
            id: format!("{parent}:execution:{id}"),
            kind: "execution".into(),
            thread: parent.into(),
            turn: None,
            item: None,
            process: None,
            execution: Some(id),
            status: status.unwrap_or("armed").into(),
            exit_code: code,
        });
    }
    let mut result = store.register_resolved(
        parent,
        text(&origin, "id")?,
        reason,
        &resolved,
        &labels,
        Some(&client.generation),
        &pending,
    )?;
    if resolved
        .iter()
        .any(|job| job.kind == "execution" && job.status == "lost")
    {
        result["mode"] = json!("report_only");
        result["next"] = json!(
            "Report the unknown execution outcome; do not retry or continue old follow-up actions automatically."
        );
    }
    for job in &resolved {
        if let Some(id) = &job.execution {
            if job.status == "lost" {
                execution_unknown_barrier(
                    store,
                    parent,
                    &crate::executions::read(&home, parent, id)?,
                )?;
            }
            crate::executions::mark_observation(
                &home,
                parent,
                id,
                "armed",
                &json!({"watch":result}),
            )?;
        }
    }
    Ok(result)
}

pub(crate) fn register_execution(
    store: &mut Store,
    client: &mut Client,
    parent: &str,
    execution: &str,
    reason: &str,
) -> Result<Value> {
    register(
        store,
        client,
        parent,
        &[],
        &[],
        &[execution.to_owned()],
        reason,
    )
}

/// Explicit recovery observes the original attempt and revokes automatic old follow-up authority.
pub(crate) fn recover_execution_watch(
    store: &mut Store,
    parent: &str,
    id: &str,
    reason: &str,
) -> Result<Value> {
    let home = store
        .directory
        .parent()
        .ok_or_else(|| anyhow!("state directory has no home"))?;
    let id = Uuid::parse_str(id)?.to_string();
    let record = crate::executions::reconcile(home, parent, &id)?;
    if record["root"] != parent || record["origin"]["root"] != parent {
        bail!("execution belongs to a different root");
    }
    require_execution_launch(&record)?;
    let (status, code, _) = execution_outcome(&record)?;
    let job = ResolvedJob {
        id: format!("{parent}:execution:{id}"),
        kind: "execution".into(),
        thread: parent.into(),
        turn: None,
        item: None,
        process: None,
        execution: Some(id),
        status: status.unwrap_or("armed").into(),
        exit_code: code,
    };
    let origin = text(&record["origin"], "turn")?;
    let mut result = store.register_resolved(
        parent,
        origin,
        reason,
        &[job.clone()],
        &HashMap::new(),
        None,
        &HashSet::new(),
    )?;
    store.execute("UPDATE jobs SET report=1 WHERE id=?", &[json!(job.id)])?;
    if job.status == "lost" {
        execution_unknown_barrier(store, parent, &record)?;
    }
    crate::executions::mark_observation(
        store
            .directory
            .parent()
            .ok_or_else(|| anyhow!("state directory has no home"))?,
        parent,
        job.execution.as_deref().unwrap(),
        "armed",
        &json!({"mode":"report_only","watch":result}),
    )?;
    result["mode"] = json!("report_only");
    result["origin"] = record["origin"].clone();
    result["next"] = json!(
        "Report recovered outcomes and uncertainty; await renewed direction before continuing old actions."
    );
    store.publish(parent)?;
    Ok(result)
}

fn execution_unknown_barrier(store: &mut Store, root: &str, record: &Value) -> Result<()> {
    let snapshot = store.goal_status(root)?;
    if record["origin"]["goal"].is_object()
        && snapshot["goal"]["id"] == record["origin"]["goal"]["id"]
        && matches!(
            snapshot["goal"]["state"].as_str(),
            Some("active" | "waiting" | "needs_input")
        )
    {
        let reason = if crate::ledger::execution_cleanup_unresolved(record) {
            "Execution cleanup is unresolved; report the known command receipt and await renewed direction without replay."
        } else {
            "Execution outcome is unknown; report evidence and await renewed direction without replay."
        };
        store.goal_recovery(
            root,
            &format!("execution-unknown:{}", text(record, "id")?),
            reason,
            now(),
        )?;
    }
    Ok(())
}

fn execution_outcome(record: &Value) -> Result<(Option<&str>, Option<i64>, bool)> {
    if matches!(
        record["state"].as_str(),
        Some("completed" | "failed" | "cancelled")
    ) && crate::ledger::execution_cleanup_unresolved(record)
    {
        return Ok((Some("lost"), record["result"]["exit_code"].as_i64(), false));
    }
    Ok(match text(record, "state")? {
        "prepared" => {
            return Err(RegistrationError("execution has not been launched".into()).into());
        }
        "completed" => (
            Some("completed"),
            record["result"]["exit_code"].as_i64(),
            false,
        ),
        "failed" => (
            Some("failed"),
            record["result"]["exit_code"].as_i64(),
            false,
        ),
        "cancelled" => (
            Some("interrupted"),
            record["result"]["exit_code"].as_i64(),
            false,
        ),
        "unknown" => (Some("lost"), None, false),
        "launching" | "running" | "cancel_requested" => (None, None, true),
        state => bail!("invalid execution state {state}"),
    })
}

fn require_execution_launch(record: &Value) -> Result<()> {
    if record["state"] == "prepared"
        || record["started_at"]
            .as_f64()
            .is_none_or(|at| !at.is_finite() || at <= 0.0)
    {
        return Err(RegistrationError("execution has not been launched".into()).into());
    }
    Ok(())
}

fn validate_execution_binding(
    store: &mut Store,
    parent: &str,
    origin: &Value,
    record: &Value,
) -> Result<()> {
    if record["root"] != parent || record["origin"]["root"] != parent {
        return Err(RegistrationError("execution belongs to a different root".into()).into());
    }
    require_execution_launch(record)?;
    let current = store.goal_status(parent)?;
    if record["origin"]["turn"].as_str().is_none_or(str::is_empty)
        || record["origin"]["turn"] != origin["id"]
        || record["origin"]["input_epoch"]
            .as_i64()
            .is_none_or(|epoch| epoch < 0)
        || record["origin"]["input_epoch"] != current["input_epoch"]
        || current["report_barrier"] != 0
        || !current["pending_input"].is_null()
        || current["open_dispatches"]
            .as_array()
            .is_none_or(|dispatches| {
                dispatches.iter().any(|dispatch| {
                    dispatch["state"] != "accepted" || dispatch["accepted_turn"] != origin["id"]
                })
            })
    {
        return Err(RegistrationError("execution watch binding is stale or fenced by newer input/report state; use codex-plus job watch --id UUID for explicit REPORT ONLY recovery".into()).into());
    }
    let execution_goal = &record["origin"]["goal"];
    if execution_goal.is_object() {
        let binding = &current["binding"];
        if current["goal"]["state"] != "active"
            || current["goal"]["id"] != execution_goal["id"]
            || current["goal"]["revision"] != execution_goal["revision"]
            || binding["root"] != parent
            || binding["goal"] != execution_goal["id"]
            || binding["revision"] != execution_goal["revision"]
            || binding["input_epoch"] != record["origin"]["input_epoch"]
            || binding["turn"] != origin["id"]
            || record["origin"]["turn"] != binding["turn"]
        {
            return Err(RegistrationError("execution goal binding is stale; it cannot bind to a different goal or admitted turn".into()).into());
        }
    } else if !current["binding"].is_null() {
        return Err(
            RegistrationError("ordinary execution cannot acquire a goal binding".into()).into(),
        );
    }
    Ok(())
}

struct Observer<'a> {
    store: &'a mut Store,
    parent: &'a str,
    client: Option<Client>,
    recovery_loaded: bool,
    run_id: String,
    last_heartbeat: Option<Instant>,
}

/// Reconcile positive admission evidence while held; never resumes, enqueues, starts or deletes work.
pub(crate) fn maintenance_reconcile(store: &mut Store, root: &str) -> Result<Value> {
    let mut observer = Observer::new(store, root);
    observer.poll_executions()?;
    let meta = observer.call("thread/read", json!({"threadId":root,"includeTurns":false}))?;
    if !root_thread(&meta["thread"], root) {
        bail!("maintenance requires an exact root");
    }
    let queue = pages(&mut observer, "thread/queue/list", json!({"threadId":root}))?;
    for callback in observer.store.callbacks_to_reconcile(root)? {
        let consumed = find_item(&mut observer, root, None, |entry| {
            entry["item"]["type"] == "userMessage"
                && entry["item"]["clientId"] == callback["client"]
        })?
        .is_some();
        let queued = queue
            .iter()
            .find(|entry| entry["clientUserMessageId"] == callback["client"])
            .cloned();
        observer.store.reconcile_evidence(
            &callback,
            &ReconcileEvidence {
                consumed,
                queued,
                ..Default::default()
            },
        )?;
    }
    for dispatch in observer.store.goal_dispatches(root)? {
        if dispatch["accepted_turn"].is_string() {
            continue;
        }
        if let Some(entry) = find_item(&mut observer, root, None, |entry| {
            entry["item"]["type"] == "userMessage"
                && entry["item"]["clientId"] == dispatch["client"]
        })? {
            observer.store.goal_accept_dispatch(
                text(&dispatch, "id")?,
                dispatch["queue"].as_str(),
                text(&entry, "turnId")?,
                now(),
            )?;
        }
    }
    observer.account_goal_turns()?;
    let unresolved = observer.store.exists("SELECT 1 FROM callbacks WHERE parent=? AND state IN ('pending','starting','in_flight','uncertain','cancel_uncertain') UNION ALL SELECT 1 FROM goal_dispatches WHERE root=? AND state IN ('submitting','pending','starting','uncertain') AND accepted_turn IS NULL LIMIT 1", &[json!(root),json!(root)])?;
    let admitted = observer.store.goal_turns(root)?.len();
    let state = meta["thread"]["status"]["type"]
        .as_str()
        .unwrap_or("unknown");
    Ok(
        json!({"root":root,"state":state,"queued":queue.len(),"unresolved_admission":unresolved,"admitted_turns":admitted,"quiet":matches!(state,"idle"|"notLoaded") && queue.is_empty() && !unresolved && admitted==0}),
    )
}

impl Calls for Observer<'_> {
    fn call(&mut self, method: &str, params: Value) -> Result<Value> {
        if self
            .last_heartbeat
            .is_none_or(|last| last.elapsed() >= Duration::from_secs(HEARTBEAT_SECONDS))
        {
            self.store.publish(self.parent)?;
            self.last_heartbeat = Some(Instant::now());
        }
        self.connect()?;
        self.client.as_mut().unwrap().call(method, params)
    }
}

impl<'a> Observer<'a> {
    fn admission(&self) -> Result<Option<crate::maintenance::AdmissionGuard>> {
        let home = self
            .store
            .directory
            .parent()
            .ok_or_else(|| anyhow!("state directory has no home"))?;
        match crate::maintenance::admission(home) {
            Ok(guard) => Ok(Some(guard)),
            Err(error) if error.downcast_ref::<crate::maintenance::Held>().is_some() => Ok(None),
            Err(error) => Err(error),
        }
    }
    fn new(store: &'a mut Store, parent: &'a str) -> Self {
        Self {
            store,
            parent,
            client: None,
            recovery_loaded: true,
            run_id: Uuid::new_v4().to_string(),
            last_heartbeat: None,
        }
    }

    fn connect(&mut self) -> Result<()> {
        if self.client.is_none() {
            let home = self
                .store
                .directory
                .parent()
                .ok_or_else(|| anyhow!("state directory has no home"))?;
            self.client = Some(Client::connect(
                &home.join("app-server-control/app-server-control.sock"),
                Duration::from_secs(5),
            )?);
        }
        Ok(())
    }

    fn native_queue(&self) -> bool {
        self.client
            .as_ref()
            .is_some_and(|client| client.initialize_result["queueIdempotency"] == true)
    }

    fn recover_generation(&mut self) -> Result<()> {
        self.connect()?;
        let generation = self.client.as_ref().unwrap().generation.clone();
        let goal_status = self.store.goal_status(self.parent)?;
        if goal_status["goal"].is_object()
            || goal_status["running_jobs"].as_i64().unwrap_or(0) > 0
            || !self.store.goal_dispatches(self.parent)?.is_empty()
        {
            self.store
                .observe_generation(self.parent, Some(&generation))?;
            return Ok(());
        }
        if self
            .store
            .observe_generation(self.parent, Some(&generation))?
        {
            self.cancel_interrupted()?;
            if self.store.active_parks(self.parent)?.is_empty() {
                return Ok(());
            }
            let result = self.call(
                "thread/read",
                json!({"threadId":self.parent,"includeTurns":false}),
            )?;
            let parent = &result["thread"];
            if !root_thread(parent, self.parent) {
                bail!("registered recovery parent is not a root thread");
            }
            self.recovery_loaded = parent["status"]["type"] != "notLoaded";
            if !self.recovery_loaded {
                let queue = pages(self, "thread/queue/list", json!({"threadId":self.parent}))?;
                self.cancel_interrupted()?;
                if !self
                    .store
                    .can_resume(self.parent, &queue, self.native_queue())?
                {
                    return Ok(());
                }
                let mut params = json!({"threadId":self.parent,"excludeTurns":true});
                let mode_instructions = self
                    .store
                    .ultracode_load_instructions(self.parent)
                    .ok()
                    .flatten();
                let _mutation_lock = if self.store.goal_provenance(self.parent, None)?.is_some()
                    || mode_instructions.is_some()
                {
                    Some(crate::goal_command::GoalLock::acquire(
                        &self.store.directory,
                        self.parent,
                    )?)
                } else {
                    None
                };
                if self.store.goal_provenance(self.parent, None)?.is_some() {
                    crate::goal_command::isolation(
                        self.store,
                        self.client.as_mut().unwrap(),
                        self.parent,
                    )?;
                    params["config"] = json!({"features.goals":false});
                }
                if let Some(instructions) = &mode_instructions {
                    params["developerInstructions"] = json!(instructions);
                }
                let Some(_admission) = self.admission()? else {
                    return Ok(());
                };
                self.call("thread/resume", params)?;
                if mode_instructions.is_some() {
                    if let Err(error) = self.store.ultracode_record_load(
                        self.parent,
                        &self.client.as_ref().unwrap().generation,
                        "cold-resume",
                        None,
                    ) {
                        eprintln!(
                            "codex-plus: Ultracode cold-load qualification unavailable: {error:#}"
                        );
                    }
                }
                self.recovery_loaded = true;
            }
        }
        Ok(())
    }

    fn cancel_interrupted(&mut self) -> Result<()> {
        let home = self
            .store
            .directory
            .parent()
            .ok_or_else(|| anyhow!("state directory has no home"))?
            .to_path_buf();
        for park in self.store.active_parks(self.parent)? {
            let origin = text(&park, "origin")?;
            let anchor = turn(self, self.parent, origin)?;
            let (status, mut evidence) = classify_turn(self, &home, self.parent, anchor.as_ref())?;
            if flag(&park, "goal_bound")
                && (status.as_deref() == Some("interrupted")
                    || anchor
                        .as_ref()
                        .is_some_and(|a| a["status"] == "interrupted"))
            {
                self.store
                    .goal_origin_interrupted(self.parent, text(&park, "id")?, now())?;
                continue;
            }
            if status.as_deref() == Some("interrupted") {
                let evidence = evidence
                    .as_mut()
                    .ok_or_else(|| anyhow!("interruption lacks evidence"))?;
                evidence["observer"] = json!(self.run_id);
                evidence["generation"] =
                    json!(self.client.as_ref().map(|client| &client.generation));
                self.store
                    .cancel(self.parent, Some(origin), Some(evidence))?;
                continue;
            }
            if anchor
                .as_ref()
                .is_some_and(|anchor| anchor["status"] == "interrupted")
                && status.is_none()
            {
                self.store.origin_pending(self.parent, origin, true)?;
            } else if matches!(status.as_deref(), Some("completed" | "failed")) {
                self.store.origin_pending(self.parent, origin, false)?;
            }
            if !flag(&park, "goal_bound")
                && park["deadline"]
                    .as_f64()
                    .ok_or_else(|| anyhow!("invalid deadline"))?
                    < now()
            {
                self.store.expire_registration(text(&park, "id")?)?;
            }
        }
        Ok(())
    }

    fn poll_jobs(&mut self) -> Result<()> {
        let jobs: Vec<_> = self
            .store
            .jobs_to_observe(self.parent)?
            .into_iter()
            .filter(|job| job["kind"] != "execution")
            .collect();
        let mut running = HashSet::new();
        if jobs.iter().any(|job| job["kind"] == "process") {
            for row in pages(
                self,
                "thread/backgroundTerminals/list",
                json!({"threadId":self.parent}),
            )? {
                running.insert((
                    text(&row, "itemId")?.to_owned(),
                    text(&row, "processId")?.to_owned(),
                ));
            }
        }
        let home = self
            .store
            .directory
            .parent()
            .ok_or_else(|| anyhow!("state directory has no home"))?
            .to_path_buf();
        for job in jobs {
            let mut observed_turn = None;
            let (status, exit_code, observed_running) = if job["kind"] == "worker" {
                let thread = text(&job, "thread")?;
                let ident = text(&job, "turn")?;
                let current = turn(self, thread, ident)?;
                let (mut status, _) = classify_turn(self, &home, thread, current.as_ref())?;
                if (current
                    .as_ref()
                    .is_some_and(|current| current["status"] == "interrupted")
                    && status.is_none())
                    || (flag(&job, "interruption_pending")
                        && !terminal(status.as_deref())
                        && status.as_deref() != Some("inProgress"))
                {
                    self.store.worker_pending(text(&job, "id")?, true)?;
                    if !flag(&job, "verify_running") {
                        continue;
                    }
                    let result = self.call(
                        "thread/read",
                        json!({"threadId":thread,"includeTurns":false}),
                    )?;
                    let child = &result["thread"];
                    let runtime = if child["id"] == thread {
                        child["status"]["type"].as_str()
                    } else {
                        None
                    };
                    if runtime == Some("active") {
                        let active = latest(self, thread)?;
                        if active["id"] == ident {
                            if active["status"] != "inProgress" {
                                continue;
                            }
                            self.store.worker_pending(text(&job, "id")?, false)?;
                            self.store.observe_job(&job, None, None, None, true)?;
                            continue;
                        }
                    } else if !matches!(runtime, Some("idle" | "notLoaded")) {
                        continue;
                    }
                    status = Some("lost".into());
                }
                self.store.worker_pending(text(&job, "id")?, false)?;
                let mut observed_running = status.as_deref() == Some("inProgress");
                if observed_running && flag(&job, "verify_running") {
                    let result = self.call(
                        "thread/read",
                        json!({"threadId":thread,"includeTurns":false}),
                    )?;
                    let child = &result["thread"];
                    observed_running =
                        if child["id"] == thread && child["status"]["type"] == "active" {
                            let current = latest(self, thread)?;
                            current["id"] == ident && current["status"] == "inProgress"
                        } else {
                            false
                        };
                }
                (status, None, observed_running)
            } else if job["kind"] == "process" {
                let item = text(&job, "item")?;
                let process = text(&job, "process")?;
                if running.contains(&(item.to_owned(), process.to_owned())) {
                    self.store.observe_job(&job, None, None, None, true)?;
                    continue;
                }
                let entry =
                    find_item(self, text(&job, "thread")?, job["turn"].as_str(), |entry| {
                        entry["item"]["id"] == item && entry["item"]["processId"] == process
                    })?;
                observed_turn = entry
                    .as_ref()
                    .map(|entry| text(entry, "turnId").map(str::to_owned))
                    .transpose()?;
                let status = entry
                    .as_ref()
                    .map(|entry| command_status(&entry["item"]).to_owned());
                let exit_code = entry
                    .as_ref()
                    .and_then(|entry| exit_code(&entry["item"]["exitCode"]));
                let observed_running = entry.is_some()
                    && status.as_deref() == Some("armed")
                    && !flag(&job, "verify_running");
                (status, exit_code, observed_running)
            } else {
                bail!("unsupported watched job kind");
            };
            self.store.observe_job(
                &job,
                status.as_deref(),
                exit_code,
                observed_turn.as_deref(),
                observed_running,
            )?;
        }
        if self.recovery_loaded {
            self.store.finish_recovery(self.parent)?;
        }
        Ok(())
    }

    fn poll_executions(&mut self) -> Result<()> {
        let home = self
            .store
            .directory
            .parent()
            .ok_or_else(|| anyhow!("state directory has no home"))?
            .to_path_buf();
        for job in self
            .store
            .jobs_to_observe(self.parent)?
            .into_iter()
            .filter(|job| job["kind"] == "execution")
        {
            let record =
                crate::executions::reconcile(&home, self.parent, text(&job, "execution")?)?;
            let (status, code, running) = execution_outcome(&record)?;
            self.store.observe_job(&job, status, code, None, running)?;
            if status == Some("lost") {
                execution_unknown_barrier(self.store, self.parent, &record)?;
            }
        }
        Ok(())
    }

    fn apply_receipt(&mut self, callback: &Value, receipt: &Value) -> Result<()> {
        let state = text(receipt, "receiptState")?;
        if !matches!(state, "pending" | "consumed" | "cancelled") {
            bail!("invalid callback receipt state");
        }
        let queued = receipt
            .get("queuedSubmission")
            .filter(|queued| !queued.is_null());
        let queue = if let Some(queued) = queued {
            let id = text(queued, "id")?;
            if id.is_empty()
                || queued["clientUserMessageId"] != callback["client"]
                || (flag(callback, "queue") && queued["id"] != callback["queue"])
            {
                bail!("callback receipt identity does not match");
            }
            Some(id)
        } else {
            if state != "cancelled" {
                bail!("callback receipt lacks submission identity");
            }
            None
        };
        self.store.record_receipt(callback, state, queue)
    }

    fn consumed(&mut self, callback: &Value) -> Result<bool> {
        let consumed = find_item(self, self.parent, None, |entry| {
            entry["item"]["type"] == "userMessage"
                && entry["item"]["clientId"] == callback["client"]
        })?;
        if let Some(entry) = &consumed {
            let _lock = crate::goal_command::GoalLock::acquire(&self.store.directory, self.parent)?;
            crate::ultracode_command::input_optional(
                self.store,
                self.client.as_mut().unwrap(),
                self.parent,
                text(callback, "client")?,
                entry["turnId"].as_str(),
                "accepted",
            );
        }
        Ok(consumed.is_some())
    }

    fn reconcile(&mut self, callback: &Value, queue: &[Value]) -> Result<()> {
        let cancelling = self.store.callback_cancelling(callback)?;
        if flag(callback, "idempotent") && self.native_queue() {
            let result = self.call(
                if cancelling {
                    "thread/queue/receipt/cancel"
                } else {
                    "thread/queue/receipt"
                },
                json!({"threadId":self.parent,"idempotencyKey":callback["client"]}),
            )?;
            let receipt = result
                .get("receipt")
                .ok_or_else(|| anyhow!("missing native callback receipt"))?;
            if receipt.is_null() {
                if cancelling {
                    bail!("missing native callback receipt");
                }
                return self.store.reconcile_evidence(
                    callback,
                    &ReconcileEvidence {
                        receipt_absent: true,
                        ..Default::default()
                    },
                );
            }
            return self.apply_receipt(callback, receipt);
        }
        if flag(callback, "idempotent") {
            let consumed = self.consumed(callback)?;
            return self.store.reconcile_evidence(
                callback,
                &ReconcileEvidence {
                    consumed,
                    native_unavailable: true,
                    ..Default::default()
                },
            );
        }
        let matches: Vec<_> = queue
            .iter()
            .filter(|queued| queued["clientUserMessageId"] == callback["client"])
            .collect();
        if matches.len() > 1 {
            bail!("multiple queue items matched owned callback");
        }
        let consumed = if matches.is_empty() {
            self.consumed(callback)?
        } else {
            false
        };
        let deleted = if !matches.is_empty() && cancelling {
            self.call(
                "thread/queue/delete",
                json!({"threadId":self.parent,"queuedSubmissionId":matches[0]["id"]}),
            )?["deleted"]
                .as_bool()
        } else {
            None
        };
        self.store.reconcile_evidence(
            callback,
            &ReconcileEvidence {
                consumed,
                queued: matches.first().map(|queued| (*queued).clone()),
                deleted,
                ..Default::default()
            },
        )
    }

    fn deliver(&mut self) -> Result<()> {
        let queue = pages(self, "thread/queue/list", json!({"threadId":self.parent}))?;
        for callback in self.store.callbacks_to_reconcile(self.parent)? {
            match self.reconcile(&callback, &queue) {
                Err(error) if error.downcast_ref::<HistoryLimitError>().is_some() => {
                    self.store.reconciliation_limited(&callback, &format!("Consumed-message history exceeds {MAX_PAGES} pages; delivery remains unknown; enqueue will not be retried."))?;
                }
                result => result?,
            }
        }
        let parent = self.call(
            "thread/read",
            json!({"threadId":self.parent,"includeTurns":false}),
        )?;
        if parent["thread"]["status"]["type"] != "idle"
            || !self.store.queue_owned(self.parent, &queue)?
        {
            return Ok(());
        }
        let (ready, blocked) = self
            .store
            .next_callback(self.parent, false, self.native_queue())?;
        if blocked {
            return Ok(());
        }
        if let Some(ready) = ready {
            if !self.account_goal_turns()? {
                return Ok(());
            }
            self.cancel_interrupted()?;
            let _mutation_lock =
                crate::goal_command::GoalLock::acquire(&self.store.directory, self.parent)?;
            let Some(_admission) = self.admission()? else {
                return Ok(());
            };
            let Some(ready) = self
                .store
                .claim_callback(&ready, false, self.native_queue())?
            else {
                return Ok(());
            };
            crate::ultracode_command::reserve_optional(
                self.store,
                self.parent,
                text(&ready, "client")?,
                &self.client.as_ref().unwrap().generation,
            );
            let result = (|| -> Result<()> {
                let mut params = json!({"threadId":self.parent,"clientUserMessageId":ready["client"],"input":[{"type":"text","text":ready["message"],"textElements":[]}]});
                if self.native_queue() {
                    params["idempotencyKey"] = ready["client"].clone();
                }
                let result = self.call("thread/queue/add", params)?;
                let queued = &result["queuedSubmission"];
                let id = text(queued, "id")?;
                if id.is_empty() || queued["clientUserMessageId"] != ready["client"] {
                    bail!("enqueue acknowledgement does not match callback");
                }
                if self.native_queue() {
                    self.apply_receipt(&ready, &result)?;
                } else {
                    self.store.dispatch_result(&ready, false, Some(id), false)?;
                }
                Ok(())
            })();
            if let Err(error) = result {
                self.store.dispatch_result(&ready, false, None, false)?;
                crate::ultracode_command::input_optional(
                    self.store,
                    self.client.as_mut().unwrap(),
                    self.parent,
                    text(&ready, "client")?,
                    None,
                    "uncertain",
                );
                return Err(error);
            }
        }
        let (pending, _) = self
            .store
            .next_callback(self.parent, true, self.native_queue())?;
        let Some(pending) = pending else {
            return Ok(());
        };
        let queue = pages(self, "thread/queue/list", json!({"threadId":self.parent}))?;
        if !self.store.queue_owned(self.parent, &queue)?
            || !queue.iter().any(|queued| {
                queued["id"] == pending["queue"]
                    && queued["clientUserMessageId"] == pending["client"]
            })
        {
            return Ok(());
        }
        if self.call(
            "thread/read",
            json!({"threadId":self.parent,"includeTurns":false}),
        )?["thread"]["status"]["type"]
            != "idle"
        {
            return Ok(());
        }
        if !self.account_goal_turns()? {
            return Ok(());
        }
        self.cancel_interrupted()?;
        let _mutation_lock =
            crate::goal_command::GoalLock::acquire(&self.store.directory, self.parent)?;
        let Some(_admission) = self.admission()? else {
            return Ok(());
        };
        if self
            .store
            .claim_callback(&pending, true, self.native_queue())?
            .is_none()
        {
            return Ok(());
        }
        let result = (|| -> Result<()> {
            let started = self.call(
                "thread/queue/start",
                json!({"threadId":self.parent,"queuedSubmissionId":pending["queue"]}),
            )?;
            crate::ultracode_command::input_optional(
                self.store,
                self.client.as_mut().unwrap(),
                self.parent,
                text(&pending, "client")?,
                started["turn"]["id"].as_str(),
                "accepted",
            );
            drop(_mutation_lock);
            if flag(&pending, "idempotent") {
                self.reconcile(&pending, &[])?;
            } else {
                self.store.dispatch_result(&pending, true, None, true)?;
            }
            Ok(())
        })();
        if let Err(error) = result {
            self.store.dispatch_result(&pending, true, None, false)?;
            let _mode_lock =
                crate::goal_command::GoalLock::acquire(&self.store.directory, self.parent)?;
            crate::ultracode_command::input_optional(
                self.store,
                self.client.as_mut().unwrap(),
                self.parent,
                text(&pending, "client")?,
                None,
                "uncertain",
            );
            return Err(error);
        }
        Ok(())
    }

    fn goal_observing(&mut self) -> Result<bool> {
        Ok(crate::goal_command::needs_observation(
            &self.store.goal_status(self.parent)?,
        ))
    }

    fn ordinary_callback_ready(&mut self, queue: &[Value]) -> Result<bool> {
        if !self.store.queue_owned(self.parent, queue)? {
            return Ok(false);
        }
        let (ready, _) = self
            .store
            .next_callback(self.parent, false, self.native_queue())?;
        match ready {
            Some(ready) => Ok(!self.store.callback_cancelling(&ready)?),
            None => Ok(false),
        }
    }

    fn goal_reconcile(&mut self, queue: &[Value]) -> Result<()> {
        for dispatch in self.store.goal_dispatches(self.parent)? {
            if dispatch["state"] == "ready" {
                continue;
            }
            if let Some(turn) = dispatch["accepted_turn"].as_str() {
                self.store.goal_accept_dispatch(
                    text(&dispatch, "id")?,
                    dispatch["queue"].as_str(),
                    turn,
                    now(),
                )?;
                let _lock =
                    crate::goal_command::GoalLock::acquire(&self.store.directory, self.parent)?;
                crate::ultracode_command::input_optional(
                    self.store,
                    self.client.as_mut().unwrap(),
                    self.parent,
                    text(&dispatch, "client")?,
                    Some(turn),
                    "accepted",
                );
                continue;
            }
            let consumed = find_item(self, self.parent, None, |entry| {
                entry["item"]["type"] == "userMessage"
                    && entry["item"]["clientId"] == dispatch["client"]
            })?;
            if let Some(entry) = consumed {
                let turn = text(&entry, "turnId")?;
                self.store.goal_accept_dispatch(
                    text(&dispatch, "id")?,
                    dispatch["queue"].as_str(),
                    turn,
                    now(),
                )?;
                let _lock =
                    crate::goal_command::GoalLock::acquire(&self.store.directory, self.parent)?;
                crate::ultracode_command::input_optional(
                    self.store,
                    self.client.as_mut().unwrap(),
                    self.parent,
                    text(&dispatch, "client")?,
                    Some(turn),
                    "accepted",
                );
                continue;
            }
            if dispatch["state"] == "starting" {
                self.store.goal_dispatch_result(text(&dispatch,"id")?,"uncertain",dispatch["queue"].as_str(),None,Some("Observer recovered a queue/start submission without positive admission evidence; no blind start retry."),now())?;
                continue;
            }
            let matches: Vec<_> = queue
                .iter()
                .filter(|entry| entry["clientUserMessageId"] == dispatch["client"])
                .collect();
            if matches.len() > 1 {
                bail!("multiple queue entries match one custom goal dispatch");
            }
            if let Some(entry) = matches.first() {
                let snapshot = self.store.goal_status(self.parent)?;
                let current = snapshot["goal"]["id"] == dispatch["goal"]
                    && snapshot["goal"]["revision"] == dispatch["revision"]
                    && snapshot["input_epoch"] == dispatch["input_epoch"]
                    && (matches!(
                        snapshot["goal"]["state"].as_str(),
                        Some("active" | "waiting")
                    ) || dispatch["kind"] == "report"
                        && snapshot["report_barrier"] == 1
                        && snapshot["report_delivered"] == 0);
                if !current {
                    let deleted = self.call(
                        "thread/queue/delete",
                        json!({"threadId":self.parent,"queuedSubmissionId":entry["id"]}),
                    )?;
                    if deleted["deleted"] == true {
                        self.store.goal_dispatch_result(
                            text(&dispatch, "id")?,
                            "done",
                            entry["id"].as_str(),
                            None,
                            None,
                            now(),
                        )?;
                    }
                } else if dispatch["state"] == "submitting"
                    || dispatch["state"] == "uncertain" && dispatch["queue"].is_null()
                {
                    self.store.goal_dispatch_result(
                        text(&dispatch, "id")?,
                        "pending",
                        entry["id"].as_str(),
                        None,
                        None,
                        now(),
                    )?;
                }
            } else if dispatch["state"] == "submitting" || dispatch["state"] == "pending" {
                if dispatch["state"] == "pending" && self.settle_goal_admission(&dispatch)? {
                    continue;
                }
                self.store.goal_dispatch_result(
                    text(&dispatch, "id")?,
                    "uncertain",
                    None,
                    None,
                    Some("Queue admission outcome unknown; no blind resend."),
                    now(),
                )?;
            }
        }
        Ok(())
    }

    fn settle_goal_admission(&mut self, dispatch: &Value) -> Result<bool> {
        let Some(started) = dispatch["settlement_started"]
            .as_f64()
            .filter(|value| value.is_finite())
        else {
            return Ok(false);
        };
        if !dispatch["queue"].as_str().is_some_and(|id| !id.is_empty()) {
            return Ok(false);
        }
        let elapsed = now() - started;
        if !(0.0..GOAL_SETTLEMENT_SECONDS).contains(&elapsed) {
            return Ok(false);
        }
        let deadline = Instant::now() + Duration::from_secs_f64(GOAL_SETTLEMENT_SECONDS - elapsed);
        // Stock can consume an acknowledged queue entry before its user item is visible.
        while let Some(remaining) = deadline.checked_duration_since(Instant::now()) {
            std::thread::sleep(remaining.min(Duration::from_millis(50)));
            let consumed = find_item(self, self.parent, None, |entry| {
                entry["item"]["type"] == "userMessage"
                    && entry["item"]["clientId"] == dispatch["client"]
            });
            let consumed = match consumed {
                Ok(consumed) => consumed,
                Err(error) => {
                    self.store.goal_dispatch_result(
                        text(dispatch, "id")?,
                        "uncertain",
                        None,
                        None,
                        Some("Acknowledged handoff reconciliation failed; no blind retry."),
                        now(),
                    )?;
                    return Err(error);
                }
            };
            if let Some(entry) = consumed {
                self.store.goal_accept_dispatch(
                    text(dispatch, "id")?,
                    dispatch["queue"].as_str(),
                    text(&entry, "turnId")?,
                    now(),
                )?;
                let _lock =
                    crate::goal_command::GoalLock::acquire(&self.store.directory, self.parent)?;
                crate::ultracode_command::input_optional(
                    self.store,
                    self.client.as_mut().unwrap(),
                    self.parent,
                    text(dispatch, "client")?,
                    entry["turnId"].as_str(),
                    "accepted",
                );
                return Ok(true);
            }
        }
        Ok(false)
    }

    fn account_goal_turns(&mut self) -> Result<bool> {
        self.store.goal_reconcile_accepted(self.parent, now())?;
        // Finalize only explicitly admitted exact turns. Unrelated turns cannot consume checkpoints.
        let home = self
            .store
            .directory
            .parent()
            .ok_or_else(|| anyhow!("state home unavailable"))?
            .to_path_buf();
        let mut admitted_turns = self.store.goal_turns(self.parent)?;
        for dispatch in self.store.goal_dispatches(self.parent)? {
            if let Some(turn) = dispatch["accepted_turn"].as_str() {
                admitted_turns.push(json!({"turn":turn}));
            }
        }
        for admitted in admitted_turns {
            let ident = text(&admitted, "turn")?;
            let current = turn(self, self.parent, ident)?;
            let (status, _) = classify_turn(self, &home, self.parent, current.as_ref())?;
            if current
                .as_ref()
                .is_some_and(|turn| turn["status"] == "interrupted")
                && status.is_none()
            {
                self.store.goal_turn_uncertain(self.parent,ident,"Admitted goal turn has an interruption without positive terminal evidence; outcome remains unknown.",now())?;
            }
            if matches!(
                status.as_deref(),
                Some("completed" | "failed" | "interrupted")
            ) {
                if status.as_deref() == Some("completed")
                    && self.call(
                        "thread/read",
                        json!({"threadId":self.parent,"includeTurns":false}),
                    )?["thread"]["status"]["type"]
                        != "idle"
                {
                    continue;
                }
                self.store.goal_finalize(
                    self.parent,
                    ident,
                    status.as_deref() == Some("completed"),
                    now(),
                )?;
            }
        }
        let snapshot = self.store.goal_status(self.parent)?;
        Ok(!self.store.goal_turns(self.parent)?.iter().any(|turn| {
            turn["goal"] == snapshot["goal"]["id"]
                && turn["revision"] == snapshot["goal"]["revision"]
                && turn["input_epoch"] == snapshot["input_epoch"]
        }))
    }

    fn goal_step(&mut self) -> Result<()> {
        let snapshot = self.store.goal_status(self.parent)?;
        if !snapshot["goal"].is_object() && self.store.goal_dispatches(self.parent)?.is_empty() {
            return Ok(());
        }
        self.account_goal_turns()?;
        let queue = pages(self, "thread/queue/list", json!({"threadId":self.parent}))?;
        self.goal_reconcile(&queue)?;
        self.store.goal_reconcile_reports(self.parent, now())?;
        let snapshot = self.store.goal_status(self.parent)?;
        let report = snapshot["report_barrier"] == 1
            && snapshot["report_delivered"] == 0
            && matches!(
                snapshot["goal"]["state"].as_str(),
                Some("active" | "waiting" | "needs_input")
            );
        if !report
            && !matches!(
                snapshot["goal"]["state"].as_str(),
                Some("active" | "waiting")
            )
        {
            return Ok(());
        }
        self.connect()?;
        let isolation = {
            let _mutation_lock =
                crate::goal_command::GoalLock::acquire(&self.store.directory, self.parent)?;
            crate::goal_command::isolation(self.store, self.client.as_mut().unwrap(), self.parent)
        };
        if let Err(error) = isolation {
            self.store.goal_set_error(
                self.parent,
                &format!("Native goal isolation unavailable: {error:#}"),
                now(),
            )?;
            return Ok(());
        }
        let meta = self.call(
            "thread/read",
            json!({"threadId":self.parent,"includeTurns":false}),
        )?;
        if !root_thread(&meta["thread"], self.parent) {
            bail!("custom goal parent is not a root thread");
        }
        if meta["thread"]["status"]["type"] == "notLoaded" {
            // Reconcile first: resume itself may admit existing queue work.
            let owned = self.store.goal_dispatches(self.parent)?;
            if !snapshot["pending_input"].is_null()
                || !queue.iter().all(|entry| {
                    owned.iter().any(|dispatch| {
                        dispatch["state"] == "pending"
                            && dispatch["queue"] == entry["id"]
                            && dispatch["client"] == entry["clientUserMessageId"]
                            && dispatch["goal"] == snapshot["goal"]["id"]
                            && dispatch["revision"] == snapshot["goal"]["revision"]
                            && dispatch["input_epoch"] == snapshot["input_epoch"]
                    })
                })
            {
                return Ok(());
            }
            let _mutation_lock =
                crate::goal_command::GoalLock::acquire(&self.store.directory, self.parent)?;
            let current = self.store.goal_status(self.parent)?;
            if current["goal"]["revision"] != snapshot["goal"]["revision"]
                || current["input_epoch"] != snapshot["input_epoch"]
                || !current["pending_input"].is_null()
            {
                return Ok(());
            }
            let Some(_admission) = self.admission()? else {
                return Ok(());
            };
            let mode_instructions = self
                .store
                .ultracode_load_instructions(self.parent)
                .ok()
                .flatten();
            let mut params = json!({"threadId":self.parent,"excludeTurns":true,"config":{"features.goals":false}});
            if let Some(instructions) = &mode_instructions {
                params["developerInstructions"] = json!(instructions);
            }
            self.call("thread/resume", params)?;
            if mode_instructions.is_some() {
                if let Err(error) = self.store.ultracode_record_load(
                    self.parent,
                    &self.client.as_ref().unwrap().generation,
                    "cold-resume",
                    None,
                ) {
                    eprintln!(
                        "codex-plus: Ultracode cold-load qualification unavailable: {error:#}"
                    );
                }
            }
            return Ok(());
        }
        if meta["thread"]["status"]["type"] != "idle" {
            return Ok(());
        }
        // Ordinary park callbacks and user queue entries keep priority.
        let owned = self.store.goal_dispatches(self.parent)?;
        if !queue.iter().all(|q| {
            owned
                .iter()
                .any(|d| q["clientUserMessageId"] == d["client"])
        }) {
            return Ok(());
        }
        if self.ordinary_callback_ready(&queue)? {
            return Ok(());
        }
        self.store.goal_wait_ready(self.parent, now())?;
        let mut snapshot = self.store.goal_status(self.parent)?;
        let completion = self.store.goal_completion_results(self.parent)?;
        let completed: Vec<Value> = completion["results"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|job| job["id"].clone())
            .collect();
        snapshot["completion_jobs"] = json!(completed);
        let kind = if !completed.is_empty() {
            "completion"
        } else if snapshot["goal"]["review_due"]
            .as_f64()
            .is_some_and(|due| due <= now())
        {
            "review"
        } else {
            "continue"
        };
        let message = if kind == "completion" {
            format!(
                "LIVE GOAL COMPLETION: Review these completed registered jobs while preserving observation of remaining jobs, then continue authorized useful work. Results: {}. {}",
                completion,
                crate::goal_command::instructions(&snapshot)
            )
        } else {
            format!(
                "LIVE GOAL CONTINUATION: Review the current custom goal and checkpoint, then continue authorized useful work. {}",
                crate::goal_command::instructions(&snapshot)
            )
        };
        if report {
            let message = format!(
                "REPORT ONLY: The coordinator recovered after an unwatched gap. Report recovered results and uncertainty, then wait for renewed user direction. Do not resume old follow-up actions or clear the report barrier. Stock already-admitted interrupted work may independently replay. Goal: {}. Recovered results (bounded; total includes any remaining results): {}",
                snapshot["goal"],
                self.store
                    .goal_report_results(self.parent, text(&snapshot, "report_id")?)?
            );
            self.store
                .goal_report_intent(self.parent, &snapshot, &message, now())?;
        } else {
            self.store
                .goal_intent(self.parent, &snapshot, kind, &message, now())?;
        }
        let dispatches = self.store.goal_dispatches(self.parent)?;
        let ready = dispatches
            .iter()
            .find(|entry| entry["state"] == "ready")
            .cloned();
        if let Some(ready) = ready {
            let _mutation_lock =
                crate::goal_command::GoalLock::acquire(&self.store.directory, self.parent)?;
            let Some(_admission) = self.admission()? else {
                return Ok(());
            };
            let Some(claimed) = self.store.goal_claim_dispatch(text(&ready, "id")?)? else {
                return Ok(());
            };
            crate::ultracode_command::reserve_optional(
                self.store,
                self.parent,
                text(&claimed, "client")?,
                &self.client.as_ref().unwrap().generation,
            );
            let result=self.call("thread/queue/add",json!({"threadId":self.parent,"clientUserMessageId":claimed["client"],"input":[{"type":"text","text":claimed["message"],"textElements":[]}]}));
            match result {
                Ok(result)
                    if result["queuedSubmission"]["clientUserMessageId"] == claimed["client"]
                        && result["queuedSubmission"]["id"].is_string() =>
                {
                    self.store.goal_dispatch_result(
                        text(&claimed, "id")?,
                        "pending",
                        result["queuedSubmission"]["id"].as_str(),
                        None,
                        None,
                        now(),
                    )?;
                }
                result => {
                    self.store.goal_dispatch_result(text(&claimed,"id")?,"uncertain",None,None,Some("Queue/add may have admitted work; reconcile before further continuation."),now())?;
                    crate::ultracode_command::input_optional(
                        self.store,
                        self.client.as_mut().unwrap(),
                        self.parent,
                        text(&claimed, "client")?,
                        None,
                        "uncertain",
                    );
                    if let Err(error) = result {
                        return Err(error);
                    } else {
                        bail!("invalid custom goal enqueue acknowledgement");
                    }
                }
            }
        }
        let queue = pages(self, "thread/queue/list", json!({"threadId":self.parent}))?;
        self.goal_reconcile(&queue)?;
        let dispatches = self.store.goal_dispatches(self.parent)?;
        let Some(pending) = dispatches
            .iter()
            .find(|entry| entry["state"] == "pending")
            .cloned()
        else {
            return Ok(());
        };
        let _mutation_lock =
            crate::goal_command::GoalLock::acquire(&self.store.directory, self.parent)?;
        let snapshot = self.store.goal_status(self.parent)?;
        if snapshot["goal"]["id"] != pending["goal"]
            || snapshot["goal"]["revision"] != pending["revision"]
            || snapshot["input_epoch"] != pending["input_epoch"]
            || !snapshot["pending_input"].is_null()
        {
            return Ok(());
        }
        if queue.len() != 1
            || queue[0]["id"] != pending["queue"]
            || queue[0]["clientUserMessageId"] != pending["client"]
        {
            return Ok(());
        }
        if self.call(
            "thread/read",
            json!({"threadId":self.parent,"includeTurns":false}),
        )?["thread"]["status"]["type"]
            != "idle"
        {
            return Ok(());
        }
        // Persist uncertainty before start; a lost start acknowledgement is never retried blindly.
        let Some(_admission) = self.admission()? else {
            return Ok(());
        };
        let started = self.store.goal_dispatch_result(
            text(&pending, "id")?,
            "starting",
            pending["queue"].as_str(),
            None,
            None,
            now(),
        )?;
        match self.call(
            "thread/queue/start",
            json!({"threadId":self.parent,"queuedSubmissionId":pending["queue"]}),
        ) {
            Ok(result) => {
                let turn = text(&result["turn"], "id")?;
                self.store.goal_accept_dispatch(
                    text(&pending, "id")?,
                    pending["queue"].as_str(),
                    turn,
                    now(),
                )?;
                crate::ultracode_command::input_optional(
                    self.store,
                    self.client.as_mut().unwrap(),
                    self.parent,
                    text(&pending, "client")?,
                    Some(turn),
                    "accepted",
                );
            }
            Err(error) => {
                drop(_mutation_lock);
                if self.settle_goal_admission(&started)? {
                    return Ok(());
                }
                self.store.goal_dispatch_result(
                    text(&pending, "id")?,
                    "uncertain",
                    pending["queue"].as_str(),
                    None,
                    Some("Queue/start outcome unknown; no blind retry."),
                    now(),
                )?;
                let _mode_lock =
                    crate::goal_command::GoalLock::acquire(&self.store.directory, self.parent)?;
                crate::ultracode_command::input_optional(
                    self.store,
                    self.client.as_mut().unwrap(),
                    self.parent,
                    text(&pending, "client")?,
                    None,
                    "uncertain",
                );
                return Err(error);
            }
        }
        Ok(())
    }

    fn step(&mut self) -> Result<bool> {
        self.store
            .claim_observation(self.parent, &self.run_id, false)?;
        let result = (|| -> Result<bool> {
            if !self.store.has_work(self.parent)? && !self.goal_observing()? {
                return Ok(false);
            }
            self.poll_executions()?;
            self.store.batch(self.parent)?;
            self.recover_generation()?;
            self.cancel_interrupted()?;
            self.poll_jobs()?;
            self.store.batch(self.parent)?;
            self.goal_step()?;
            self.deliver()?;
            Ok(self.store.finish_cycle(self.parent)? || self.goal_observing()?)
        })();
        self.store.publish(self.parent)?;
        result
    }

    fn run(&mut self, package_root: &Path, failed_package: Option<&Path>) -> Result<i32> {
        if check_upgrade(self.store, self.parent, package_root, failed_package)?.is_some() {
            return Ok(UPGRADE_EXIT);
        }
        self.store
            .claim_observation(self.parent, &self.run_id, true)?;
        self.store.publish(self.parent)?;
        let deadline = Instant::now() + Duration::from_secs(MAX_SECONDS as u64);
        let result = (|| -> Result<i32> {
            let mut backoff = 1;
            while Instant::now() < deadline
                || self.goal_observing()?
                || self.store.has_execution_watches(self.parent)?
            {
                let active = match self.step() {
                    Ok(active) => {
                        self.store.monitoring_succeeded(self.parent)?;
                        backoff = 1;
                        active
                    }
                    Err(error) => {
                        if error.downcast_ref::<sqlx::Error>().is_some() {
                            return Err(error);
                        }
                        self.client = None;
                        self.store
                            .monitoring_failed(self.parent, error_type(&error))?;
                        if backoff >= 4 {
                            self.store.goal_set_error(
                                self.parent,
                                &format!(
                                    "Goal monitoring unavailable after local retries: {error:#}"
                                ),
                                now(),
                            )?;
                        }
                        self.store.publish(self.parent)?;
                        backoff = 30.min(backoff * 2);
                        true
                    }
                };
                if check_upgrade(self.store, self.parent, package_root, failed_package)?.is_some() {
                    return Ok(UPGRADE_EXIT);
                }
                self.store.publish(self.parent)?;
                if !active {
                    return Ok(0);
                }
                let mut remaining = backoff;
                while remaining > 0 {
                    let delay = HEARTBEAT_SECONDS.min(remaining);
                    std::thread::sleep(Duration::from_secs(delay));
                    remaining -= delay;
                    self.store.publish(self.parent)?;
                }
            }
            self.store.expire_observation(self.parent)?;
            Ok(0)
        })();
        self.client = None;
        self.store.publish(self.parent)?;
        result
    }
}

fn error_type(error: &anyhow::Error) -> &'static str {
    if error.downcast_ref::<std::io::Error>().is_some() {
        "OSError"
    } else if error.downcast_ref::<crate::rpc::RpcError>().is_some() {
        "RpcError"
    } else if error.downcast_ref::<RegistrationError>().is_some() {
        "RegistrationError"
    } else {
        "ValueError"
    }
}

struct RoleLock(File);
impl RoleLock {
    fn open(home: &Path, parent: &str, action: &str) -> Result<File> {
        let suffix = match action {
            "supervise" => "supervisor.lock",
            "launch" => "launch.lock",
            _ => "lock",
        };
        Ok(OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(state_directory(home)?.join(format!("{parent}.{suffix}")))?)
    }
    fn try_acquire(home: &Path, parent: &str, action: &str) -> Result<Option<Self>> {
        let file = Self::open(home, parent, action)?;
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
            return Ok(Some(Self(file)));
        }
        let error = std::io::Error::last_os_error();
        if error.kind() == std::io::ErrorKind::WouldBlock {
            return Ok(None);
        }
        Err(error.into())
    }
    fn acquire(home: &Path, parent: &str, action: &str) -> Result<Self> {
        let file = Self::open(home, parent, action)?;
        loop {
            if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) } == 0 {
                return Ok(Self(file));
            }
            let error = std::io::Error::last_os_error();
            if error.kind() != std::io::ErrorKind::Interrupted {
                return Err(error.into());
            }
        }
    }
}
impl Drop for RoleLock {
    fn drop(&mut self) {
        unsafe {
            libc::flock(self.0.as_raw_fd(), libc::LOCK_UN);
        }
    }
}

fn check_upgrade(
    store: &mut Store,
    parent: &str,
    root: &Path,
    failed: Option<&Path>,
) -> Result<Option<PathBuf>> {
    let (candidate, invalid) = selection(root, failed);
    store.upgrade_warning(parent, invalid)?;
    Ok(candidate)
}

fn selection(root: &Path, failed: Option<&Path>) -> (Option<PathBuf>, bool) {
    match select_package(root) {
        Ok(Some(candidate)) if Some(candidate.as_path()) == failed => (None, true),
        Ok(candidate) => (candidate, false),
        Err(_) => (None, true),
    }
}

fn supervise(
    store: &mut Store,
    parent: &str,
    home: &Path,
    package_root: &Path,
    failed_package: Option<&Path>,
) -> Result<i32> {
    let mut attempt: u32 = std::env::var("CODEX_PLUS_SUPERVISOR_FAILURES")
        .unwrap_or_else(|_| "0".into())
        .parse()?;
    store.supervisor_failures = attempt;
    if check_upgrade(store, parent, package_root, failed_package)?.is_some() {
        return Ok(UPGRADE_EXIT);
    }
    while attempt < 6 {
        let mut command = package_command(package_root, home, "observe", parent, None);
        command
            .env("CODEX_PLUS_SUPERVISED", "1")
            .env(
                "CODEX_PLUS_FAILED_PACKAGE",
                failed_package.unwrap_or_else(|| Path::new("")),
            )
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        let result = crate::local::spawn_exact(&mut command).and_then(|mut child| child.wait());
        if let Ok(result) = result {
            if result.success() {
                return Ok(0);
            }
            if result.code() == Some(UPGRADE_EXIT) {
                if check_upgrade(store, parent, package_root, failed_package)?.is_some() {
                    store.supervisor_failures = attempt;
                    return Ok(UPGRADE_EXIT);
                }
                store.publish(parent)?;
                continue;
            }
        }
        if attempt < 5 {
            std::thread::sleep(Duration::from_secs(30.min(1 << attempt)));
        }
        attempt += 1;
    }
    store.supervisor_failed(parent)?;
    store.goal_set_error(
        parent,
        "Supervisor restart limit reached; autonomous goal scheduling is unsafe.",
        now(),
    )?;
    Ok(0)
}

pub(crate) fn launch(parent: &str, home: &Path, package_root: &Path) -> Result<()> {
    let deadline = Instant::now() + Duration::from_secs(5);
    let _launch = loop {
        if let Some(lock) = RoleLock::try_acquire(home, parent, "launch")? {
            break lock;
        }
        if Instant::now() >= deadline {
            bail!("coordinator launch is already in progress");
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    let Some(reservation) = RoleLock::try_acquire(home, parent, "supervise")? else {
        return Ok(());
    };
    drop(reservation);
    let mut command = package_command(package_root, home, "supervise", parent, None);
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    unsafe {
        command.pre_exec(|| {
            if libc::setsid() == -1 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = crate::local::spawn_exact(&mut command)?;
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(status) = child.try_wait()? {
            if status.success() {
                return Ok(());
            }
            bail!("coordinator supervisor exited during launch: {status}");
        }
        if RoleLock::try_acquire(home, parent, "supervise")?.is_none() {
            return Ok(());
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            bail!("coordinator supervisor did not acquire its role within five seconds");
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[derive(Parser)]
#[command(name="codex-plus",about="Explicit, durable background completion callbacks for a loaded local parent.",group(ArgGroup::new("mode").args(["status","cancel"])))]
struct Arguments {
    #[arg(value_parser=["park","observe","supervise"])]
    action: String,
    #[arg(long)]
    parent: Option<String>,
    #[arg(long)]
    worker: Vec<String>,
    #[arg(long)]
    process: Vec<String>,
    #[arg(long)]
    execution: Vec<String>,
    #[arg(long)]
    reason: Option<String>,
    #[arg(long, hide = true)]
    package_root: Option<OsString>,
    #[arg(long)]
    status: bool,
    #[arg(long)]
    cancel: bool,
}

fn printable(reason: &str) -> bool {
    reason.chars().all(crate::python_text::printable)
}

fn park(
    store: &mut Store,
    args: &Arguments,
    parent: &str,
    home: &Path,
    package_root: &Path,
) -> Result<i32> {
    if args.cancel {
        store.cancel(parent, None, None)?;
        launch(parent, home, package_root)?;
        println!("{}", serde_json::to_string(&store.status(parent)?)?);
    } else if args.status {
        println!("{}", serde_json::to_string(&store.status(parent)?)?);
    } else {
        let _execution_lock = if args.execution.is_empty() {
            None
        } else {
            Some(crate::goal_command::GoalLock::acquire(
                &store.directory,
                parent,
            )?)
        };
        let mut client = Client::connect(
            &home.join("app-server-control/app-server-control.sock"),
            Duration::from_secs(5),
        )?;
        let mut result = register(
            store,
            &mut client,
            parent,
            &args.worker,
            &args.process,
            &args.execution,
            args.reason.as_deref().unwrap(),
        )?;
        drop(client);
        if matches!(
            result["state"].as_str(),
            Some("armed" | "already_registered")
        ) {
            if let Err(error) = launch(parent, home, package_root) {
                store.launch_failed(parent)?;
                return Err(error);
            }
            result["next"] = json!(
                "End the parent turn now. The local observer will wake it only after registered completion and actual Idle."
            );
        }
        println!("{}", serde_json::to_string(&result)?);
    }
    Ok(0)
}

pub fn main(argv: &[OsString], package_root: &Path) -> i32 {
    let args = match Arguments::try_parse_from(
        std::iter::once(OsString::from("codex-plus")).chain(argv.iter().cloned()),
    ) {
        Ok(args) => args,
        Err(error) => {
            let code = error.exit_code();
            let _ = error.print();
            return code;
        }
    };
    let mut own_pin = package_root.as_os_str().to_os_string();
    own_pin.push("/");
    if args
        .package_root
        .as_ref()
        .is_some_and(|pin| pin != &own_pin)
    {
        eprintln!(
            "codex-plus: --package-root must identify this package directory with a trailing slash"
        );
        return 2;
    }
    let parent = match args
        .parent
        .as_deref()
        .map(str::to_owned)
        .or_else(|| std::env::var("CODEX_THREAD_ID").ok())
        .and_then(|parent| Uuid::parse_str(&parent).ok())
    {
        Some(parent) => parent.to_string(),
        None => {
            eprintln!("codex-plus: set CODEX_THREAD_ID or pass --parent with a thread UUID");
            return 2;
        }
    };
    if args.action == "park"
        && !args.status
        && !args.cancel
        && ((args.worker.is_empty() && args.process.is_empty() && args.execution.is_empty())
            || args.reason.as_ref().is_none_or(|reason| {
                reason.is_empty() || reason.chars().count() > 240 || !printable(reason)
            }))
    {
        eprintln!(
            "codex-plus: park needs explicit --worker/--process/--execution and a printable --reason of at most 240 characters"
        );
        return 2;
    }
    let result = run_main(&args, argv, package_root, &parent);
    match result {
        Ok(code) => code,
        Err(error) if error.downcast_ref::<HistoryLimitError>().is_some() => {
            eprintln!(
                "codex-plus park: local history exceeds {MAX_PAGES} pages; selection remains unknown."
            );
            1
        }
        Err(error) if error.downcast_ref::<RegistrationError>().is_some() => {
            eprintln!(
                "codex-plus park: {}",
                error.downcast_ref::<RegistrationError>().unwrap()
            );
            1
        }
        Err(error) => {
            eprintln!(
                "codex-plus park failed ({}); inspect local daemon availability and selectors.",
                error_type(&error)
            );
            1
        }
    }
}

fn run_main(args: &Arguments, argv: &[OsString], package_root: &Path, parent: &str) -> Result<i32> {
    let home = state_home();
    let home = if home.is_absolute() {
        home
    } else {
        std::env::current_dir()?.join(home)
    };
    let mut failed_package = std::env::var_os("CODEX_PLUS_FAILED_PACKAGE")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from);
    if args.action != "park" && package_root.join("codex-plus.json").exists() {
        std::env::set_current_dir(package_root)?;
        if args.package_root.is_none() {
            return Err(exec_package(package_root, &home, &args.action, parent, Some(argv)).into());
        }
    }
    loop {
        let mut candidate;
        {
            let _lock = if args.action == "park" {
                None
            } else if args.action == "supervise" {
                let Some(lock) = RoleLock::try_acquire(&home, parent, &args.action)? else {
                    return Ok(0);
                };
                Some(lock)
            } else {
                Some(RoleLock::acquire(&home, parent, &args.action)?)
            };
            let (selected, invalid) = selection(package_root, failed_package.as_deref());
            candidate = selected;
            if candidate.is_none() {
                let mut store = Store::open(&home)?;
                store.upgrade_warning(parent, invalid)?;
                if invalid && args.action == "park" {
                    eprintln!(
                        "codex-plus: coordinator upgrade unavailable; continuing installed helper."
                    );
                }
                let result = match args.action.as_str() {
                    "observe" => Observer::new(&mut store, parent)
                        .run(package_root, failed_package.as_deref())?,
                    "supervise" => supervise(
                        &mut store,
                        parent,
                        &home,
                        package_root,
                        failed_package.as_deref(),
                    )?,
                    _ => {
                        let result = park(&mut store, args, parent, &home, package_root)?;
                        store.upgrade_warning(parent, invalid)?;
                        store.publish(parent)?;
                        result
                    }
                };
                if result != UPGRADE_EXIT {
                    return Ok(result);
                }
                if args.action == "supervise" {
                    // The process is single-threaded; preserve the remaining budget across exec.
                    unsafe {
                        std::env::set_var(
                            "CODEX_PLUS_SUPERVISOR_FAILURES",
                            store.supervisor_failures.to_string(),
                        );
                    }
                }
                if args.action == "observe"
                    && std::env::var("CODEX_PLUS_SUPERVISED").as_deref() == Ok("1")
                {
                    return Ok(UPGRADE_EXIT);
                }
            }
        }
        // SQLite, RPC, and both role locks must be closed before replacing the process.
        if candidate.is_none() {
            match select_package(package_root) {
                Ok(selected) => candidate = selected,
                Err(_) => continue,
            }
        }
        if let Some(candidate) = candidate {
            if args.action == "observe"
                && std::env::var("CODEX_PLUS_SUPERVISED").as_deref() == Ok("1")
            {
                return Ok(UPGRADE_EXIT);
            }
            let _error = exec_package(&candidate, &home, &args.action, parent, Some(argv));
            failed_package = Some(candidate);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detached_outcomes_require_launched_identity_and_preserve_unknown() {
        assert!(execution_outcome(&json!({"state":"prepared"})).is_err());
        for (state, expected, code) in [
            ("completed", "completed", Some(0)),
            ("failed", "failed", Some(7)),
            ("cancelled", "interrupted", None),
            ("unknown", "lost", None),
        ] {
            let record = json!({"state":state,"result":{"exit_code":code,"cleanup":{"status":"not_required"}}});
            assert_eq!(
                execution_outcome(&record).unwrap(),
                (Some(expected), code, false)
            );
        }
        assert_eq!(
            execution_outcome(&json!({"state":"running"})).unwrap(),
            (None, None, true)
        );
    }

    #[test]
    fn unlaunched_cancellation_cannot_be_watched_or_recovered() {
        let home = tempfile::tempdir().unwrap();
        let root = Uuid::new_v4().to_string();
        let prepared = crate::executions::prepare(home.path(),&root,&json!({"root":root,"turn":"origin","goal":null}),&json!({"schema":1,"topic":"never launched","argv":["/usr/bin/true"],"cwd":home.path()})).unwrap();
        let id = prepared["id"].as_str().unwrap();
        let record = crate::executions::cancel(home.path(), &root, id).unwrap();
        let mut store = Store::open(home.path()).unwrap();
        assert!(
            validate_execution_binding(&mut store, &root, &json!({"id":"origin"}), &record)
                .is_err()
        );
        assert!(recover_execution_watch(&mut store, &root, id, "not launched").is_err());
    }

    #[test]
    fn unresolved_cleanup_preserves_known_exit_but_blocks_live_followup_and_goal_continuation() {
        for goal_bound in [false, true] {
            let home = tempfile::tempdir().unwrap();
            let root = Uuid::new_v4().to_string();
            let mut store = Store::open(home.path()).unwrap();
            let goal = if goal_bound {
                let snapshot = store
                    .goal_start(&root, "verify cleanup", None, false, now())
                    .unwrap();
                let id = snapshot["goal"]["id"].as_str().unwrap();
                store.goal_admit(&root, id, 1, "origin", 1, now()).unwrap();
                snapshot["goal"].clone()
            } else {
                Value::Null
            };
            let prepared = crate::executions::prepare(home.path(),&root,&json!({"root":root,"turn":"origin","input_epoch":1,"goal":goal}),&json!({"schema":1,"topic":"cleanup uncertainty","argv":["/usr/bin/true"],"cwd":home.path()})).unwrap();
            let id = prepared["id"].as_str().unwrap();
            let mut record = crate::executions::cancel(home.path(), &root, id).unwrap();
            record["started_at"] = json!(now());
            record["state"] = json!("failed");
            record["result"]["state"] = json!("failed");
            record["result"]["exit_code"] = json!(0);
            record["result"]["workload_attempted"] = json!(true);
            record["result"]["cleanup"]["status"] = json!("unknown");
            let directory = home
                .path()
                .join("codex-plus/executions/v1")
                .join(&root)
                .join(id);
            crate::local::atomic_json(&directory.join("state.json"), &record).unwrap();
            crate::local::atomic_json(&directory.join("result.json"), &record["result"]).unwrap();
            assert_eq!(
                execution_outcome(&record).unwrap(),
                (Some("lost"), Some(0), false)
            );
            let job = ResolvedJob {
                id: format!("{root}:execution:{id}"),
                kind: "execution".into(),
                thread: root.clone(),
                turn: None,
                item: None,
                process: None,
                execution: Some(id.into()),
                status: "armed".into(),
                exit_code: None,
            };
            store
                .register_resolved(
                    &root,
                    "origin",
                    "report known receipt",
                    &[job],
                    &HashMap::new(),
                    None,
                    &HashSet::new(),
                )
                .unwrap();
            let mut observer = Observer::new(&mut store, &root);
            observer.poll_executions().unwrap();
            let status = observer.store.status(&root).unwrap();
            assert_eq!(status["jobs"][0]["status"], "lost");
            assert_eq!(status["jobs"][0]["exit_code"], 0);
            assert_eq!(status["jobs"][0]["report"], 1);
            if goal_bound {
                let goal = observer.store.goal_status(&root).unwrap();
                assert_eq!(goal["report_barrier"], 1);
                assert_eq!(goal["goal"]["state"], "needs_input");
            } else {
                observer.store.batch(&root).unwrap();
                let ready = observer
                    .store
                    .next_callback(&root, false, false)
                    .unwrap()
                    .0
                    .unwrap();
                let claimed = observer
                    .store
                    .claim_callback(&ready, false, false)
                    .unwrap()
                    .unwrap();
                let message = claimed["message"].as_str().unwrap();
                assert!(message.starts_with("REPORT ONLY"));
                assert!(message.contains("cleanup=UNRESOLVED command_receipt=KNOWN exit=0"));
                assert!(!message.contains("outcome=UNKNOWN"));
            }
            assert_eq!(
                crate::executions::read(home.path(), &root, id).unwrap()["result"]["exit_code"],
                0
            );
            record["resolution"] = json!({"cleanup_verified":true});
            assert_eq!(
                execution_outcome(&record).unwrap(),
                (Some("failed"), Some(0), false)
            );
        }
    }

    #[test]
    fn already_unknown_execution_blocks_its_goal_without_blocking_a_replacement() {
        let home = tempfile::tempdir().unwrap();
        let root = Uuid::new_v4().to_string();
        let mut store = Store::open(home.path()).unwrap();
        let snapshot = store
            .goal_start(&root, "unknown attempt", None, false, now())
            .unwrap();
        let id = snapshot["goal"]["id"].as_str().unwrap();
        let record = json!({"id":Uuid::new_v4().to_string(),"origin":{"goal":{"id":id}}});
        execution_unknown_barrier(&mut store, &root, &record).unwrap();
        let snapshot = store.goal_status(&root).unwrap();
        assert_eq!(snapshot["report_barrier"], 1);
        assert_eq!(snapshot["goal"]["state"], "needs_input");
        store
            .goal_replace(
                &root,
                id,
                snapshot["goal"]["revision"].as_i64().unwrap(),
                "replacement",
                None,
                false,
                now(),
            )
            .unwrap();
        execution_unknown_barrier(&mut store, &root, &record).unwrap();
        assert_eq!(store.goal_status(&root).unwrap()["report_barrier"], 0);
    }

    #[test]
    fn detached_outcome_is_collected_before_any_daemon_connection() {
        let home = tempfile::tempdir().unwrap();
        let root = Uuid::new_v4().to_string();
        let record = crate::executions::prepare(home.path(),&root,&json!({"root":root,"turn":"origin","goal":null}),&json!({"schema":1,"topic":"offline collection","argv":["/usr/bin/true"],"cwd":home.path()})).unwrap();
        let id = record["id"].as_str().unwrap();
        let mut store = Store::open(home.path()).unwrap();
        let job = ResolvedJob {
            id: format!("{root}:execution:{id}"),
            kind: "execution".into(),
            thread: root.clone(),
            turn: None,
            item: None,
            process: None,
            execution: Some(id.into()),
            status: "armed".into(),
            exit_code: None,
        };
        store
            .register_resolved(
                &root,
                "origin",
                "collect locally",
                &[job],
                &HashMap::new(),
                None,
                &HashSet::new(),
            )
            .unwrap();
        let outcome = crate::executions::cancel(home.path(), &root, id).unwrap();
        let mut observer = Observer::new(&mut store, &root);
        assert!(observer.step().is_err());
        assert_eq!(
            observer.store.status(&root).unwrap()["jobs"][0]["status"],
            "interrupted"
        );
        assert_eq!(
            observer.store.status(&root).unwrap()["callbacks"][0]["state"],
            "ready"
        );
        observer.store.cancel(&root, None, None).unwrap();
        let mut actual = crate::executions::read(home.path(), &root, id).unwrap()["result"].clone();
        let mut expected = outcome["result"].clone();
        let actual_finished = actual
            .as_object_mut()
            .unwrap()
            .remove("finished_at")
            .unwrap()
            .as_f64()
            .unwrap();
        let expected_finished = expected
            .as_object_mut()
            .unwrap()
            .remove("finished_at")
            .unwrap()
            .as_f64()
            .unwrap();
        assert!((actual_finished - expected_finished).abs() <= 1e-5);
        assert_eq!(actual, expected);
        assert_eq!(
            crate::executions::read_observation(home.path(), &root, id).unwrap()["state"],
            "cancelled"
        );
    }

    #[test]
    fn maintenance_preserves_ready_and_queued_callbacks_without_admission() {
        use std::os::unix::net::UnixListener;
        use tungstenite::Message;
        for pending in [false, true] {
            let home = tempfile::tempdir().unwrap();
            crate::maintenance::hold_enter(home.path()).unwrap();
            let parent = Uuid::new_v4().to_string();
            let mut store = Store::open(home.path()).unwrap();
            let job = ResolvedJob {
                id: "job".into(),
                kind: "process".into(),
                thread: parent.clone(),
                turn: None,
                item: Some("item".into()),
                process: Some("1".into()),
                execution: None,
                status: "armed".into(),
                exit_code: None,
            };
            store
                .register_resolved(
                    &parent,
                    "origin",
                    "preserve intent",
                    &[job],
                    &HashMap::new(),
                    Some("generation"),
                    &HashSet::new(),
                )
                .unwrap();
            store
                .observe_job(
                    &json!({"id":"job"}),
                    Some("completed"),
                    Some(0),
                    None,
                    false,
                )
                .unwrap();
            store.batch(&parent).unwrap();
            let callback = store
                .next_callback(&parent, false, false)
                .unwrap()
                .0
                .unwrap();
            let client = callback["client"].clone();
            if pending {
                let claimed = store
                    .claim_callback(&callback, false, false)
                    .unwrap()
                    .unwrap();
                store
                    .dispatch_result(&claimed, false, Some("queue"), false)
                    .unwrap();
            }
            let directory = home.path().join("app-server-control");
            std::fs::create_dir(&directory).unwrap();
            let listener = UnixListener::bind(directory.join("app-server-control.sock")).unwrap();
            let server_parent = parent.clone();
            let server = std::thread::spawn(move || {
                let (socket, _) = listener.accept().unwrap();
                socket
                    .set_read_timeout(Some(Duration::from_secs(5)))
                    .unwrap();
                let mut socket = tungstenite::accept(socket).unwrap();
                let mut mutations = Vec::new();
                while let Ok(Message::Text(request)) = socket.read() {
                    let request: Value = serde_json::from_str(&request).unwrap();
                    if request["id"].is_null() {
                        continue;
                    }
                    let method = request["method"].as_str().unwrap();
                    let result = match method {
                        "initialize" => json!({}),
                        "thread/read" => {
                            json!({"thread":{"id":server_parent,"status":{"type":"idle"}}})
                        }
                        "thread/queue/list" => {
                            json!({"data":if pending {vec![json!({"id":"queue","clientUserMessageId":client})]} else {vec![]}})
                        }
                        "thread/turns/list" => {
                            json!({"data":[{"id":"origin","status":"completed"}]})
                        }
                        "thread/items/list" => json!({"data":[]}),
                        _ => {
                            mutations.push(method.to_owned());
                            json!({})
                        }
                    };
                    socket
                        .send(Message::text(
                            json!({"id":request["id"],"result":result}).to_string(),
                        ))
                        .unwrap();
                }
                mutations
            });
            let mut observer = Observer::new(&mut store, &parent);
            observer.deliver().unwrap();
            assert_eq!(
                observer.store.status(&parent).unwrap()["callbacks"][0]["state"],
                if pending { "pending" } else { "ready" }
            );
            drop(observer);
            assert!(server.join().unwrap().is_empty());
        }
    }

    #[test]
    fn execution_cannot_acquire_a_replacement_goal_or_new_admitted_turn() {
        let home = tempfile::tempdir().unwrap();
        let mut store = Store::open(home.path()).unwrap();
        let root = Uuid::new_v4().to_string();
        let status = store
            .goal_start(&root, "original", None, false, now())
            .unwrap();
        let id = status["goal"]["id"].as_str().unwrap();
        store.goal_admit(&root, id, 1, "origin", 1, now()).unwrap();
        let record = json!({"root":root,"state":"running","started_at":1.0,"origin":{"root":root,"turn":"origin","input_epoch":1,"goal":{"id":id,"revision":1}}});
        assert!(
            validate_execution_binding(&mut store, &root, &json!({"id":"origin"}), &record).is_ok()
        );
        assert!(
            validate_execution_binding(&mut store, &root, &json!({"id":"another"}), &record)
                .is_err()
        );
        store
            .goal_replace(&root, id, 1, "replacement", None, false, now())
            .unwrap();
        assert!(
            validate_execution_binding(&mut store, &root, &json!({"id":"origin"}), &record)
                .is_err()
        );
    }

    #[test]
    fn ordinary_watch_requires_original_turn_epoch_and_unfenced_admission() {
        for change in ["none", "turn", "epoch", "report", "pending"] {
            let home = tempfile::tempdir().unwrap();
            let root = Uuid::new_v4().to_string();
            let mut store = Store::open(home.path()).unwrap();
            let mut record = json!({"root":root,"state":"running","started_at":1.0,"origin":{"root":root,"turn":"origin","input_epoch":0,"goal":null}});
            let mut current_turn = json!({"id":"origin"});
            let submission = Uuid::new_v4().to_string();
            match change {
                "turn" => current_turn["id"] = json!("later"),
                "epoch" => {
                    store
                        .goal_input_submission(&root, &submission, false, now())
                        .unwrap();
                    store
                        .goal_input_settle(&root, &submission, Some("later"), "accepted", now())
                        .unwrap();
                }
                "report" => {
                    store
                        .goal_recovery(&root, "recovered", "report only", now())
                        .unwrap();
                    record["origin"]["input_epoch"] =
                        store.goal_status(&root).unwrap()["input_epoch"].clone();
                }
                "pending" => {
                    store
                        .goal_input_submission(&root, &submission, false, now())
                        .unwrap();
                    record["origin"]["input_epoch"] =
                        store.goal_status(&root).unwrap()["input_epoch"].clone();
                }
                _ => {}
            }
            let result = validate_execution_binding(&mut store, &root, &current_turn, &record);
            if change == "none" {
                assert!(result.is_ok());
            } else {
                assert!(
                    result
                        .unwrap_err()
                        .to_string()
                        .contains("explicit REPORT ONLY recovery"),
                    "{change}"
                );
            }
        }
    }

    #[test]
    fn ordinary_watch_can_remain_unbound_beside_an_unadmitted_goal() {
        let home = tempfile::tempdir().unwrap();
        let root = Uuid::new_v4().to_string();
        let mut store = Store::open(home.path()).unwrap();
        let snapshot = store
            .goal_start(&root, "unrelated goal", None, false, now())
            .unwrap();
        let record = json!({"root":root,"state":"running","started_at":1.0,"origin":{"root":root,"turn":"origin","input_epoch":1,"goal":null}});
        assert!(
            validate_execution_binding(&mut store, &root, &json!({"id":"origin"}), &record).is_ok()
        );
        store
            .goal_admit(
                &root,
                snapshot["goal"]["id"].as_str().unwrap(),
                1,
                "origin",
                1,
                now(),
            )
            .unwrap();
        assert!(
            validate_execution_binding(&mut store, &root, &json!({"id":"origin"}), &record)
                .is_err()
        );
    }
    use std::collections::VecDeque;

    struct Fake {
        responses: VecDeque<Value>,
        calls: Vec<(String, Value)>,
    }
    impl Calls for Fake {
        fn call(&mut self, method: &str, params: Value) -> Result<Value> {
            self.calls.push((method.into(), params));
            self.responses
                .pop_front()
                .ok_or_else(|| anyhow!("unexpected call"))
        }
    }
    fn fake(responses: Vec<Value>) -> Fake {
        Fake {
            responses: responses.into(),
            calls: Vec::new(),
        }
    }

    #[test]
    fn current_turn_process_does_not_scan_long_parent_history() {
        let item = json!({"turnId":"origin","item":{"type":"commandExecution","id":"recent","processId":"42","status":"completed","exitCode":0}});
        let mut responses = vec![json!({"data":[item]})];
        responses
            .extend((0..MAX_PAGES).map(|index| json!({"data":[],"nextCursor":index.to_string()})));
        let mut client = fake(responses);
        let result =
            process_item(&mut client, "root", "origin", "42", &mut None, &mut None).unwrap();
        assert_eq!(result["item"]["id"], "recent");
        assert_eq!(client.calls.len(), 1);
        assert_eq!(client.calls[0].1["turnId"], "origin");
    }

    #[test]
    fn process_selection_caches_current_turn_and_falls_back_only_for_older_jobs() {
        let current = json!({"turnId":"origin","item":{"type":"commandExecution","id":"recent","processId":"42"}});
        let old = json!({"turnId":"older","item":{"type":"commandExecution","id":"old","processId":"43"}});
        let mut client = fake(vec![json!({"data":[current]}), json!({"data":[old]})]);
        let (mut current, mut history) = (None, None);
        for (process, expected) in [("42", "recent"), ("43", "old"), ("42", "recent")] {
            let item = process_item(
                &mut client,
                "root",
                "origin",
                process,
                &mut current,
                &mut history,
            )
            .unwrap();
            assert_eq!(item["item"]["id"], expected);
        }
        assert_eq!(client.calls.len(), 2);
        assert_eq!(client.calls[0].1["turnId"], "origin");
        assert!(client.calls[1].1.get("turnId").is_none());
    }

    #[test]
    fn process_selection_rejects_duplicate_current_matches_and_foreign_turns() {
        let item = json!({"turnId":"origin","item":{"type":"commandExecution","id":"recent","processId":"42"}});
        let mut client = fake(vec![
            json!({"data":[item.clone()],"nextCursor":"more"}),
            json!({"data":[item.clone()]}),
        ]);
        assert!(
            process_item(&mut client, "root", "origin", "42", &mut None, &mut None)
                .unwrap_err()
                .downcast_ref::<RegistrationError>()
                .is_some()
        );
        assert_eq!(client.calls.len(), 2);
        for turn in [json!("other"), Value::Null] {
            let mut foreign = item.clone();
            foreign["turnId"] = turn;
            let mut client = fake(vec![json!({"data":[foreign]})]);
            assert!(
                process_item(&mut client, "root", "origin", "42", &mut None, &mut None).is_err()
            );
            assert_eq!(client.calls.len(), 1);
        }
    }

    #[test]
    fn paginated_history_is_bounded_and_repeated_cursors_fail_closed() {
        let mut client = fake(
            (0..MAX_PAGES)
                .map(|index| json!({"data":[],"nextCursor":index.to_string()}))
                .collect(),
        );
        assert!(
            pages(&mut client, "history", json!({}))
                .unwrap_err()
                .downcast_ref::<HistoryLimitError>()
                .is_some()
        );
        assert_eq!(client.calls.len(), 32);
        let mut client = fake(vec![
            json!({"data":[],"nextCursor":"same"}),
            json!({"data":[],"nextCursor":"same"}),
        ]);
        assert!(pages(&mut client, "history", json!({})).is_err());
        let mut client = fake(vec![json!({"data":[{"id":"wanted"}],"nextCursor":"next"})]);
        assert_eq!(
            turn(&mut client, "root", "wanted").unwrap().unwrap()["id"],
            "wanted"
        );
    }

    #[test]
    fn page_rows_and_latest_turn_must_be_objects() {
        let mut client = fake(vec![json!({"data":[{},null]})]);
        assert!(pages(&mut client, "history", json!({})).is_err());
        let mut client = fake(vec![json!({"data":[]})]);
        assert!(latest(&mut client, "root").is_err());
    }

    #[test]
    fn command_completion_requires_exit_evidence() {
        assert_eq!(
            command_status(&json!({"status":"completed","exitCode":0})),
            "completed"
        );
        assert_eq!(
            command_status(&json!({"status":"completed","exitCode":2})),
            "failed"
        );
        assert_eq!(command_status(&json!({"status":"completed"})), "armed");
        assert_eq!(command_status(&json!({"status":"declined"})), "interrupted");
        assert_eq!(
            command_status(&json!({"status":"completed","exitCode":false})),
            "completed"
        );
        assert_eq!(
            command_status(&json!({"status":"completed","exitCode":true})),
            "failed"
        );
        assert_eq!(
            command_status(&json!({"status":"completed","exitCode":0.0})),
            "completed"
        );
    }

    #[test]
    fn projected_interruption_requires_positive_evidence() {
        let home = tempfile::tempdir().unwrap();
        let mut client = fake(vec![json!({"thread":{"id":"root"}})]);
        assert_eq!(
            classify_turn(
                &mut client,
                home.path(),
                "root",
                Some(&json!({"id":"turn","status":"interrupted"}))
            )
            .unwrap(),
            (None, None)
        );
        let (status, evidence) = classify_turn(
            &mut client,
            home.path(),
            "root",
            Some(&json!({"id":"turn","status":"interrupted","completedAt":1})),
        )
        .unwrap();
        assert_eq!(status.as_deref(), Some("interrupted"));
        assert_eq!(evidence.unwrap()["source"], "api");
        let (status, _) = classify_turn(
            &mut client,
            home.path(),
            "root",
            Some(&json!({"id":"turn","status":"completed"})),
        )
        .unwrap();
        assert_eq!(status.as_deref(), Some("completed"));
    }

    #[test]
    fn reason_printability_matches_unicode_categories() {
        assert!(printable("Continue 中文 🦀"));
        for reason in [
            "new\nline",
            "\u{a0}",
            "\u{200b}",
            "\u{e000}",
            "\u{2028}",
            "\u{0378}",
        ] {
            assert!(!printable(reason), "{reason:?}");
        }
    }

    #[test]
    fn only_root_parent_is_accepted() {
        assert!(root_thread(&json!({"id":"root","source":"cli"}), "root"));
        assert!(!root_thread(
            &json!({"id":"root","parentThreadId":"other"}),
            "root"
        ));
        assert!(!root_thread(
            &json!({"id":"root","source":{"subAgent":null}}),
            "root"
        ));
    }

    #[test]
    fn receipts_require_exact_owned_identity_and_positive_state() {
        let home = tempfile::tempdir().unwrap();
        let parent = Uuid::new_v4().to_string();
        let mut store = Store::open(home.path()).unwrap();
        let resolved = [ResolvedJob {
            id: "job".into(),
            kind: "process".into(),
            thread: parent.clone(),
            turn: Some("turn".into()),
            item: Some("item".into()),
            process: Some("42".into()),
            execution: None,
            status: "armed".into(),
            exit_code: None,
        }];
        store
            .register_resolved(
                &parent,
                "origin",
                "finish",
                &resolved,
                &HashMap::new(),
                Some("generation"),
                &HashSet::new(),
            )
            .unwrap();
        let job = store.jobs_to_observe(&parent).unwrap().remove(0);
        store
            .observe_job(&job, Some("completed"), Some(0), None, false)
            .unwrap();
        store.batch(&parent).unwrap();
        let ready = store
            .next_callback(&parent, false, true)
            .unwrap()
            .0
            .unwrap();
        let callback = store.claim_callback(&ready, false, true).unwrap().unwrap();
        let mut observer = Observer::new(&mut store, &parent);
        assert!(observer.apply_receipt(&callback,&json!({"receiptState":"pending","queuedSubmission":{"id":"queue","clientUserMessageId":"foreign"}})).is_err());
        assert!(
            observer
                .apply_receipt(&callback, &json!({"receiptState":"consumed"}))
                .is_err()
        );
        observer.apply_receipt(&callback,&json!({"receiptState":"pending","queuedSubmission":{"id":"queue","clientUserMessageId":callback["client"]}})).unwrap();
        let callback = observer
            .store
            .callbacks_to_reconcile(&parent)
            .unwrap()
            .remove(0);
        assert!(observer.apply_receipt(&callback,&json!({"receiptState":"consumed","queuedSubmission":{"id":"other","clientUserMessageId":callback["client"]}})).is_err());
        observer.apply_receipt(&callback,&json!({"receiptState":"consumed","queuedSubmission":{"id":"queue","clientUserMessageId":callback["client"]}})).unwrap();
        assert_eq!(
            observer.store.status(&parent).unwrap()["callbacks"][0]["state"],
            "delivered"
        );
    }

    #[test]
    fn role_locks_refuse_symlink_targets_and_close_on_drop() {
        let home = tempfile::tempdir().unwrap();
        let parent = Uuid::new_v4().to_string();
        let directory = state_directory(home.path()).unwrap();
        let lock = directory.join(format!("{parent}.lock"));
        let target = home.path().join("target");
        std::fs::write(&target, "untouched").unwrap();
        std::os::unix::fs::symlink(&target, &lock).unwrap();
        assert!(RoleLock::acquire(home.path(), &parent, "observe").is_err());
        assert_eq!(std::fs::read_to_string(target).unwrap(), "untouched");
        std::fs::remove_file(lock).unwrap();
        let held = RoleLock::acquire(home.path(), &parent, "observe").unwrap();
        let fd = held.0.as_raw_fd();
        assert_ne!(
            unsafe { libc::fcntl(fd, libc::F_GETFD) } & libc::FD_CLOEXEC,
            0
        );
        let contender = File::open(directory.join(format!("{parent}.lock"))).unwrap();
        assert_eq!(
            unsafe { libc::flock(contender.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
            -1
        );
        drop(held);
        assert_eq!(
            unsafe { libc::flock(contender.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
            0
        );
    }

    fn delayed_goal_handoff(change: &'static str) -> (Value, Value, (usize, usize, usize)) {
        use std::os::unix::net::UnixListener;
        use tungstenite::Message;
        let home = tempfile::tempdir().unwrap();
        let parent = Uuid::new_v4().to_string();
        let mut store = Store::open(home.path()).unwrap();
        let snapshot = store
            .goal_start(
                &parent,
                "settle the acknowledged submission",
                None,
                false,
                now() - 3600.0,
            )
            .unwrap();
        let dispatch = store
            .goal_intent(
                &parent,
                &snapshot,
                "continue",
                "one frozen continuation",
                now() - 3600.0,
            )
            .unwrap()
            .unwrap();
        let dispatch = store
            .goal_claim_dispatch(text(&dispatch, "id").unwrap())
            .unwrap()
            .unwrap();
        let client = dispatch["client"].clone();
        let server_home = home.path().to_path_buf();
        let server_parent = parent.clone();
        let directory = home.path().join("app-server-control");
        std::fs::create_dir(&directory).unwrap();
        let listener = UnixListener::bind(directory.join("app-server-control.sock")).unwrap();
        let server = std::thread::spawn(move || {
            let mut store = Store::open(&server_home).unwrap();
            let (socket, _) = listener.accept().unwrap();
            socket
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut socket = tungstenite::accept(socket).unwrap();
            let mut adds = 0;
            let mut reads = 0;
            let mut starts = 0;
            while let Ok(Message::Text(request)) = socket.read() {
                let request: Value = serde_json::from_str(&request).unwrap();
                if request["id"].is_null() {
                    continue;
                }
                let result = match request["method"].as_str().unwrap() {
                    "initialize" => json!({}),
                    "thread/queue/add" => {
                        adds += 1;
                        json!({"queuedSubmission":{"id":"acknowledged-queue","clientUserMessageId":client}})
                    }
                    "thread/queue/start" => {
                        starts += 1;
                        socket.send(Message::text(json!({"id":request["id"],"error":{"code":-32600,"message":"Already consumed"}}).to_string())).unwrap();
                        continue;
                    }
                    "thread/items/list" => {
                        reads += 1;
                        if reads == 1 {
                            let current = store.goal_status(&server_parent).unwrap();
                            let id = current["goal"]["id"].as_str().unwrap();
                            let revision = current["goal"]["revision"].as_i64().unwrap();
                            match change {
                                "pause" => {
                                    store
                                        .goal_mutate(
                                            &server_parent,
                                            id,
                                            revision,
                                            "pause",
                                            &json!({}),
                                            now(),
                                        )
                                        .unwrap();
                                }
                                "input" => {
                                    store
                                        .goal_input_submission(
                                            &server_parent,
                                            &Uuid::new_v4().to_string(),
                                            false,
                                            now(),
                                        )
                                        .unwrap();
                                }
                                "replace" => {
                                    store
                                        .goal_replace(
                                            &server_parent,
                                            id,
                                            revision,
                                            "New user objective",
                                            None,
                                            false,
                                            now(),
                                        )
                                        .unwrap();
                                }
                                "report" => {
                                    store
                                        .goal_recovery(
                                            &server_parent,
                                            "report-barrier",
                                            "Unwatched recovery",
                                            now(),
                                        )
                                        .unwrap();
                                }
                                _ => {}
                            }
                        }
                        json!({"data":if reads<3 || change=="missing"{vec![]}else{vec![json!({"turnId":"accepted-turn","item":{"id":"accepted-message","type":"userMessage","clientId":client}})]},"nextCursor":null})
                    }
                    method => panic!("unexpected mutation or model request: {method}"),
                };
                socket
                    .send(Message::text(
                        json!({"id":request["id"],"result":result}).to_string(),
                    ))
                    .unwrap();
            }
            (adds, reads, starts)
        });
        let mut observer = Observer::new(&mut store, &parent);
        let ack=observer.call("thread/queue/add",json!({"threadId":parent,"clientUserMessageId":dispatch["client"],"input":[{"type":"text","text":dispatch["message"],"textElements":[]}]})).unwrap();
        let acknowledged = observer
            .store
            .goal_dispatch_result(
                text(&dispatch, "id").unwrap(),
                "pending",
                ack["queuedSubmission"]["id"].as_str(),
                None,
                None,
                if change == "expired" {
                    now() - GOAL_SETTLEMENT_SECONDS - 1.0
                } else {
                    now()
                },
            )
            .unwrap();
        match change {
            "uncertain" => {
                observer
                    .store
                    .goal_dispatch_result(
                        text(&dispatch, "id").unwrap(),
                        "uncertain",
                        None,
                        None,
                        Some("Separate genuine delivery uncertainty"),
                        now(),
                    )
                    .unwrap();
            }
            "recovered-start" => {
                observer
                    .store
                    .goal_dispatch_result(
                        text(&dispatch, "id").unwrap(),
                        "starting",
                        acknowledged["queue"].as_str(),
                        None,
                        None,
                        now(),
                    )
                    .unwrap();
            }
            "live-start" => {
                let started = observer
                    .store
                    .goal_dispatch_result(
                        text(&dispatch, "id").unwrap(),
                        "starting",
                        acknowledged["queue"].as_str(),
                        None,
                        None,
                        now(),
                    )
                    .unwrap();
                assert!(
                    observer
                        .call(
                            "thread/queue/start",
                            json!({"threadId":parent,"queuedSubmissionId":started["queue"]})
                        )
                        .is_err()
                );
                assert!(observer.settle_goal_admission(&started).unwrap());
            }
            _ => {}
        }
        for _ in 0..3 {
            observer.goal_reconcile(&[]).unwrap();
        }
        let snapshot = observer.store.goal_status(&parent).unwrap();
        let saved = observer.store.goal_dispatches(&parent).unwrap().remove(0);
        drop(observer);
        (snapshot, saved, server.join().unwrap())
    }

    #[test]
    fn acknowledged_goal_queue_handoff_waits_for_matching_history() {
        let (snapshot, saved, calls) = delayed_goal_handoff("none");
        assert_eq!(calls, (1, 3, 0));
        assert_eq!(saved["accepted_turn"], "accepted-turn");
        assert_eq!(snapshot["goal"]["state"], "active");
        assert_eq!(snapshot["binding"]["turn"], "accepted-turn");
    }

    #[test]
    fn acknowledged_handoff_preserves_newer_user_and_report_authority() {
        for change in ["pause", "input", "replace", "report"] {
            let (snapshot, saved, calls) = delayed_goal_handoff(change);
            assert_eq!(calls, (1, 3, 0));
            assert_eq!(saved["accepted_turn"], "accepted-turn");
            assert!(snapshot["binding"].is_null(), "{change}");
            match change {
                "pause" => assert_eq!(snapshot["goal"]["state"], "paused"),
                "input" => {
                    assert_eq!(snapshot["pending_input"]["state"], "submitting");
                    assert_eq!(snapshot["input_epoch"], 2);
                }
                "replace" => {
                    assert_ne!(snapshot["goal"]["id"], saved["goal"]);
                    assert_eq!(snapshot["goal"]["state"], "active");
                }
                "report" => {
                    assert_eq!(snapshot["report_barrier"], 1);
                    assert_eq!(snapshot["goal"]["state"], "needs_input");
                }
                _ => unreachable!(),
            }
        }
    }

    #[test]
    fn expired_or_recovered_unknown_handoffs_never_auto_renew() {
        for change in ["expired", "uncertain", "recovered-start"] {
            let (snapshot, saved, calls) = delayed_goal_handoff(change);
            assert_eq!(calls, (1, 3, 0));
            assert_eq!(saved["accepted_turn"], "accepted-turn");
            assert_eq!(snapshot["goal"]["state"], "needs_input");
            assert!(snapshot["binding"].is_null());
        }
    }

    #[test]
    fn live_start_failure_observes_consumption_without_retrying_start() {
        let (snapshot, saved, calls) = delayed_goal_handoff("live-start");
        assert_eq!(calls, (1, 3, 1));
        assert_eq!(saved["accepted_turn"], "accepted-turn");
        assert_eq!(snapshot["goal"]["state"], "active");
        assert_eq!(snapshot["binding"]["turn"], "accepted-turn");
    }

    #[test]
    fn acknowledged_handoff_without_positive_evidence_expires_locally() {
        let before = Instant::now();
        let (snapshot, saved, calls) = delayed_goal_handoff("missing");
        assert!(before.elapsed() < Duration::from_secs(7));
        assert_eq!(calls.0, 1);
        assert_eq!(calls.2, 0);
        assert!(saved["accepted_turn"].is_null());
        assert_eq!(saved["state"], "uncertain");
        assert_eq!(snapshot["goal"]["state"], "needs_input");
        assert!(snapshot["binding"].is_null());
    }

    #[test]
    fn ordinary_queue_admission_accounts_for_a_goal_eos_after_the_first_pass() {
        use std::os::unix::net::UnixListener;
        use tungstenite::Message;
        let home = tempfile::tempdir().unwrap();
        let parent = Uuid::new_v4().to_string();
        let mut store = Store::open(home.path()).unwrap();
        let goal = store
            .goal_start(&parent, "review independent completion", None, false, now())
            .unwrap();
        let job = ResolvedJob {
            id: "independent".into(),
            kind: "process".into(),
            thread: parent.clone(),
            turn: Some("origin".into()),
            item: Some("item".into()),
            process: Some("1".into()),
            execution: None,
            status: "armed".into(),
            exit_code: None,
        };
        store
            .register_resolved(
                &parent,
                "origin",
                "independent result",
                &[job],
                &HashMap::new(),
                Some("generation"),
                &HashSet::new(),
            )
            .unwrap();
        let job = store.jobs_to_observe(&parent).unwrap().remove(0);
        store
            .observe_job(&job, Some("completed"), Some(0), None, false)
            .unwrap();
        store.batch(&parent).unwrap();
        let id = goal["goal"]["id"].as_str().unwrap();
        let epoch = goal["input_epoch"].as_i64().unwrap();
        store
            .goal_admit(&parent, id, 1, "origin", epoch, now())
            .unwrap();
        store.goal_checkpoint(&parent,id,1,"origin",epoch,&json!({"disposition":"continue","next_action":"Review the result","evidence":["registered:independent"]}),now()).unwrap();
        let socket_directory = home.path().join("app-server-control");
        std::fs::create_dir(&socket_directory).unwrap();
        let listener =
            UnixListener::bind(socket_directory.join("app-server-control.sock")).unwrap();
        let server_home = home.path().to_path_buf();
        let server_parent = parent.clone();
        let server = std::thread::spawn(move || {
            let mut store = Store::open(&server_home).unwrap();
            let (socket, _) = listener.accept().unwrap();
            socket
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut socket = tungstenite::accept(socket).unwrap();
            let mut first = true;
            let mut admitted = false;
            let mut additions = 0;
            while let Ok(Message::Text(request)) = socket.read() {
                let request: Value = serde_json::from_str(&request).unwrap();
                if request["id"].is_null() {
                    continue;
                }
                let result = match request["method"].as_str().unwrap() {
                    "initialize" => json!({}),
                    "thread/turns/list" => {
                        let status = if first { "inProgress" } else { "completed" };
                        first = false;
                        json!({"data":[{"id":"origin","status":status}],"nextCursor":null})
                    }
                    "thread/read" => {
                        json!({"thread":{"id":server_parent,"status":{"type":if admitted{"active"}else{"idle"}}}})
                    }
                    "thread/queue/list" => json!({"data":[],"nextCursor":null}),
                    "thread/queue/add" => {
                        assert!(
                            store.goal_turns(&server_parent).unwrap().is_empty(),
                            "old goal turn must finalize before queue/add can admit the ordinary callback"
                        );
                        assert_eq!(
                            store.goal_status(&server_parent).unwrap()["goal"]["revision"],
                            2
                        );
                        admitted = true;
                        additions += 1;
                        json!({"queuedSubmission":{"id":"ordinary-queue","clientUserMessageId":request["params"]["clientUserMessageId"]}})
                    }
                    method => panic!("unexpected method {method}"),
                };
                socket
                    .send(Message::text(
                        json!({"id":request["id"],"result":result}).to_string(),
                    ))
                    .unwrap();
            }
            additions
        });
        let mut observer = Observer::new(&mut store, &parent);
        assert!(!observer.account_goal_turns().unwrap());
        observer.deliver().unwrap();
        drop(observer);
        assert_eq!(server.join().unwrap(), 1);
        let current = store.goal_status(&parent).unwrap();
        store
            .goal_admit(&parent, id, 2, "ordinary-callback", epoch, now())
            .unwrap();
        store
            .goal_checkpoint(
                &parent,
                id,
                2,
                "ordinary-callback",
                epoch,
                &json!({"disposition":"needs_input","reason":"Independent result reviewed"}),
                now(),
            )
            .unwrap();
        assert_eq!(
            store
                .goal_finalize(&parent, "ordinary-callback", true, now())
                .unwrap()["goal"]["state"],
            "needs_input"
        );
        assert_eq!(current["binding"], Value::Null);
    }

    #[test]
    fn ordinary_callback_priority_uses_actual_dispatch_eligibility() {
        let home = tempfile::tempdir().unwrap();
        let mut store = Store::open(home.path()).unwrap();
        let parent = Uuid::new_v4().to_string();
        let job = ResolvedJob {
            id: "independent".into(),
            kind: "process".into(),
            thread: parent.clone(),
            turn: Some("origin".into()),
            item: Some("item".into()),
            process: Some("1".into()),
            execution: None,
            status: "armed".into(),
            exit_code: None,
        };
        store
            .register_resolved(
                &parent,
                "origin",
                "ordinary result",
                &[job],
                &HashMap::new(),
                Some("generation"),
                &HashSet::new(),
            )
            .unwrap();
        let job = store.jobs_to_observe(&parent).unwrap().remove(0);
        store
            .observe_job(&job, Some("completed"), Some(0), None, false)
            .unwrap();
        store.batch(&parent).unwrap();
        let mut observer = Observer::new(&mut store, &parent);
        assert!(observer.ordinary_callback_ready(&[]).unwrap());
        assert!(
            !observer
                .ordinary_callback_ready(&[json!({"clientUserMessageId":"already-admitted-goal"})])
                .unwrap()
        );
        observer
            .store
            .execute(
                "UPDATE callbacks SET idempotent=1 WHERE parent=?",
                &[json!(parent)],
            )
            .unwrap();
        assert!(!observer.ordinary_callback_ready(&[]).unwrap());
        observer
            .store
            .execute(
                "UPDATE callbacks SET idempotent=0,state='uncertain' WHERE parent=?",
                &[json!(parent)],
            )
            .unwrap();
        assert!(!observer.ordinary_callback_ready(&[]).unwrap());
    }

    #[test]
    fn repeated_launches_do_not_spawn_contenders_while_supervisor_is_owned() {
        let home = tempfile::tempdir().unwrap();
        let parent = Uuid::new_v4().to_string();
        let held = RoleLock::acquire(home.path(), &parent, "supervise").unwrap();
        let missing_package = home.path().join("no-runtime-can-be-spawned");
        let before = Instant::now();
        for _ in 0..50 {
            launch(&parent, home.path(), &missing_package).unwrap();
        }
        assert!(before.elapsed() < Duration::from_secs(2));
        assert!(
            RoleLock::try_acquire(home.path(), &parent, "supervise")
                .unwrap()
                .is_none()
        );
        drop(held);
        assert!(launch(&parent, home.path(), &missing_package).is_err());
    }

    #[test]
    fn detached_launch_rejects_corrupt_runtime_without_shell_fallback() {
        use std::os::unix::fs::PermissionsExt;
        let package = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        std::fs::create_dir(package.path().join("bin")).unwrap();
        let runtime = package.path().join("bin/codex-plus-runtime");
        std::fs::write(&runtime, "touch unexpected-shell-fallback\n").unwrap();
        std::fs::set_permissions(&runtime, std::fs::Permissions::from_mode(0o755)).unwrap();
        let error = launch(&Uuid::new_v4().to_string(), home.path(), package.path()).unwrap_err();
        assert_eq!(
            error
                .downcast_ref::<std::io::Error>()
                .unwrap()
                .raw_os_error(),
            Some(libc::ENOEXEC)
        );
        assert!(!package.path().join("unexpected-shell-fallback").exists());
    }
}
