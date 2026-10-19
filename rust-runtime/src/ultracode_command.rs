//! Root-only workflow controls; effective mode follows positive turn admission evidence.
use crate::{background, goal_command::GoalLock, ledger::Store, local::state_home, rpc::Client};
use anyhow::{Context, Result, bail, ensure};
use clap::Parser;
use serde_json::{Value, json};
use std::{ffi::OsString, fs, io::Read, os::unix::fs::PermissionsExt, path::Path, time::Duration};
use uuid::Uuid;

#[derive(Parser)]
#[command(name = "codex-plus ultracode")]
struct Arguments {
    #[arg(value_parser = ["ultracode"])]
    command: String,
    #[arg(value_parser = ["status", "on", "off", "provenance", "load-instructions"])]
    action: String,
    #[arg(long)]
    thread: Option<String>,
    #[arg(long, value_parser = ["fresh", "cold-resume"])]
    load_kind: Option<String>,
    #[arg(long)]
    developer_instructions_stdin: bool,
    #[arg(long)]
    json: bool,
}

fn connect(home: &Path) -> Result<Client> {
    Client::connect(
        &home.join("app-server-control/app-server-control.sock"),
        Duration::from_secs(5),
    )
}

fn initial_submission<'a>(items: &'a [Value], turn: &str) -> Result<Option<&'a str>> {
    ensure!(
        items.iter().all(|item| item["turnId"] == turn),
        "turn item ownership mismatch"
    );
    Ok(items
        .iter()
        .find(|item| item["item"]["type"] == "userMessage")
        .and_then(|item| item["item"]["clientId"].as_str()))
}

/// Settle from the initial user item, never from a later steer or requested state.
pub(crate) fn reconcile_turn(
    store: &mut Store,
    rpc: &mut Client,
    root: &str,
    turn: &str,
) -> Result<()> {
    if store.ultracode_status(root, Some(&rpc.generation), Some(turn))?["effective"].is_object() {
        return Ok(());
    }
    let mut retries = 0;
    let items = loop {
        match background::pages(
            rpc,
            "thread/items/list",
            json!({"threadId":root,"turnId":turn,"sortDirection":"asc"}),
        ) {
            Ok(items) => {
                initial_submission(&items, turn)?;
                if items
                    .iter()
                    .any(|item| item["item"]["type"] == "userMessage")
                    || retries == 3
                {
                    break items;
                }
            }
            Err(error) => {
                let temporary = error
                    .downcast_ref::<crate::rpc::RpcError>()
                    .is_some_and(|rpc| {
                        rpc.method == "thread/items/list"
                            && rpc.error["code"].as_i64() == Some(-32601)
                            && rpc.error["message"].as_str()
                                == Some("list_items is not supported yet")
                    });
                if !temporary || retries == 3 {
                    return Err(error);
                }
            }
        }
        retries += 1;
        std::thread::sleep(Duration::from_millis(100));
    };
    let submission =
        initial_submission(&items, turn)?.context("initial user submission is not yet visible")?;
    store.ultracode_bind(root, submission, &rpc.generation, turn)?;
    let status = store.ultracode_status(root, Some(&rpc.generation), Some(turn))?;
    publish(store, root, &status)?;
    Ok(())
}

/// Missing mode qualification must not change existing goal or callback delivery.
pub(crate) fn reserve_optional(store: &mut Store, root: &str, submission: &str, generation: &str) {
    if !store
        .ultracode_status(root, Some(generation), None)
        .ok()
        .is_some_and(|status| status["load"]["generation"] == generation)
    {
        return;
    }
    if let Err(error) = store.ultracode_reserve(root, submission, generation) {
        eprintln!("codex-plus: Ultracode reservation unavailable: {error:#}");
    }
}

