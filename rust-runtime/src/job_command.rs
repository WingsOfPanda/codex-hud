//! Reviewed root-owned execution commands; execution truth lives in `executions`.
use crate::{
    background, execution_admission, executions, goal_command::GoalLock, ledger::Store, local,
    maintenance, rpc::Client,
};
use anyhow::{Context, Result, ensure};
use clap::{Parser, Subcommand};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    ffi::OsString,
    path::{Path, PathBuf},
    time::Duration,
};
use uuid::Uuid;

#[derive(Parser)]
#[command(
    name = "codex-plus job",
    about = "Review and run detached jobs in a verified unrestricted root."
)]
struct Arguments {
    #[arg(
        long,
        global = true,
        help = "Owning root UUID; defaults to CODEX_THREAD_ID"
    )]
    parent: Option<String>,
    #[command(subcommand)]
    action: Action,
}

#[derive(Subcommand)]
enum Action {
    #[command(
        about = "Freeze a manifest and its declared inputs for root review",
        long_about = "Freeze a JSON manifest: schema=1, topic, argv (absolute native executable or explicit interpreter), cwd, optional inputs [{source:absolute,path:relative}], explicit env/env_refs/secret_refs, exclusive_keys, external_dependencies, expected_duration_seconds, deadline_seconds and cleanup. Use {inputs} in argv for the snapshot directory. Preconditions and cleanup require finite reviewed deadlines."
    )]
    Prepare {
        #[arg(long)]
        manifest: PathBuf,
        #[arg(long)]
        turn: Option<String>,
        #[arg(long)]
        goal: Option<String>,
        #[arg(long)]
        revision: Option<i64>,
        #[arg(long)]
        input_epoch: Option<i64>,
    },
    #[command(about = "Launch exactly the reviewed execution and arm completion observation")]
    Launch {
        #[arg(long)]
        id: String,
        #[arg(long)]
        review_digest: String,
        #[arg(long)]
        reason: Option<String>,
    },
    #[command(about = "Read the durable execution and observation records")]
    Status {
        #[arg(long)]
        id: String,
    },
    #[command(about = "List this root's durable executions")]
    List {
        #[arg(
            long,
            help = "Host-operator inventory of unresolved executions and package pins across all roots"
        )]
        all: bool,
    },
    #[command(
        about = "Request cancellation; host operators may pass --parent while the daemon is down"
    )]
    Cancel {
        #[arg(long)]
        id: String,
    },
    #[command(
        about = "Record verified cleanup evidence; host operators may pass --parent while offline"
    )]
    Resolve {
        #[arg(long)]
        id: String,
        #[arg(long, help = "JSON object or path to a JSON file; never executed")]
        evidence: String,
    },
    #[command(about = "Recover completion observation in REPORT ONLY mode; never replay work")]
    Watch {
        #[arg(long)]
        id: String,
        #[arg(long)]
        reason: String,
    },
    #[command(
        about = "Operator maintenance hold; enter/leave require a host shell outside model context"
    )]
    Maintenance {
        #[command(subcommand)]
        action: MaintenanceAction,
    },
}

#[derive(Subcommand)]
enum MaintenanceAction {
    Enter,
    Status,
    Leave,
}

#[derive(Clone, Debug)]
struct GoalIdentity {
    id: String,
    revision: i64,
    input_epoch: i64,
}

fn canonical_uuid(value: &str) -> Result<String> {
    Ok(Uuid::parse_str(value)?.to_string())
}

fn owning_root(
    explicit: Option<&str>,
    actor: Option<&str>,
    allow_operator: bool,
) -> Result<String> {
    let root = canonical_uuid(
        explicit
            .or(actor)
            .context("set CODEX_THREAD_ID or pass --parent")?,
    )?;
    if let Some(actor) = actor {
        ensure!(
            canonical_uuid(actor)? == root,
            "job command cannot act for another root"
        );
    } else {
        ensure!(
            allow_operator,
            "job preparation, launch and watch require the active native root's CODEX_THREAD_ID"
        );
    }
    Ok(root)
}

