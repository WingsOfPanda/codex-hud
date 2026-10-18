//! Root-only custom goal commands. Goal state is owned by the protected ledger.
use crate::{
    ledger::Store,
    local::{now, state_home},
    rpc::Client,
};
use anyhow::{Context, Result, bail};
use clap::Parser;
use serde_json::{Value, json};
use std::{
    ffi::OsString,
    fs::{File, OpenOptions},
    os::{fd::AsRawFd, unix::fs::OpenOptionsExt},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};
use uuid::Uuid;

#[derive(Parser)]
#[command(name = "codex-plus goal")]
struct Arguments {
    #[arg(value_parser=["goal"])]
    command: String,
    #[arg(value_parser=["start","status","history","instructions","checkpoint","continue","wait","needs-input","complete","pause","resume","edit","review","clear","input","input-settle","admit","provenance","reconnect"])]
    action: String,
    #[arg(long, alias = "parent")]
    thread: Option<String>,
    #[arg(long)]
    id: Option<String>,
    #[arg(long)]
    revision: Option<i64>,
    #[arg(long)]
    turn: Option<String>,
    #[arg(long)]
    input_epoch: Option<i64>,
    #[arg(long)]
    submission_id: Option<String>,
    #[arg(long)]
    accepted: bool,
    #[arg(long,value_parser=["accepted","failed","uncertain"])]
    outcome: Option<String>,
    #[arg(long)]
    objective: Option<String>,
    #[arg(long)]
    checkpoint: Option<String>,
    #[arg(long)]
    review_seconds: Option<i64>,
    #[arg(long)]
    paused: bool,
    #[arg(long)]
    renew: bool,
    #[arg(long)]
    cwd: Option<String>,
    #[arg(long,value_parser=["fresh","cold-resume"])]
    load_kind: Option<String>,
    #[arg(long)]
    json: bool,
}