pub(crate) fn input_optional(
    store: &mut Store,
    rpc: &mut Client,
    root: &str,
    submission: &str,
    turn: Option<&str>,
    outcome: &str,
) {
    if !store
        .ultracode_status(root, Some(&rpc.generation), None)
        .ok()
        .is_some_and(|status| status["load"]["generation"] == rpc.generation)
    {
        return;
    }
    let result = if outcome == "accepted" {
        turn.context("accepted input has no turn").and_then(|turn| {
            reconcile_turn(store, rpc, root, turn)?;
            store.ultracode_bind(root, submission, &rpc.generation, turn)?;
            let status = store.ultracode_status(root, Some(&rpc.generation), Some(turn))?;
            publish(store, root, &status)
        })
    } else {
        store
            .ultracode_settle(root, submission, &rpc.generation, outcome, outcome)
            .map(|_| ())
    };
    if let Err(error) = result {
        eprintln!("codex-plus: Ultracode input snapshot unavailable: {error:#}");
        if outcome == "accepted" {
            let projection = store
                .ultracode_status(root, Some(&rpc.generation), turn)
                .and_then(|mut status| {
                    admission_unavailable(&mut status, &error);
                    publish(store, root, &status)
                });
            if let Err(error) = projection {
                eprintln!("codex-plus: Ultracode status projection unavailable: {error:#}");
            }
        }
    }
}

fn admission_unavailable(status: &mut Value, error: &anyhow::Error) {
    if !status["effective"].is_object() {
        status["availability"] = json!("unavailable");
        status["unavailable_reason"] = json!(format!(
            "Exact turn admission could not be established: {error:#}; no replay."
        ));
    }
}

pub(crate) fn publish(store: &mut Store, root: &str, status: &Value) -> Result<()> {
    Uuid::parse_str(root)?;
    let directory = store.directory.join("ultracode");
    match fs::create_dir(&directory) {
        Ok(()) => (),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => (),
        Err(error) => return Err(error.into()),
    }
    let meta = fs::symlink_metadata(&directory)?;
    ensure!(
        meta.is_dir() && !meta.file_type().is_symlink(),
        "Ultracode projection requires a real directory"
    );
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))?;
    crate::local::atomic_json(&directory.join(format!("{root}.json")), status)
}