fn explicit_goal(
    id: Option<&str>,
    revision: Option<i64>,
    epoch: Option<i64>,
) -> Result<Option<GoalIdentity>> {
    if id.is_none() && revision.is_none() && epoch.is_none() {
        return Ok(None);
    }
    let id =
        canonical_uuid(id.context("--goal, --revision and --input-epoch are required together")?)?;
    let revision = revision.context("--revision is required with --goal")?;
    let input_epoch = epoch.context("--input-epoch is required with --goal")?;
    ensure!(
        revision > 0 && input_epoch >= 0,
        "invalid goal revision/input epoch"
    );
    Ok(Some(GoalIdentity {
        id,
        revision,
        input_epoch,
    }))
}

fn origin(
    snapshot: &Value,
    root: &str,
    turn: &str,
    explicit: Option<&GoalIdentity>,
    blocked: bool,
) -> Result<Value> {
    ensure!(
        snapshot["root"] == root,
        "goal snapshot belongs to another root"
    );
    ensure!(
        !blocked,
        "report-only recovery requires renewed direction before preparing or launching work"
    );
    ensure!(
        snapshot["pending_input_state"].is_null(),
        "user input admission is pending or uncertain"
    );
    let epoch = snapshot["input_epoch"]
        .as_i64()
        .filter(|n| *n >= 0)
        .context("input epoch evidence missing")?;
    let dispatches = snapshot["open_dispatches"]
        .as_array()
        .context("goal dispatch evidence missing")?;
    ensure!(
        dispatches
            .iter()
            .all(|dispatch| dispatch["state"] == "accepted" && dispatch["accepted_turn"] == turn),
        "goal dispatch admission is pending or uncertain"
    );
    let binding = &snapshot["binding"];
    let goal = if binding.is_object() {
        ensure!(
            binding["root"] == root && binding["turn"] == turn,
            "another admitted goal turn is unresolved"
        );
        let explicit = explicit
            .context("admitted goal turns require explicit --goal, --revision and --input-epoch")?;
        ensure!(
            snapshot["goal"]["state"] == "active"
                && snapshot["report_barrier"] == 0
                && snapshot["goal"]["id"] == explicit.id
                && snapshot["goal"]["revision"] == explicit.revision
                && binding["goal"] == explicit.id
                && binding["revision"] == explicit.revision
                && binding["input_epoch"] == explicit.input_epoch
                && epoch == explicit.input_epoch,
            "goal review binding is stale or inactive"
        );
        json!({"id":explicit.id,"revision":explicit.revision})
    } else {
        ensure!(
            binding.is_null() && explicit.is_none(),
            "job review must not implicitly admit a goal turn"
        );
        Value::Null
    };
    Ok(json!({"root":root,"turn":turn,"input_epoch":epoch,"goal":goal}))
}

fn validate_review(
    record: &Value,
    snapshot: &Value,
    root: &str,
    turn: &str,
    blocked: bool,
) -> Result<()> {
    let saved = &record["origin"];
    ensure!(
        record["root"] == root
            && saved["root"] == root
            && saved["turn"] == turn
            && saved.get("goal").is_some(),
        "execution review belongs to another root or originating turn"
    );
    let explicit = if saved["goal"].is_object() {
        explicit_goal(
            saved["goal"]["id"].as_str(),
            saved["goal"]["revision"].as_i64(),
            saved["input_epoch"].as_i64(),
        )?
    } else {
        ensure!(saved["goal"].is_null(), "invalid saved goal binding");
        None
    };
    let current = origin(snapshot, root, turn, explicit.as_ref(), blocked)?;
    for key in ["root", "turn", "input_epoch", "goal"] {
        ensure!(
            saved[key] == current[key],
            "review {key} changed; prepare and review a fresh execution"
        );
    }
    Ok(())
}

fn connect(home: &Path) -> Result<Client> {
    Client::connect(
        &home.join("app-server-control/app-server-control.sock"),
        Duration::from_secs(5),
    )
}

fn current_turn(home: &Path, root: &str) -> Result<String> {
    let current = background::goal_current_turn(&mut connect(home)?, root)?;
    ensure!(
        current["status"] == "inProgress",
        "job mutation requires an active originating root turn"
    );
    canonical_uuid(
        current["id"]
            .as_str()
            .context("current root turn missing")?,
    )
}

