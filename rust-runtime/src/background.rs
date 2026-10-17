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

trait Calls {
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

fn pages(client: &mut impl Calls, method: &str, params: Value) -> Result<Vec<Value>> {
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
                    status: "armed".into(),
                    exit_code: None,
                });
                continue;
            }
            if history.is_none() {
                history = Some(pages(
                    client,
                    "thread/items/list",
                    json!({"threadId":parent,"sortDirection":"desc"}),
                )?);
            }
            let matches: Vec<_> = history
                .as_ref()
                .unwrap()
                .iter()
                .filter(|entry| {
                    entry["item"]["type"] == "commandExecution"
                        && entry["item"]["processId"] == process.as_str()
                })
                .collect();
            if matches.len() != 1 {
                return Err(RegistrationError(format!(
                    "process {process:?} must uniquely match a parent command item"
                ))
                .into());
            }
            let entry = matches[0];
            let item = &entry["item"];
            let id = text(item, "id")?;
            resolved.push(ResolvedJob {
                id: format!("{parent}:process:item:{id}"),
                kind: "process".into(),
                thread: parent.into(),
                turn: Some(text(entry, "turnId")?.into()),
                item: Some(id.into()),
                process: Some(process.clone()),
                status: command_status(item).into(),
                exit_code: exit_code(&item["exitCode"]),
            });
        }
    }
    store.register_resolved(
        parent,
        text(&origin, "id")?,
        reason,
        &resolved,
        &labels,
        Some(&client.generation),
        &pending,
    )
}

struct Observer<'a> {
    store: &'a mut Store,
    parent: &'a str,
    client: Option<Client>,
    recovery_loaded: bool,
    run_id: String,
    last_heartbeat: Option<Instant>,
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
                self.call(
                    "thread/resume",
                    json!({"threadId":self.parent,"excludeTurns":true}),
                )?;
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
            if park["deadline"]
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
        let jobs = self.store.jobs_to_observe(self.parent)?;
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
            } else {
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
        Ok(find_item(self, self.parent, None, |entry| {
            entry["item"]["type"] == "userMessage"
                && entry["item"]["clientId"] == callback["client"]
        })?
        .is_some())
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
            self.reconcile(&callback, &queue)?;
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
            self.cancel_interrupted()?;
            let Some(ready) = self
                .store
                .claim_callback(&ready, false, self.native_queue())?
            else {
                return Ok(());
            };
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
        self.cancel_interrupted()?;
        if self
            .store
            .claim_callback(&pending, true, self.native_queue())?
            .is_none()
        {
            return Ok(());
        }
        let result = (|| -> Result<()> {
            self.call(
                "thread/queue/start",
                json!({"threadId":self.parent,"queuedSubmissionId":pending["queue"]}),
            )?;
            if flag(&pending, "idempotent") {
                self.reconcile(&pending, &[])?;
            } else {
                self.store.dispatch_result(&pending, true, None, true)?;
            }
            Ok(())
        })();
        if let Err(error) = result {
            self.store.dispatch_result(&pending, true, None, false)?;
            return Err(error);
        }
        Ok(())
    }

    fn step(&mut self) -> Result<bool> {
        self.store
            .claim_observation(self.parent, &self.run_id, false)?;
        let result = (|| -> Result<bool> {
            if !self.store.has_work(self.parent)? {
                return Ok(false);
            }
            self.recover_generation()?;
            self.cancel_interrupted()?;
            self.poll_jobs()?;
            self.store.batch(self.parent)?;
            self.deliver()?;
            self.store.finish_cycle(self.parent)
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
            while Instant::now() < deadline {
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
    fn acquire(home: &Path, parent: &str, action: &str) -> Result<Self> {
        let suffix = if action == "supervise" {
            "supervisor.lock"
        } else {
            "lock"
        };
        let file = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(state_directory(home)?.join(format!("{parent}.{suffix}")))?;
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
    Ok(0)
}

fn launch(parent: &str, home: &Path, package_root: &Path) -> Result<()> {
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
    crate::local::spawn_exact(&mut command)?;
    Ok(())
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
        && ((args.worker.is_empty() && args.process.is_empty())
            || args.reason.as_ref().is_none_or(|reason| {
                reason.is_empty() || reason.chars().count() > 240 || !printable(reason)
            }))
    {
        eprintln!(
            "codex-plus: park needs explicit --worker/--process and a printable --reason of at most 240 characters"
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