fn execute(args: &Arguments) -> Result<Value> {
    let own = std::env::var("CODEX_THREAD_ID").ok();
    let root = Uuid::parse_str(
        args.thread
            .as_deref()
            .or(own.as_deref())
            .context("set CODEX_THREAD_ID or pass --thread")?,
    )?
    .to_string();
    if let Some(own) = &own {
        ensure!(
            Uuid::parse_str(own)?.to_string() == root,
            "cannot inspect another root"
        );
        ensure!(
            args.action == "status",
            "Ultracode settings require an explicit host action"
        );
    }
    let developer_instructions = if args.developer_instructions_stdin {
        ensure!(
            args.action == "provenance",
            "developer instruction input is only valid for provenance"
        );
        let mut bytes = Vec::new();
        std::io::stdin()
            .take(1024 * 1024 + 1)
            .read_to_end(&mut bytes)?;
        ensure!(
            bytes.len() <= 1024 * 1024,
            "developer instructions exceed 1 MiB"
        );
        let text = String::from_utf8(bytes).context("developer instructions must be UTF-8")?;
        ensure!(
            text.contains(crate::ultracode::BOOTSTRAP),
            "complete fixed bootstrap is required"
        );
        Some(text)
    } else {
        None
    };
    let home = state_home();
    let mut store = Store::open(&home)?;
    if args.action == "load-instructions" {
        return Ok(
            json!({"root":root,"developer_instructions":store.ultracode_load_instructions(&root)?}),
        );
    }
    let mut rpc = match connect(&home) {
        Ok(rpc) => rpc,
        Err(error) if args.action == "status" => {
            let mut status = store.ultracode_status(&root, None, None)?;
            status["unavailable_reason"] =
                json!(format!("Local daemon unavailable: {error:#}; no replay."));
            return Ok(status);
        }
        Err(error) => return Err(error),
    };
    let meta = match rpc.call("thread/read", json!({"threadId":root,"includeTurns":false})) {
        Ok(meta) => meta,
        Err(error) if args.action == "status" => {
            let mut status = store.ultracode_status(&root, None, None)?;
            status["unavailable_reason"] = json!(format!(
                "Root daemon evidence unavailable: {error:#}; no replay."
            ));
            return Ok(status);
        }
        Err(error) => return Err(error),
    };
    ensure!(
        crate::goal_command::root(&meta["thread"], &root),
        "Ultracode requires a local interactive root"
    );
    let _lock = GoalLock::acquire(&store.directory, &root)?;
    match args.action.as_str() {
        "on" | "off" => {
            store.ultracode_request(&root, args.action == "on")?;
        }
        "provenance" => {
            ensure!(
                meta["thread"]["status"]["type"] != "notLoaded",
                "record bootstrap provenance only after a controlled load"
            );
            let kind = args
                .load_kind
                .as_deref()
                .context("--load-kind is required")?;
            if kind == "fresh" {
                match rpc.call("thread/turns/list", json!({"threadId":root,"limit":1,"sortDirection":"desc","itemsView":"notLoaded"})) {
                    Ok(turns) => ensure!(turns["data"].as_array().is_some_and(Vec::is_empty), "fresh bootstrap provenance requires a root with no prior turns"),
                    Err(error) => {
                        let expected = format!("thread {root} is not materialized yet; thread/turns/list is unavailable before first user message");
                        ensure!(error.downcast_ref::<crate::rpc::RpcError>().is_some_and(|error|
                            error.error["message"] == expected), "fresh bootstrap no-turn evidence unavailable");
                    }
                }
            }
            store.ultracode_record_load(
                &root,
                &rpc.generation,
                kind,
                developer_instructions.as_deref(),
            )?;
        }
        "status" => {}
        _ => bail!("unsupported Ultracode action"),
    }
    let current_result = background::goal_current_turn(&mut rpc, &root);
    let current_error = current_result
        .as_ref()
        .err()
        .map(|error| format!("Current turn unavailable: {error:#}; no replay."));
    let current = current_result.ok();
    let turn = current
        .as_ref()
        .filter(|turn| turn["status"] == "inProgress")
        .and_then(|turn| turn["id"].as_str());
    let error = turn.and_then(|turn| reconcile_turn(&mut store, &mut rpc, &root, turn).err());
    let mut status = store.ultracode_status(&root, Some(&rpc.generation), turn)?;
    if let Some(error) = error {
        admission_unavailable(&mut status, &error);
    } else if let Some(error) = current_error {
        status["unavailable_reason"] = json!(error);
    } else if turn.is_none() {
        status["unavailable_reason"] =
            json!("There is no active root turn; no effective turn snapshot.");
    }
    if status["availability"] == "unavailable" && status.get("unavailable_reason").is_none() {
        status["unavailable_reason"] = json!(
            "A fixed Ultracode bootstrap is not qualified for this root and daemon generation; no replay."
        );
    }
    publish(&mut store, &root, &status)?;
    Ok(status)
}

pub fn main(argv: &[OsString]) -> i32 {
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
    match execute(&args) {
        Ok(status) => {
            println!("{status}");
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
    fn earliest_user_item_wins_and_an_unknown_initial_item_cannot_be_skipped() {
        let mut items = vec![
            json!({"turnId":"turn","item":{"type":"userMessage","clientId":"initial"}}),
            json!({"turnId":"turn","item":{"type":"userMessage","clientId":"steer"}}),
        ];
        assert_eq!(initial_submission(&items, "turn").unwrap(), Some("initial"));
        items[0]["item"]["clientId"] = Value::Null;
        assert_eq!(initial_submission(&items, "turn").unwrap(), None);
        assert!(initial_submission(&items, "foreign").is_err());
    }
}