fn json_file(path: &Path, limit: usize) -> Result<Value> {
    Ok(serde_json::from_slice(&local::read_regular(path, limit)?)?)
}

fn cleanup_evidence(text: &str) -> Result<Value> {
    if text.trim_start().starts_with('{') {
        ensure!(text.len() <= 16384, "cleanup evidence exceeds 16384 bytes");
        Ok(serde_json::from_str(text)?)
    } else {
        json_file(Path::new(text), 16384)
    }
}

fn reason(text: &str) -> Result<&str> {
    ensure!(
        !text.trim().is_empty() && text.len() <= 240 && !text.chars().any(char::is_control),
        "reason must contain 1-240 printable bytes"
    );
    Ok(text)
}

fn monitoring_failure(
    home: &Path,
    root: &str,
    id: &str,
    execution: Value,
    error: &anyhow::Error,
) -> Value {
    let message = format!("{error:#}");
    let mut monitoring = json!({"state":"recovery_needed","error":message,
        "next":"Inspect this execution, then use job watch for REPORT ONLY recovery; never replay this UUID."});
    match executions::read_observation(home, root, id) {
        Ok(prior) if prior["state"] == "cancelled" => {
            monitoring["state"] = json!("cancelled");
            monitoring["next"] = json!(
                "This watch was explicitly cancelled and cannot be rearmed; inspect job status for results."
            );
        }
        _ => {
            if let Err(marker_error) = executions::mark_observation(
                home,
                root,
                id,
                "recovery_needed",
                &json!({"error":message}),
            ) {
                monitoring["marker_error"] = json!(format!("{marker_error:#}"));
            }
        }
    }
    json!({"id":id,"execution":execution,"watch":null,"monitoring":monitoring})
}

fn observed(
    home: &Path,
    package: &Path,
    root: &str,
    id: &str,
    execution: Value,
    watch: Result<Value>,
) -> (Value, i32) {
    let result = (|| -> Result<Value> {
        let watch = watch?;
        if watch["state"] != "already_finished" {
            background::launch(root, home, package)?;
        }
        let observation = executions::read_observation(home, root, id)?;
        ensure!(
            observation["state"] == "armed",
            "execution observation was not armed"
        );
        Ok(json!({"id":id,"execution":execution,"watch":watch,"observation":observation}))
    })();
    match result {
        Ok(value) => (value, 0),
        Err(error) => (monitoring_failure(home, root, id, execution, &error), 1),
    }
}

fn operator_allowed(actor: Option<&str>) -> Result<()> {
    ensure!(
        actor.is_none(),
        "maintenance enter/leave require an operator shell outside CODEX_THREAD_ID model context"
    );
    Ok(())
}

fn inventory(home: &Path) -> Result<Value> {
    let mut unresolved = Vec::new();
    let mut packages = BTreeSet::new();
    for root in executions::roots(home)? {
        for record in executions::list(home, &root)? {
            if maintenance::execution_is_quiet(&record) {
                continue;
            }
            if !record["package"].is_null() {
                let package = record["package"]
                    .as_str()
                    .context("invalid execution package pin")?;
                ensure!(
                    Path::new(package).is_absolute(),
                    "execution package pin must be absolute"
                );
                packages.insert(package.to_owned());
            }
            unresolved.push(json!({"root":record["root"],"id":record["id"],
                "state":record["state"],"package":record["package"]}));
        }
    }
    Ok(json!({"schema":1,"execution_record_format":1,"unresolved":unresolved,"packages":packages}))
}

fn run(args: &Arguments, package: &Path) -> Result<(Value, i32)> {
    let actor = std::env::var_os("CODEX_THREAD_ID")
        .map(|value| {
            value
                .into_string()
                .map_err(|_| anyhow::anyhow!("invalid actor thread encoding"))
        })
        .transpose()?;
    let home = local::state_home();
    let home = if home.is_absolute() {
        home
    } else {
        std::env::current_dir()?.join(home)
    };
    run_with_context(args, package, &home, actor.as_deref())
}