/// Serialize user mutations with the actual queue admission boundary.
pub(crate) struct GoalLock(File);
impl GoalLock {
    pub(crate) fn acquire(directory: &Path, parent: &str) -> Result<Self> {
        let file = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(directory.join(format!("{parent}.goal-mutation.lock")))?;
        if !file.metadata()?.is_file() {
            bail!("goal mutation lock must be a regular file");
        }
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
                return Ok(Self(file));
            }
            let error = std::io::Error::last_os_error();
            if error.kind() != std::io::ErrorKind::WouldBlock {
                return Err(error.into());
            }
            if Instant::now() >= deadline {
                bail!(
                    "goal mutation lock is busy; queue admission outcome is still being recorded"
                );
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}
impl Drop for GoalLock {
    fn drop(&mut self) {
        unsafe {
            libc::flock(self.0.as_raw_fd(), libc::LOCK_UN);
        }
    }
}

fn home() -> Result<PathBuf> {
    let home = state_home();
    Ok(if home.is_absolute() {
        home
    } else {
        std::env::current_dir()?.join(home)
    })
}
pub(crate) fn root(meta: &Value, id: &str) -> bool {
    meta["id"] == id && meta["parentThreadId"].is_null() && meta["source"].get("subAgent").is_none()
}
fn client(home: &Path) -> Result<Client> {
    Client::connect(
        &home.join("app-server-control/app-server-control.sock"),
        Duration::from_secs(5),
    )
}

fn uptime() -> Result<f64> {
    let text = std::fs::read_to_string("/proc/uptime")?;
    let value: f64 = text
        .split_whitespace()
        .next()
        .context("host monotonic uptime unavailable")?
        .parse()?;
    if !value.is_finite() || value < 0.0 {
        bail!("invalid host monotonic uptime");
    }
    Ok(value)
}

fn observation_gap(proof: &Value, generation: &str, monotonic: f64) -> Result<bool> {
    let old = proof["generation"]
        .as_str()
        .context("invalid daemon generation")?;
    Ok(old.split(':').next() != generation.split(':').next()
        || proof["last_observed_uptime"]
            .as_f64()
            .is_some_and(|last| monotonic - last > 30.0))
}

fn preserve_observation_gap(
    store: &mut Store,
    parent: &str,
    proof: &Value,
    generation: &str,
    monotonic: f64,
) -> Result<()> {
    let snapshot = store.goal_status(parent)?;
    if needs_observation(&snapshot)
        && matches!(
            snapshot["goal"]["state"].as_str(),
            Some("active" | "waiting" | "needs_input")
        )
        && observation_gap(proof, generation, monotonic)?
    {
        let report = format!("{}:{}", generation, proof["last_observed_at"]);
        store.goal_recovery(parent,&report,"Recovered after an unwatched gap; report outcomes and await renewed user direction. Stock already-admitted interrupted work may independently replay.",now())?;
    }
    Ok(())
}

pub(crate) fn needs_observation(snapshot: &Value) -> bool {
    matches!(
        snapshot["goal"]["state"].as_str(),
        Some("active" | "waiting")
    ) || snapshot["running_jobs"].as_i64().unwrap_or(0) > 0
        || snapshot["open_dispatches"]
            .as_array()
            .is_some_and(|rows| !rows.is_empty())
        || snapshot["report_barrier"] == 1
            && snapshot["report_delivered"] == 0
            && matches!(
                snapshot["goal"]["state"].as_str(),
                Some("active" | "waiting" | "needs_input")
            )
}

/// Loading provenance is provided only by controlled Plus fresh/cold load paths.
pub(crate) fn isolation(store: &mut Store, client: &mut Client, parent: &str) -> Result<Value> {
    let mut proof=store.goal_provenance(parent,None)?.context("controlled Plus load provenance is missing; reconnect through a qualified fresh or cold load")?;
    if proof["root"] != parent {
        bail!("controlled Plus load provenance does not match this root");
    }
    let meta = client.call(
        "thread/read",
        json!({"threadId":parent,"includeTurns":false}),
    )?;
    if !root(&meta["thread"], parent) {
        bail!("isolation requires the exact root thread");
    }
    let cwd = meta["thread"]["cwd"]
        .as_str()
        .context("root cwd unavailable")?;
    let effective = client.native_goals_disabled(cwd)?;
    proof["daemon_identity"] = effective["daemonIdentity"].clone();
    proof["cwd"] = json!(cwd);
    let monotonic = uptime()?;
    preserve_observation_gap(store, parent, &proof, &client.generation, monotonic)?;
    proof["generation"] = json!(client.generation);
    proof["last_observed_at"] = json!(now());
    proof["last_observed_uptime"] = json!(monotonic);
    store.goal_provenance(parent, Some(&proof))?;
    Ok(proof)
}

pub(crate) fn instructions(snapshot: &Value) -> String {
    let goal = &snapshot["goal"];
    if !goal.is_object() {
        return String::new();
    }
    format!(
        "[Codex Plus custom goal]\nObjective: {}\nGoal ID: {}; revision: {}; input epoch: {}; root: {}.\nHandle ordinary user instructions first. Before ending an admitted goal turn, use codex-plus goal checkpoint --thread {} --id {} --revision {} --turn EXACT_TURN_FROM_STATUS_BINDING_OR_CURRENT_TURN --input-epoch {} --checkpoint JSON --json. Use the exact turn in goal status binding; if unadmitted, goal status reports current_turn from the local daemon. When continuing the goal after handling user input, first call goal admit with that same tuple before registering any goal-owned work or checkpointing. Ordinary unrelated turns must not be admitted. Never clear a report-only recovery barrier through ordinary input; explicit renewed direction or /goal resume is required. JSON must have disposition continue (next_action plus stable evidence refs), wait (exact registered job IDs), needs_input (reason), or complete (evidence). Register finite work with park and end the turn while waiting; do not poll the model. A checkpoint only takes effect after positive successful end-of-turn evidence. Stale revisions/input epochs fail; read current state and reassess user intent rather than overwriting it. Pause/edit/clear do not cancel running jobs. Goal state: {}; checkpoint: {}. A report-only recovery barrier requires renewed user direction, never an automatic Continue.\n[/Codex Plus custom goal]",
        goal["objective"].as_str().unwrap_or(""),
        goal["id"],
        goal["revision"],
        snapshot["input_epoch"],
        snapshot["root"],
        snapshot["root"].as_str().unwrap_or(""),
        goal["id"].as_str().unwrap_or(""),
        goal["revision"],
        snapshot["input_epoch"],
        goal["state"],
        goal["checkpoint"]
    )
}

fn identity(args: &Arguments) -> Result<(&str, i64)> {
    Ok((
        args.id
            .as_deref()
            .context("--id is required for compare-and-swap")?,
        args.revision
            .context("--revision is required for compare-and-swap")?,
    ))
}
fn binding(args: &Arguments) -> Result<(&str, i64, &str, i64)> {
    let (id, revision) = identity(args)?;
    Ok((
        id,
        revision,
        args.turn.as_deref().context("--turn is required")?,
        args.input_epoch.context("--input-epoch is required")?,
    ))
}

fn local_input_allowed(args: &Arguments, model_context: bool, status: &Value) -> bool {
    !model_context
        && args.thread.is_some()
        && matches!(args.action.as_str(), "input" | "input-settle")
        && status["goal"].is_null()
        && status["binding"].is_null()
        && status["running_jobs"].as_i64() == Some(0)
        && status["open_dispatches"]
            .as_array()
            .is_some_and(Vec::is_empty)
        && status["report_barrier"] == 0
}

fn input_mutation(
    args: &Arguments,
    store: &mut Store,
    parent: &str,
    timestamp: f64,
    mut rpc: Option<&mut Client>,
) -> Result<Value> {
    if args.action == "input" {
        let submission = args
            .submission_id
            .clone()
            .unwrap_or_else(|| Uuid::new_v4().to_string());
        let result = store.goal_input_submission(parent, &submission, args.renew, timestamp)?;
        if let Some(rpc) = rpc.as_mut() {
            crate::ultracode_command::reserve_optional(store, parent, &submission, &rpc.generation);
        }
        return Ok(result);
    }
    let submission = args
        .submission_id
        .as_deref()
        .context("--submission-id is required")?;
    let outcome = if args.accepted {
        "accepted"
    } else {
        args.outcome
            .as_deref()
            .context("--accepted or --outcome is required")?
    };
    if outcome == "accepted" {
        let turn = args
            .turn
            .as_deref()
            .context("--turn is required for accepted input")?;
        if let Some(rpc) = rpc.as_mut() {
            if !crate::background::goal_known_turn(rpc, parent, turn)? {
                bail!("input admission receipt must identify an exact known root turn");
            }
        }
    }
    let result =
        store.goal_input_settle(parent, submission, args.turn.as_deref(), outcome, timestamp)?;
    if let Some(rpc) = rpc.as_mut() {
        crate::ultracode_command::input_optional(
            store,
            rpc,
            parent,
            submission,
            args.turn.as_deref(),
            outcome,
        );
    }
    Ok(result)
}

fn execute(args: &Arguments, package_root: &Path) -> Result<Value> {
    let env_root = std::env::var("CODEX_THREAD_ID").ok();
    let model_context = env_root.is_some();
    if args.action == "complete" && env_root.is_some() && args.turn.is_none() {
        bail!(
            "model completion requires --turn and --input-epoch; stage completion for successful end-of-turn finalization"
        );
    }
    let parent = Uuid::parse_str(
        args.thread
            .as_deref()
            .or(env_root.as_deref())
            .context("set CODEX_THREAD_ID or pass --thread")?,
    )?
    .to_string();
    if let Some(own) = env_root {
        if Uuid::parse_str(&own)?.to_string() != parent {
            bail!("goal CLI cannot mutate or inspect another root from this session");
        }
    }
    let home = home()?;
    let mut store = Store::open(&home)?;
    if matches!(args.action.as_str(), "status" | "history" | "instructions") {
        let mut snapshot = if args.action == "history" {
            store.goal_snapshot(&parent)?
        } else {
            store.goal_status(&parent)?
        };
        if args.action != "history" {
            if let Ok(mut rpc) = client(&home) {
                if let Ok(current) = crate::background::goal_current_turn(&mut rpc, &parent) {
                    snapshot["current_turn"] = current;
                }
            }
        }
        snapshot["instructions"] = json!(instructions(&snapshot));
        return Ok(snapshot);
    }
    let _mutation_lock = GoalLock::acquire(&store.directory, &parent)?;
    if args.action == "reconnect"
        && !needs_observation(&store.goal_status(&parent)?)
        && (!model_context || store.goal_status(&parent)?["report_barrier"] == 0)
    {
        let mut snapshot = store.goal_status(&parent)?;
        snapshot["instructions"] = json!(instructions(&snapshot));
        return Ok(snapshot);
    }
    // Embedded stock clients have no control socket; an inactive local ledger cannot schedule goals.
    if local_input_allowed(args, model_context, &store.goal_status(&parent)?) {
        let mut mode_rpc = if store
            .directory
            .join("ultracode")
            .join(format!("{parent}.json"))
            .is_file()
        {
            client(&home).ok()
        } else {
            None
        };
        let result = input_mutation(args, &mut store, &parent, now(), mode_rpc.as_mut())?;
        store.publish(&parent)?;
        let mut snapshot = store.goal_status(&parent)?;
        snapshot["instructions"] = json!(instructions(&snapshot));
        snapshot["result"] = result;
        return Ok(snapshot);
    }
    let mut rpc = client(&home)?;
    let meta = rpc.call(
        "thread/read",
        json!({"threadId":parent,"includeTurns":false}),
    )?;
    if !root(&meta["thread"], &parent) {
        bail!("custom goals require an interactive root thread");
    }
    if model_context && store.goal_model_mutation_blocked(&parent)? {
        bail!(
            "a report-only model turn must report and wait; goal mutations require new user direction or an explicit host /goal action"
        );
    }
    let timestamp = now();
    let result = match args.action.as_str() {
        "provenance" => {
            let cwd = args
                .cwd
                .as_deref()
                .or(meta["thread"]["cwd"].as_str())
                .context("root cwd unavailable")?;
            if !Path::new(cwd).is_absolute() {
                bail!("root cwd must be absolute");
            }
            if meta["thread"]["cwd"].as_str() != Some(cwd) {
                bail!("provenance cwd must match the loaded root");
            }
            if meta["thread"]["status"]["type"] == "notLoaded" {
                bail!("record provenance only after a controlled root load succeeds");
            }
            let kind = args
                .load_kind
                .as_deref()
                .context("--load-kind requires a controlled fresh or cold-resume load")?;
            let effective = rpc.native_goals_disabled(cwd)?;
            let monotonic = uptime()?;
            if let Some(old) = store.goal_provenance(&parent, None)? {
                preserve_observation_gap(&mut store, &parent, &old, &rpc.generation, monotonic)?;
            }
            let proof = json!({"version":1,"root":parent,"generation":rpc.generation,"cwd":cwd,"load_kind":kind,"native_goals":false,"recorded_at":timestamp,"last_observed_at":timestamp,"last_observed_uptime":monotonic,"daemon_identity":effective["daemonIdentity"]});
            store.goal_provenance(&parent, Some(&proof))?;
            proof
        }
        "reconnect" => {
            let snapshot = store.goal_status(&parent)?;
            if matches!(
                snapshot["goal"]["state"].as_str(),
                Some("active" | "waiting")
            ) || snapshot["report_barrier"] == 1
                && snapshot["report_delivered"] == 0
                && snapshot["goal"]["state"] == "needs_input"
            {
                isolation(&mut store, &mut rpc, &parent)?;
            }
            store.goal_status(&parent)?
        }
        "start" => {
            if !args.paused {
                isolation(&mut store, &mut rpc, &parent)?;
            }
            {
                let objective = args
                    .objective
                    .as_deref()
                    .context("--objective is required")?;
                if args.id.is_some() || args.revision.is_some() {
                    let (id, revision) = identity(args)?;
                    store.goal_replace(
                        &parent,
                        id,
                        revision,
                        objective,
                        args.review_seconds,
                        args.paused,
                        timestamp,
                    )?
                } else {
                    store.goal_start(
                        &parent,
                        objective,
                        args.review_seconds,
                        args.paused,
                        timestamp,
                    )?
                }
            }
        }
        "input" | "input-settle" => {
            input_mutation(args, &mut store, &parent, timestamp, Some(&mut rpc))?
        }
        "admit" => {
            let (id, revision, turn, epoch) = binding(args)?;
            let current = crate::background::goal_current_turn(&mut rpc, &parent)?;
            if current["id"] != turn || current["status"] != "inProgress" {
                bail!("admit must bind the exact current active root turn");
            }
            store.goal_admit(&parent, id, revision, turn, epoch, timestamp)?
        }
        "complete" if args.turn.is_some() => {
            let (id, revision, turn, epoch) = binding(args)?;
            let mut disposition: Value = serde_json::from_str(
                args.checkpoint
                    .as_deref()
                    .context("--checkpoint JSON with completion evidence is required")?,
            )?;
            if !disposition.is_object() {
                bail!("checkpoint must be a JSON object");
            }
            disposition["disposition"] = json!("complete");
            store.goal_checkpoint(&parent, id, revision, turn, epoch, &disposition, timestamp)?
        }
        "checkpoint" | "continue" | "wait" | "needs-input" => {
            let (id, revision, turn, epoch) = binding(args)?;
            let mut disposition: Value = serde_json::from_str(
                args.checkpoint
                    .as_deref()
                    .context("--checkpoint JSON is required")?,
            )?;
            if !disposition.is_object() {
                bail!("checkpoint must be a JSON object");
            }
            if args.action != "checkpoint" {
                disposition["disposition"] = json!(args.action.replace('-', "_"));
            }
            store.goal_checkpoint(&parent, id, revision, turn, epoch, &disposition, timestamp)?
        }
        action => {
            let (id, revision) = identity(args)?;
            if action == "resume" {
                isolation(&mut store, &mut rpc, &parent)?;
            }
            let mut payload = args
                .checkpoint
                .as_deref()
                .map(serde_json::from_str::<Value>)
                .transpose()?
                .unwrap_or_else(|| json!({}));
            if !payload.is_object() {
                bail!("mutation payload must be a JSON object");
            }
            if args.objective.is_some() {
                payload["objective"] = json!(args.objective);
            }
            if args.review_seconds.is_some() {
                payload["review_seconds"] = json!(args.review_seconds);
            }
            store.goal_mutate(&parent, id, revision, action, &payload, timestamp)?
        }
    };
    let resumed_input = args.action == "input-settle"
        && (args.accepted || args.outcome.as_deref() == Some("accepted"))
        && matches!(
            store.goal_status(&parent)?["goal"]["state"].as_str(),
            Some("active" | "waiting")
        );
    let resumed_observation = matches!(args.action.as_str(), "provenance" | "reconnect")
        && needs_observation(&store.goal_status(&parent)?);
    if resumed_input
        || resumed_observation
        || !matches!(
            args.action.as_str(),
            "provenance"
                | "reconnect"
                | "checkpoint"
                | "continue"
                | "wait"
                | "needs-input"
                | "admit"
                | "input"
                | "input-settle"
        )
    {
        if let Err(error) = crate::background::launch(&parent, &home, package_root) {
            store.goal_set_error(
                &parent,
                &format!("Goal observer launch failed: {error:#}"),
                timestamp,
            )?;
            return Err(error);
        }
    }
    store.publish(&parent)?;
    let mut snapshot = store.goal_status(&parent)?;
    snapshot["instructions"] = json!(instructions(&snapshot));
    snapshot["result"] = result;
    Ok(snapshot)
}

pub fn main(argv: &[OsString], package_root: &Path) -> i32 {
    let args = Arguments::try_parse_from(
        std::iter::once(OsString::from("codex-plus")).chain(argv.iter().cloned()),
    );
    let args = match args {
        Ok(args) => args,
        Err(error) => {
            println!("{}", json!({"error":error.to_string()}));
            return error.exit_code();
        }
    };
    match execute(&args, package_root) {
        Ok(result) => {
            println!("{result}");
            0
        }
        Err(error) => {
            println!("{}", json!({"error":format!("{error:#}")}));
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cold_load_preserves_a_same_boot_gap_once_before_refreshing_provenance() {
        let home = tempfile::tempdir().unwrap();
        let mut store = Store::open(home.path()).unwrap();
        let root = Uuid::new_v4().to_string();
        store
            .goal_start(&root, "keep recovery ordering", None, false, 1.0)
            .unwrap();
        let old = json!({"generation":"boot:10:start","last_observed_uptime":10.0,"last_observed_at":1.0});
        assert!(!observation_gap(&old, "boot:11:start", 20.0).unwrap());
        assert!(observation_gap(&old, "boot:11:start", 50.0).unwrap());
        preserve_observation_gap(&mut store, &root, &old, "boot:11:start", 50.0).unwrap();
        let first = store.goal_status(&root).unwrap();
        assert_eq!(first["report_barrier"], 1);
        assert_eq!(first["goal"]["state"], "needs_input");
        preserve_observation_gap(&mut store, &root, &old, "boot:11:start", 51.0).unwrap();
        let again = store.goal_status(&root).unwrap();
        assert_eq!(again["report_id"], first["report_id"]);
        assert_eq!(again["goal"]["revision"], first["goal"]["revision"]);
        assert!(needs_observation(&again));
        let paused = store
            .goal_mutate(
                &root,
                again["goal"]["id"].as_str().unwrap(),
                again["goal"]["revision"].as_i64().unwrap(),
                "pause",
                &json!({}),
                2.0,
            )
            .unwrap();
        preserve_observation_gap(&mut store, &root, &old, "newboot:12:start", 10.0).unwrap();
        let unchanged = store.goal_status(&root).unwrap();
        assert_eq!(unchanged["goal"]["state"], "paused");
        assert_eq!(unchanged["goal"]["revision"], paused["goal"]["revision"]);
        assert!(!needs_observation(&unchanged));
    }
    #[test]
    fn observation_lifetime_covers_zero_jobs_and_retained_jobs_without_unpausing() {
        let mut status = json!({"goal":null,"running_jobs":0,"open_dispatches":[],"report_barrier":0,"report_delivered":0});
        assert!(!needs_observation(&status));
        status["goal"] = json!({"state":"active"});
        assert!(needs_observation(&status));
        status["goal"]["state"] = json!("paused");
        assert!(!needs_observation(&status));
        status["running_jobs"] = json!(1);
        assert!(needs_observation(&status));
        status["running_jobs"] = json!(0);
        status["goal"]["state"] = json!("needs_input");
        status["report_barrier"] = json!(1);
        assert!(needs_observation(&status));
        status["report_delivered"] = json!(1);
        assert!(!needs_observation(&status));
        status["open_dispatches"] = json!([{"state":"accepted"}]);
        assert!(needs_observation(&status));
    }
    #[test]
    fn quiescent_needs_input_does_not_create_a_reconnect_review() {
        let home = tempfile::tempdir().unwrap();
        let mut store = Store::open(home.path()).unwrap();
        let root = Uuid::new_v4().to_string();
        store
            .goal_start(&root, "needs a user decision", None, false, 1.0)
            .unwrap();
        store
            .goal_set_error(&root, "No further authorized work", 2.0)
            .unwrap();
        let old = json!({"generation":"boot:10:start","last_observed_uptime":10.0,"last_observed_at":1.0});
        preserve_observation_gap(&mut store, &root, &old, "newboot:20:start", 100.0).unwrap();
        let status = store.goal_status(&root).unwrap();
        assert_eq!(status["goal"]["state"], "needs_input");
        assert_eq!(status["report_barrier"], 0);
        assert!(!needs_observation(&status));
    }
    #[test]
    fn embedded_input_requires_an_authoritatively_inactive_root() {
        let parent = Uuid::new_v4().to_string();
        let args = Arguments::try_parse_from(["codex-plus", "goal", "input", "--thread", &parent])
            .unwrap();
        let status = json!({"goal":null,"binding":null,"running_jobs":0,"open_dispatches":[],"report_barrier":0});
        assert!(local_input_allowed(&args, false, &status));
        assert!(!local_input_allowed(&args, true, &status));
        for (key, value) in [
            ("goal", json!({"state":"paused"})),
            ("binding", json!({"turn":"admitted"})),
            ("running_jobs", json!(1)),
            ("open_dispatches", json!([{"state":"uncertain"}])),
            ("report_barrier", json!(1)),
        ] {
            let mut blocked = status.clone();
            blocked[key] = value;
            assert!(!local_input_allowed(&args, false, &blocked), "{key}");
        }
        assert!(!local_input_allowed(&args, false, &json!({})));
    }
    #[test]
    fn embedded_input_records_and_settles_a_fence_without_a_daemon() {
        let home = tempfile::tempdir().unwrap();
        let mut store = Store::open(home.path()).unwrap();
        let parent = Uuid::new_v4().to_string();
        let submission = Uuid::new_v4().to_string();
        let args = Arguments::try_parse_from([
            "codex-plus",
            "goal",
            "input",
            "--thread",
            &parent,
            "--submission-id",
            &submission,
        ])
        .unwrap();
        assert!(local_input_allowed(
            &args,
            false,
            &store.goal_status(&parent).unwrap()
        ));
        let recorded = input_mutation(&args, &mut store, &parent, 1.0, None).unwrap();
        assert_eq!(recorded["pending_input"]["submission_id"], submission);
        let settled = Arguments::try_parse_from([
            "codex-plus",
            "goal",
            "input-settle",
            "--thread",
            &parent,
            "--submission-id",
            &submission,
            "--turn",
            "embedded-turn",
            "--accepted",
        ])
        .unwrap();
        assert!(local_input_allowed(
            &settled,
            false,
            &store.goal_status(&parent).unwrap()
        ));
        assert!(
            input_mutation(&settled, &mut store, &parent, 2.0, None).unwrap()["pending_input"]
                .is_null()
        );
        store
            .goal_start(&parent, "paused preserved goal", None, true, 3.0)
            .unwrap();
        assert!(!local_input_allowed(
            &args,
            false,
            &store.goal_status(&parent).unwrap()
        ));
    }
    #[test]
    fn mutation_lock_excludes_other_handles_and_refuses_symlinks() {
        use std::os::unix::fs::symlink;
        let directory = tempfile::tempdir().unwrap();
        let parent = Uuid::new_v4().to_string();
        let held = GoalLock::acquire(directory.path(), &parent).unwrap();
        let file = File::open(
            directory
                .path()
                .join(format!("{parent}.goal-mutation.lock")),
        )
        .unwrap();
        assert_ne!(
            unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
            0
        );
        drop(held);
        assert_eq!(
            unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
            0
        );
        drop(file);
        let other = Uuid::new_v4().to_string();
        symlink(
            "target",
            directory.path().join(format!("{other}.goal-mutation.lock")),
        )
        .unwrap();
        assert!(GoalLock::acquire(directory.path(), &other).is_err());
    }
    #[test]
    fn cli_requires_explicit_binding() {
        let args =
            Arguments::try_parse_from(["codex-plus", "goal", "checkpoint", "--json"]).unwrap();
        assert!(binding(&args).is_err());
    }
    #[test]
    fn subagents_cannot_own_goals() {
        assert!(root(&json!({"id":"root","source":"cli"}), "root"));
        assert!(!root(
            &json!({"id":"child","parentThreadId":"root"}),
            "child"
        ));
        assert!(!root(
            &json!({"id":"child","source":{"subAgent":{}}}),
            "child"
        ));
    }
}