fn run_with_context(
    args: &Arguments,
    package: &Path,
    home: &Path,
    actor: Option<&str>,
) -> Result<(Value, i32)> {
    if matches!(args.action, Action::List { all: true }) {
        ensure!(
            actor.is_none(),
            "job list --all requires a host operator outside model context"
        );
        return Ok((inventory(home)?, 0));
    }
    if let Action::Maintenance { action } = &args.action {
        let result = match action {
            MaintenanceAction::Status => maintenance::hold_status(&home)?,
            MaintenanceAction::Enter => {
                operator_allowed(actor)?;
                maintenance::hold_enter(&home)?
            }
            MaintenanceAction::Leave => {
                operator_allowed(actor)?;
                maintenance::hold_leave(&home)?
            }
        };
        return Ok((result, 0));
    }
    let allow_operator = matches!(
        args.action,
        Action::Status { .. }
            | Action::List { .. }
            | Action::Cancel { .. }
            | Action::Resolve { .. }
    );
    let root = owning_root(args.parent.as_deref(), actor, allow_operator)?;
    match &args.action {
        Action::Status { id } => {
            let id = canonical_uuid(id)?;
            let mut execution = executions::read(&home, &root, &id)?;
            execution["observation"] = executions::read_observation(&home, &root, &id)?;
            return Ok((execution, 0));
        }
        Action::List { .. } => return Ok((json!(executions::list(&home, &root)?), 0)),
        _ => {}
    }
    if actor.is_none() {
        let execution = match &args.action {
            Action::Cancel { id } => executions::cancel(home, &root, &canonical_uuid(id)?)?,
            Action::Resolve { id, evidence } => executions::resolve(
                home,
                &root,
                &canonical_uuid(id)?,
                &cleanup_evidence(evidence)?,
            )?,
            _ => unreachable!(),
        };
        return Ok((execution, 0));
    }
    let mut store = Store::open(&home)?;
    let _goal = GoalLock::acquire(&store.directory, &root)?;
    // Cancellation and verified cleanup stay available while goals or new admission are paused.
    let _admission = if matches!(args.action, Action::Cancel { .. } | Action::Resolve { .. }) {
        None
    } else {
        Some(maintenance::admission(&home)?)
    };
    let turn = match &args.action {
        Action::Prepare {
            turn: Some(turn), ..
        } => canonical_uuid(turn)?,
        Action::Launch { id, .. } => {
            let reviewed = executions::read(&home, &root, &canonical_uuid(id)?)?;
            canonical_uuid(
                reviewed["origin"]["turn"]
                    .as_str()
                    .context("review origin missing")?,
            )?
        }
        _ => current_turn(&home, &root)?,
    };
    let admission = execution_admission::authorize(&home, &root, &turn)?;
    let snapshot = store.goal_status(&root)?;
    let blocked = store.goal_model_mutation_blocked(&root)?;
    ensure!(
        !blocked,
        "report-only recovery requires renewed direction before job mutations"
    );
    ensure!(
        snapshot["pending_input_state"].is_null(),
        "user input admission is pending or uncertain"
    );
    match &args.action {
        Action::Prepare {
            manifest,
            goal,
            revision,
            input_epoch,
            ..
        } => {
            let explicit = explicit_goal(goal.as_deref(), *revision, *input_epoch)?;
            let binding = origin(&snapshot, &root, &turn, explicit.as_ref(), blocked)?;
            let manifest = json_file(manifest, 2 * 1024 * 1024)?;
            Ok((executions::prepare(&home, &root, &binding, &manifest)?, 0))
        }
        Action::Launch {
            id,
            review_digest,
            reason: why,
        } => {
            let id = canonical_uuid(id)?;
            let why = reason(
                why.as_deref()
                    .unwrap_or("Review detached execution completion"),
            )?;
            let record = executions::read(&home, &root, &id)?;
            ensure!(
                record["review_digest"] == *review_digest,
                "execution review digest mismatch"
            );
            validate_review(&record, &snapshot, &root, &turn, blocked)?;
            let execution = match executions::launch(
                &home,
                package,
                &root,
                &id,
                review_digest,
                &admission,
            ) {
                Ok(execution) => execution,
                Err(error) => {
                    let current = executions::read(&home, &root, &id).unwrap_or_else(|read_error|
                            json!({"id":id,"root":root,"state":"unknown","error":format!("execution record could not be reread after launch failure: {read_error:#}")}));
                    if current["state"] == "prepared" {
                        return Err(error);
                    }
                    return Ok((monitoring_failure(&home, &root, &id, current, &error), 1));
                }
            };
            let watch = connect(&home).and_then(|mut rpc| {
                background::register_execution(&mut store, &mut rpc, &root, &id, why)
            });
            Ok(observed(&home, package, &root, &id, execution, watch))
        }
        Action::Cancel { id } => Ok((executions::cancel(&home, &root, &canonical_uuid(id)?)?, 0)),
        Action::Resolve { id, evidence } => Ok((
            executions::resolve(
                &home,
                &root,
                &canonical_uuid(id)?,
                &cleanup_evidence(evidence)?,
            )?,
            0,
        )),
        Action::Watch { id, reason: why } => {
            let id = canonical_uuid(id)?;
            let execution = executions::reconcile(&home, &root, &id)?;
            let watch = background::recover_execution_watch(&mut store, &root, &id, reason(why)?);
            Ok(observed(&home, package, &root, &id, execution, watch))
        }
        _ => unreachable!(),
    }
}

pub fn main(argv: &[OsString], package_root: &Path) -> i32 {
    let args = Arguments::try_parse_from(
        std::iter::once(OsString::from("codex-plus job")).chain(argv.iter().skip(1).cloned()),
    );
    let args = match args {
        Ok(args) => args,
        Err(error) => {
            let _ = error.print();
            return error.exit_code();
        }
    };
    match run(&args, package_root) {
        Ok((value, code)) => {
            println!("{value}");
            code
        }
        Err(error) => {
            println!("{}", json!({"error":format!("{error:#}")}));
            1
        }
    }
}

#[derive(Parser)]
struct ExecutorArguments {
    #[arg(value_parser=["execution-executor"])]
    command: String,
    #[arg(long)]
    parent: String,
    #[arg(long)]
    id: String,
}

/// Private executor entry: the core validates the persisted launch claim and pinned package.
pub fn executor(argv: &[OsString], package_root: &Path) -> i32 {
    match ExecutorArguments::try_parse_from(
        std::iter::once(OsString::from("codex-plus")).chain(argv.iter().cloned()),
    ) {
        Ok(args) => executions::execute(&local::state_home(), package_root, &args.parent, &args.id),
        Err(error) => {
            let _ = error.print();
            error.exit_code()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const ROOT: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    const TURN: &str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
    const GOAL: &str = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
    fn snapshot() -> Value {
        json!({"root":ROOT,"input_epoch":3,"report_barrier":0,"pending_input_state":null,"open_dispatches":[],"binding":null,"goal":{"id":GOAL,"revision":2,"state":"paused"}})
    }
    fn admitted() -> Value {
        let mut s = snapshot();
        s["goal"]["state"] = json!("active");
        s["binding"] = json!({"root":ROOT,"goal":GOAL,"revision":2,"turn":TURN,"input_epoch":3});
        s
    }
    fn identity() -> GoalIdentity {
        GoalIdentity {
            id: GOAL.into(),
            revision: 2,
            input_epoch: 3,
        }
    }

    #[test]
    fn ordinary_and_admitted_reviews_never_implicitly_change_goal_ownership() {
        let ordinary = origin(&snapshot(), ROOT, TURN, None, false).unwrap();
        assert!(ordinary["goal"].is_null());
        assert!(origin(&snapshot(), ROOT, TURN, Some(&identity()), false).is_err());
        assert!(origin(&admitted(), ROOT, TURN, None, false).is_err());
        let bound = origin(&admitted(), ROOT, TURN, Some(&identity()), false).unwrap();
        let record = json!({"root":ROOT,"origin":bound});
        validate_review(&record, &admitted(), ROOT, TURN, false).unwrap();
        assert!(
            validate_review(
                &json!({"root":ROOT,"origin":ordinary}),
                &admitted(),
                ROOT,
                TURN,
                false
            )
            .is_err()
        );
        assert!(validate_review(&record, &snapshot(), ROOT, TURN, false).is_err());
    }

    #[test]
    fn changed_goal_epoch_turn_pending_input_and_report_barrier_invalidate_review() {
        let binding = origin(&admitted(), ROOT, TURN, Some(&identity()), false).unwrap();
        let record = json!({"root":ROOT,"origin":binding});
        for (field, value) in [
            ("input_epoch", json!(4)),
            ("report_barrier", json!(1)),
            ("pending_input_state", json!("uncertain")),
            ("pending_input_state", json!("submitting")),
        ] {
            let mut changed = admitted();
            changed[field] = value;
            assert!(
                validate_review(&record, &changed, ROOT, TURN, false).is_err(),
                "{field}"
            );
        }
        for (field, value) in [
            ("state", json!("paused")),
            ("revision", json!(3)),
            ("id", json!(ROOT)),
        ] {
            let mut changed = admitted();
            changed["goal"][field] = value;
            assert!(validate_review(&record, &changed, ROOT, TURN, false).is_err());
        }
        assert!(validate_review(&record, &admitted(), ROOT, ROOT, false).is_err());
        assert!(validate_review(&record, &admitted(), ROOT, TURN, true).is_err());
        let mut pending = admitted();
        pending["open_dispatches"] = json!([{"state":"submitting"}]);
        assert!(validate_review(&record, &pending, ROOT, TURN, false).is_err());
        let ordinary = origin(&snapshot(), ROOT, TURN, None, false).unwrap();
        let mut changed = snapshot();
        changed["input_epoch"] = json!(4);
        assert!(
            validate_review(
                &json!({"root":ROOT,"origin":ordinary}),
                &changed,
                ROOT,
                TURN,
                false
            )
            .is_err()
        );
    }

    #[test]
    fn actor_and_operator_gates_are_explicit() {
        assert_eq!(owning_root(Some(ROOT), None, true).unwrap(), ROOT);
        assert!(owning_root(Some(ROOT), None, false).is_err());
        assert!(owning_root(Some(ROOT), Some(TURN), true).is_err());
        assert!(operator_allowed(Some(ROOT)).is_err());
        assert!(operator_allowed(Some("")).is_err());
        assert!(operator_allowed(None).is_ok());
        assert!(explicit_goal(Some(GOAL), None, Some(3)).is_err());
    }

    #[test]
    fn offline_operator_cancellation_requires_explicit_root_and_keeps_model_gate() {
        let temp = tempfile::tempdir().unwrap();
        let prepared = executions::prepare(temp.path(), ROOT,
            &json!({"root":ROOT,"turn":TURN,"input_epoch":3,"goal":null}),
            &json!({"schema":1,"topic":"offline cancellation","argv":["/bin/true"],"cwd":temp.path()})).unwrap();
        let id = prepared["id"].as_str().unwrap();
        let mut args = Arguments {
            parent: None,
            action: Action::Cancel { id: id.into() },
        };
        assert!(run_with_context(&args, temp.path(), temp.path(), None).is_err());
        args.parent = Some(ROOT.into());
        let (cancelled, code) = run_with_context(&args, temp.path(), temp.path(), None).unwrap();
        assert_eq!(code, 0);
        assert_eq!(cancelled["state"], "cancelled");
        assert_eq!(cancelled["result"]["workload_attempted"], false);
        assert!(!temp.path().join("codex-plus/background.sqlite").exists());
        assert!(run_with_context(&args, temp.path(), temp.path(), Some(ROOT)).is_err());
        args.action = Action::Launch {
            id: id.into(),
            review_digest: prepared["review_digest"].as_str().unwrap().into(),
            reason: None,
        };
        assert!(run_with_context(&args, temp.path(), temp.path(), None).is_err());
    }

    #[test]
    fn offline_operator_resolution_still_requires_verified_root_bound_cleanup() {
        let temp = tempfile::tempdir().unwrap();
        let mut prepared = executions::prepare(temp.path(), ROOT,
            &json!({"root":ROOT,"turn":TURN,"input_epoch":3,"goal":null}),
            &json!({"schema":1,"topic":"offline resolution","argv":["/bin/true"],"cwd":temp.path()})).unwrap();
        let id = prepared["id"].as_str().unwrap().to_owned();
        prepared["state"] = json!("unknown");
        local::atomic_json(
            &temp
                .path()
                .join("codex-plus/executions/v1")
                .join(ROOT)
                .join(&id)
                .join("state.json"),
            &prepared,
        )
        .unwrap();
        let mut proof = json!({"root":ROOT,"id":id,"cleanup_verified":false,"evidence":"fixture confirms no workload process exists"});
        let mut args = Arguments {
            parent: Some(ROOT.into()),
            action: Action::Resolve {
                id: id.clone(),
                evidence: proof.to_string(),
            },
        };
        assert!(run_with_context(&args, temp.path(), temp.path(), None).is_err());
        proof["cleanup_verified"] = json!(true);
        args.action = Action::Resolve {
            id: id.clone(),
            evidence: proof.to_string(),
        };
        let (resolved, code) = run_with_context(&args, temp.path(), temp.path(), None).unwrap();
        assert_eq!(code, 0);
        assert_eq!(resolved["resolution"], proof);
        assert_eq!(resolved["state"], "unknown");
        assert!(resolved["result"].is_null());
        assert!(!temp.path().join("codex-plus/background.sqlite").exists());
    }

    #[test]
    fn monitoring_failure_retains_execution_identity_and_never_rearms_cancelled_watch() {
        let temp = tempfile::tempdir().unwrap();
        let execution = executions::prepare(
            temp.path(),
            ROOT,
            &json!({"root":ROOT,"turn":TURN,"input_epoch":3,"goal":null}),
            &json!({"schema":1,"topic":"monitor failure","argv":["/bin/true"],"cwd":temp.path()}),
        )
        .unwrap();
        let id = execution["id"].as_str().unwrap();
        let receipt = monitoring_failure(
            temp.path(),
            ROOT,
            id,
            execution.clone(),
            &anyhow::anyhow!("observer failed"),
        );
        assert_eq!(receipt["id"], id);
        assert_eq!(receipt["execution"]["state"], "prepared");
        assert_eq!(
            executions::read_observation(temp.path(), ROOT, id).unwrap()["state"],
            "recovery_needed"
        );
        executions::mark_observation(
            temp.path(),
            ROOT,
            id,
            "cancelled",
            &json!({"source":"test"}),
        )
        .unwrap();
        let receipt = monitoring_failure(
            temp.path(),
            ROOT,
            id,
            execution.clone(),
            &anyhow::anyhow!("cancelled"),
        );
        assert_eq!(receipt["monitoring"]["state"], "cancelled");
        assert_eq!(
            executions::read_observation(temp.path(), ROOT, id).unwrap()["state"],
            "cancelled"
        );
        assert!(
            !temp
                .path()
                .join("codex-plus/executions/v1")
                .join(ROOT)
                .join(id)
                .join("stdout.log")
                .exists()
        );
    }

    #[test]
    fn cli_requires_review_digest_and_accepts_explicit_preparation_tuple() {
        assert!(Arguments::try_parse_from(["codex-plus job", "launch", "--id", ROOT]).is_err());
        assert!(
            Arguments::try_parse_from([
                "codex-plus job",
                "prepare",
                "--manifest",
                "/tmp/job.json",
                "--turn",
                TURN,
                "--goal",
                GOAL,
                "--revision",
                "2",
                "--input-epoch",
                "3"
            ])
            .is_ok()
        );
        assert!(Arguments::try_parse_from(["codex-plus job", "maintenance", "leave"]).is_ok());
        assert!(matches!(
            Arguments::try_parse_from(["codex-plus job", "list"])
                .unwrap()
                .action,
            Action::List { all: false }
        ));
        assert!(matches!(
            Arguments::try_parse_from(["codex-plus job", "list", "--all"])
                .unwrap()
                .action,
            Action::List { all: true }
        ));
    }

    fn seed_inventory_unknown(home: &Path, root: &str, package: &str) -> Value {
        let mut record = executions::prepare(
            home,
            root,
            &json!({"root":root,"turn":TURN,"input_epoch":3,"goal":null}),
            &json!({"schema":1,"topic":"inventory fixture","argv":["/bin/true"],"cwd":home}),
        )
        .unwrap();
        record["state"] = json!("unknown");
        record["package"] = json!(package);
        record["executor"] =
            json!({"boot_id":"old-boot","pid":u32::MAX,"start_ticks":1,"pgid":1,"sid":1});
        local::atomic_json(&inventory_state(home, &record), &record).unwrap();
        record
    }

    fn inventory_state(home: &Path, record: &Value) -> PathBuf {
        home.join("codex-plus/executions/v1")
            .join(record["root"].as_str().unwrap())
            .join(record["id"].as_str().unwrap())
            .join("state.json")
    }

    #[test]
    fn all_root_inventory_is_operator_only_and_missing_home_stays_missing() {
        let temp = tempfile::tempdir().unwrap();
        let home = temp.path().join("absent-home");
        let args = Arguments {
            parent: None,
            action: Action::List { all: true },
        };
        assert!(run_with_context(&args, temp.path(), &home, Some(ROOT)).is_err());
        let (result, code) = run_with_context(&args, temp.path(), &home, None).unwrap();
        assert_eq!(code, 0);
        assert_eq!(
            result,
            json!({"schema":1,"execution_record_format":1,"unresolved":[],"packages":[]})
        );
        assert!(!home.exists());
    }

    #[test]
    fn inventory_keeps_all_dead_executor_pins_deduplicates_and_uses_quiet_policy() {
        let temp = tempfile::tempdir().unwrap();
        let mut last = Value::Null;
        for index in 0..68 {
            let root = if index % 2 == 0 { ROOT } else { TURN };
            last = seed_inventory_unknown(
                temp.path(),
                root,
                &format!("/packages/release-{}", index % 66),
            );
        }
        let mut resolved = seed_inventory_unknown(temp.path(), ROOT, "/packages/resolved");
        resolved["resolution"] = json!({"root":ROOT,"id":resolved["id"],"cleanup_verified":true,"evidence":"cleanup verified"});
        local::atomic_json(&inventory_state(temp.path(), &resolved), &resolved).unwrap();
        let quiet = executions::prepare(
            temp.path(),
            ROOT,
            &json!({"root":ROOT}),
            &json!({"schema":1,"topic":"quiet prepared","argv":["/bin/true"],"cwd":temp.path()}),
        )
        .unwrap();
        executions::cancel(temp.path(), ROOT, quiet["id"].as_str().unwrap()).unwrap();
        let before = std::fs::read(inventory_state(temp.path(), &last)).unwrap();
        let result = inventory(temp.path()).unwrap();
        assert_eq!(result["unresolved"].as_array().unwrap().len(), 68);
        assert_eq!(result["packages"].as_array().unwrap().len(), 66);
        assert!(
            result["packages"]
                .as_array()
                .unwrap()
                .contains(&json!("/packages/release-65"))
        );
        assert!(
            !result["packages"]
                .as_array()
                .unwrap()
                .contains(&json!("/packages/resolved"))
        );
        assert_eq!(
            std::fs::read(inventory_state(temp.path(), &last)).unwrap(),
            before
        );
        assert!(!temp.path().join("codex-plus/background.sqlite").exists());
        let args = Arguments {
            parent: Some(ROOT.into()),
            action: Action::List { all: false },
        };
        let (per_root, _) = run_with_context(&args, temp.path(), temp.path(), None).unwrap();
        assert!(per_root.is_array());
        assert!(
            per_root
                .as_array()
                .unwrap()
                .iter()
                .all(|record| record["root"] == ROOT)
        );
    }

    #[test]
    fn inventory_refuses_corrupt_missing_or_invalid_package_evidence() {
        let temp = tempfile::tempdir().unwrap();
        let mut record = seed_inventory_unknown(temp.path(), ROOT, "relative/package");
        let path = inventory_state(temp.path(), &record);
        assert!(inventory(temp.path()).is_err());
        record["package"] = json!("/packages/retained");
        local::atomic_json(&path, &record).unwrap();
        assert!(inventory(temp.path()).is_ok());
        std::fs::write(&path, b"{broken").unwrap();
        assert!(inventory(temp.path()).is_err());
        assert_eq!(std::fs::read(&path).unwrap(), b"{broken");
        std::fs::remove_file(&path).unwrap();
        assert!(inventory(temp.path()).is_err());
        assert!(!path.exists());
        assert!(!temp.path().join("codex-plus/background.sqlite").exists());
    }
}
