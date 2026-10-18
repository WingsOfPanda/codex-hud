//! Fail-closed admission for detached work in the qualified unrestricted Linux contexts.
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{
    fs::{self, OpenOptions},
    os::{
        fd::AsRawFd,
        unix::fs::{FileExt, MetadataExt, OpenOptionsExt},
    },
    path::{Component, Path},
    time::Duration,
};

const HEADER_BYTES: usize = 64 * 1024;
const TAIL_BYTES: u64 = 2 * 1024 * 1024;
const NAMESPACES: [&str; 5] = ["pid", "mnt", "net", "user", "cgroup"];

fn extent(info: &fs::Metadata) -> (u64, u64, u64, i64, i64, i64, i64) {
    (
        info.dev(),
        info.ino(),
        info.len(),
        info.mtime(),
        info.mtime_nsec(),
        info.ctime(),
        info.ctime_nsec(),
    )
}

fn unrestricted(context: &Value) -> Result<()> {
    ensure!(
        context["permission_profile"] == json!({"type":"disabled"})
            && context["sandbox_policy"] == json!({"type":"danger-full-access"}),
        "detached execution requires canonical disabled permissions and danger-full-access"
    );
    Ok(())
}

fn context_records(bytes: &[u8], turn: &str) -> Result<Value> {
    ensure!(bytes.ends_with(b"\n"), "incomplete admission evidence");
    let mut context = None;
    for line in bytes
        .split(|byte| *byte == b'\n')
        .filter(|line| !line.is_empty())
    {
        let record: Value = serde_json::from_slice(line).context("malformed admission evidence")?;
        ensure!(record.is_object(), "malformed admission record");
        let payload = &record["payload"];
        if record["type"] == "compacted"
            || (record["type"] == "event_msg" && payload["type"] == "thread_rolled_back")
        {
            context = None;
        }
        if record["type"] == "turn_context" {
            ensure!(
                payload.is_object() && payload["turn_id"].is_string(),
                "malformed turn context"
            );
            if payload["turn_id"] == turn {
                unrestricted(payload)?;
                if let Some(previous) = &context {
                    ensure!(previous == payload, "conflicting originating turn contexts");
                }
                context = Some(payload.clone());
            } else {
                context = None;
            }
        }
    }
    context.context("exact originating turn context missing from bounded evidence")
}

fn read_context(home: &Path, path: &Path, root: &str, turn: &str) -> Result<Value> {
    let home = home.canonicalize()?;
    let candidate = if path.is_absolute() {
        path.to_owned()
    } else {
        home.join(path)
    };
    let relative = candidate
        .strip_prefix(&home)
        .context("rollout is outside CODEX_HOME")?;
    let parts: Vec<_> = relative.components().collect();
    ensure!(
        parts.len() >= 2
            && parts
                .iter()
                .all(|part| matches!(part, Component::Normal(_)))
            && parts[0].as_os_str() == "sessions",
        "invalid live rollout path"
    );
    let flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK;
    let mut directory = OpenOptions::new()
        .read(true)
        .custom_flags(flags | libc::O_DIRECTORY)
        .open(&home)?;
    for part in &parts[..parts.len() - 1] {
        directory = crate::local::open_at(
            directory.as_raw_fd(),
            part.as_os_str(),
            flags | libc::O_DIRECTORY,
            0,
        )?;
    }
    let file = crate::local::open_at(
        directory.as_raw_fd(),
        parts.last().unwrap().as_os_str(),
        flags,
        0,
    )?;
    let before = file.metadata()?;
    ensure!(before.is_file(), "rollout is not a regular file");
    let mut header = vec![0; HEADER_BYTES];
    let count = file.read_at(&mut header, 0)?;
    let newline = header[..count]
        .iter()
        .position(|b| *b == b'\n')
        .context("missing session metadata")?;
    let meta: Value = serde_json::from_slice(&header[..newline])?;
    ensure!(
        meta["type"] == "session_meta" && meta["payload"]["id"] == root,
        "rollout root identity mismatch"
    );
    let start = before.len().saturating_sub(TAIL_BYTES);
    let offset = start.saturating_sub(1);
    let mut tail = vec![0; (before.len() - offset) as usize];
    ensure!(
        file.read_at(&mut tail, offset)? == tail.len(),
        "short admission evidence read"
    );
    let tail = if start > 0 {
        let newline = tail
            .iter()
            .position(|b| *b == b'\n')
            .context("no complete bounded evidence")?;
        &tail[newline + 1..]
    } else {
        &tail[..]
    };
    let result = context_records(tail, turn)?;
    let reopened = crate::local::open_at(
        directory.as_raw_fd(),
        parts.last().unwrap().as_os_str(),
        flags,
        0,
    )?;
    ensure!(
        extent(&file.metadata()?) == extent(&before)
            && extent(&reopened.metadata()?) == extent(&before)
            && extent(&fs::symlink_metadata(&candidate)?) == extent(&before),
        "rollout changed during admission"
    );
    Ok(result)
}

fn status_fields(text: &str) -> Result<Value> {
    let mut fields = serde_json::Map::new();
    for line in text.lines() {
        let (name, value) = line.split_once(':').context("malformed process status")?;
        if matches!(
            name,
            "Uid" | "Gid" | "NSpid" | "NoNewPrivs" | "Seccomp" | "Seccomp_filters"
        ) {
            ensure!(!fields.contains_key(name), "duplicate process status field");
            fields.insert(
                name.to_owned(),
                json!(value.split_whitespace().collect::<Vec<_>>().join(" ")),
            );
        }
    }
    Ok(Value::Object(fields))
}

fn initial_map(text: &str) -> bool {
    text.split_whitespace().collect::<Vec<_>>() == ["0", "0", "4294967295"]
}

fn qualified_cgroup(text: &str, uid: u32) -> bool {
    let lines: Vec<_> = text.lines().collect();
    if lines.len() != 1 {
        return false;
    }
    let prefix = format!("0::/user.slice/user-{uid}.slice/");
    let Some(rest) = lines[0].strip_prefix(&prefix) else {
        return false;
    };
    if let Some(session) = rest
        .strip_prefix("session-")
        .and_then(|s| s.strip_suffix(".scope"))
    {
        return !session.is_empty() && session.bytes().all(|b| b.is_ascii_digit());
    }
    let prefix = format!("user@{uid}.service/tmux-spawn-");
    rest.strip_prefix(&prefix)
        .and_then(|s| s.strip_suffix(".scope"))
        .is_some_and(|id| uuid::Uuid::parse_str(id).is_ok())
}

fn process_identity(pid: u32) -> Result<(u32, String)> {
    let stat = fs::read_to_string(format!("/proc/{pid}/stat"))?;
    let fields: Vec<_> = stat
        .rsplit_once(')')
        .context("invalid process stat")?
        .1
        .split_whitespace()
        .collect();
    let parent = fields.get(1).context("missing process parent")?.parse()?;
    let start = fields.get(19).context("missing process start time")?;
    ensure!(start.parse::<u64>().is_ok(), "invalid process start time");
    Ok((parent, (*start).to_owned()))
}

fn process_facts(pid: u32) -> Result<Value> {
    let base = format!("/proc/{pid}");
    let (_, start) = process_identity(pid)?;
    let mut namespaces = serde_json::Map::new();
    for name in NAMESPACES {
        let target = fs::read_link(format!("{base}/ns/{name}"))?;
        let target = target.to_str().context("invalid namespace identity")?;
        let prefix = format!("{name}:[");
        ensure!(
            target
                .strip_prefix(&prefix)
                .and_then(|s| s.strip_suffix(']'))
                .is_some_and(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())),
            "invalid namespace identity"
        );
        namespaces.insert(name.into(), target.into());
    }
    let result = json!({"pid":pid,"start_ticks":start,
        "boot_id":fs::read_to_string("/proc/sys/kernel/random/boot_id")?.trim(),
        "namespaces":namespaces,"cgroup":fs::read_to_string(format!("{base}/cgroup"))?,
        "status":status_fields(&fs::read_to_string(format!("{base}/status"))?)?,
        "uid_map":fs::read_to_string(format!("{base}/uid_map"))?,
        "gid_map":fs::read_to_string(format!("{base}/gid_map"))?,
        "lsm":fs::read_to_string(format!("{base}/attr/current"))?.trim()});
    ensure!(
        process_identity(pid)?.1 == start,
        "process identity changed during admission"
    );
    Ok(result)
}

fn qualified_process(facts: &Value, uid: u32) -> Result<()> {
    let status = &facts["status"];
    let uids = format!("{uid} {uid} {uid} {uid}");
    ensure!(
        status["Uid"] == uids,
        "detached admission requires same-uid processes"
    );
    ensure!(
        status["NSpid"]
            == facts["pid"]
                .as_u64()
                .context("missing process pid")?
                .to_string(),
        "nested PID namespace refused"
    );
    ensure!(
        status["NoNewPrivs"] == "0" && status["Seccomp"] == "0" && status["Seccomp_filters"] == "0",
        "unqualified seccomp or no-new-privileges containment"
    );
    ensure!(
        facts["uid_map"].as_str().is_some_and(initial_map)
            && facts["gid_map"].as_str().is_some_and(initial_map),
        "unqualified user namespace mapping"
    );
    ensure!(facts["lsm"] == "unconfined", "unqualified LSM confinement");
    ensure!(
        facts["cgroup"]
            .as_str()
            .is_some_and(|text| qualified_cgroup(text, uid)),
        "detached execution requires a qualified user session or tmux scope; daemon service/container lifetime is unknown"
    );
    Ok(())
}

fn parent_death_signal() -> Result<i32> {
    let mut signal = 0;
    ensure!(
        unsafe { libc::prctl(libc::PR_GET_PDEATHSIG, &mut signal) } == 0,
        "could not inspect parent-death signal"
    );
    Ok(signal)
}

fn containment(generation: &str) -> Result<Value> {
    ensure!(
        cfg!(target_os = "linux"),
        "detached admission is qualified only on Linux"
    );
    let identity: Vec<_> = generation.split(':').collect();
    ensure!(identity.len() == 3, "invalid daemon generation");
    let daemon: u32 = identity[1].parse()?;
    let own = std::process::id();
    let uid = unsafe { libc::geteuid() };
    let mut caller = process_facts(own)?;
    let peer = process_facts(daemon)?;
    qualified_process(&caller, uid)?;
    qualified_process(&peer, uid)?;
    ensure!(
        peer["boot_id"] == identity[0] && peer["start_ticks"] == identity[2],
        "daemon generation changed"
    );
    ensure!(
        caller["namespaces"] == peer["namespaces"] && caller["cgroup"] == peer["cgroup"],
        "caller and daemon containment differ"
    );
    let mut current = own;
    let mut reached = false;
    for _ in 0..64 {
        let cmdline = fs::read(format!("/proc/{current}/cmdline"))?;
        for arg in cmdline.split(|b| *b == 0) {
            let arg = String::from_utf8_lossy(arg);
            let name = Path::new(arg.as_ref())
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("");
            ensure!(
                !matches!(name, "bwrap" | "bubblewrap" | "codex-linux-sandbox")
                    && arg != "linux-sandbox",
                "sandbox ancestry refused"
            );
        }
        if current == daemon {
            reached = true;
            break;
        }
        let parent = process_identity(current)?.0;
        ensure!(
            parent > 0 && parent != current,
            "daemon is not caller's ancestor"
        );
        current = parent;
    }
    ensure!(reached, "daemon ancestry was not positively established");
    let signal = parent_death_signal()?;
    ensure!(
        matches!(signal, 0 | libc::SIGTERM),
        "unqualified parent-death signal"
    );
    caller["pdeathsig"] = json!(signal);
    caller["qualification"] = json!("linux-initial-user-session-or-tmux-scope-v1");
    Ok(caller)
}

fn root_turn(client: &mut crate::rpc::Client, root: &str, turn: &str) -> Result<Value> {
    let response = client.call("thread/read", json!({"threadId":root,"includeTurns":false}))?;
    let meta = &response["thread"];
    ensure!(
        meta["id"] == root
            && meta.get("parentThreadId").is_some_and(Value::is_null)
            && matches!(
                meta["source"].as_str(),
                Some("cli" | "vscode" | "exec" | "appServer")
            ),
        "detached execution requires a recognized root thread"
    );
    let environments = meta["environments"]
        .as_array()
        .context("local environment evidence missing")?;
    ensure!(
        environments.len() == 1 && environments[0]["environmentId"] == "local",
        "unqualified execution environment"
    );
    let turns = client.call(
        "thread/turns/list",
        json!({"threadId":root,"limit":1,"sortDirection":"desc","itemsView":"notLoaded"}),
    )?;
    let current = turns["data"]
        .as_array()
        .and_then(|rows| rows.first())
        .context("active originating turn missing")?;
    ensure!(
        current["id"] == turn && current["status"] == "inProgress",
        "originating turn is no longer active"
    );
    Ok(
        json!({"id":meta["id"],"parentThreadId":meta["parentThreadId"],
        "source":meta["source"],"environments":meta["environments"],
        "path":meta["path"],"cwd":meta["cwd"]}),
    )
}

/// Review/goal/maintenance/resource authorization is owned by the launch transaction.
/// This is a cooperative model-workflow gate, not hostile same-UID attestation.
pub fn authorize(home: &Path, root: &str, expected_turn: &str) -> Result<Value> {
    ensure!(
        !root.is_empty() && !expected_turn.is_empty(),
        "root and originating turn are required"
    );
    ensure!(
        std::env::var("CODEX_THREAD_ID").ok().as_deref() == Some(root),
        "actor thread differs from reviewed root"
    );
    let socket = home.join("app-server-control/app-server-control.sock");
    let mut client = crate::rpc::Client::connect(&socket, Duration::from_secs(5))?;
    let meta = root_turn(&mut client, root, expected_turn)?;
    let path = Path::new(meta["path"].as_str().context("live rollout path missing")?);
    let context = read_context(home, path, root, expected_turn)?;
    let facts = containment(&client.generation)?;
    let mut fresh = crate::rpc::Client::connect(&socket, Duration::from_secs(5))?;
    ensure!(
        fresh.generation == client.generation,
        "daemon generation changed during admission"
    );
    ensure!(
        root_turn(&mut fresh, root, expected_turn)? == meta,
        "root metadata changed during admission"
    );
    Ok(
        json!({"root":root,"turn":expected_turn,"daemon_generation":client.generation,
        "permission_context":context,"containment":facts}),
    )
}

/// Check the forked executor inherited the admitted context without escaping it.
pub fn verify_executor_environment(evidence: &Value) -> Result<()> {
    let current = process_facts(std::process::id())?;
    qualified_process(&current, unsafe { libc::geteuid() })?;
    let admitted = &evidence["containment"];
    for field in [
        "boot_id",
        "namespaces",
        "cgroup",
        "uid_map",
        "gid_map",
        "lsm",
    ] {
        ensure!(
            current[field] == admitted[field],
            "executor {field} differs from admitted context"
        );
    }
    ensure!(
        parent_death_signal()? == 0,
        "executor retains a parent-death signal"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn context(turn: &str) -> Value {
        json!({"turn_id":turn,"permission_profile":{"type":"disabled"},"sandbox_policy":{"type":"danger-full-access"}})
    }
    fn line(payload: Value) -> String {
        format!("{}\n", json!({"type":"turn_context","payload":payload}))
    }

    #[test]
    fn permissions_require_canonical_disabled_not_a_legacy_projection_or_label() {
        assert!(unrestricted(&context("t")).is_ok());
        for profile in [
            json!({"type":"managed","file_system":{"type":"unrestricted"},"network":"enabled"}),
            json!({"type":"external","network":"enabled"}),
            Value::Null,
            json!({"type":"unknown"}),
        ] {
            let mut value = context("t");
            value["permission_profile"] = profile;
            assert!(unrestricted(&value).is_err());
        }
        assert!(
            unrestricted(&json!({"active_permission_profile":{"id":":danger-full-access"}}))
                .is_err()
        );
    }

    #[test]
    fn contexts_reject_stale_malformed_partial_conflicting_and_reset_evidence() {
        let good = line(context("t"));
        assert!(context_records(good.as_bytes(), "t").is_ok());
        for bad in [
            good.trim_end().to_owned(),
            format!("{good}broken\n"),
            line(context("other")),
            format!("{good}{}", line(context("other"))),
            format!("{good}{{\"type\":\"compacted\"}}\n"),
        ] {
            assert!(context_records(bad.as_bytes(), "t").is_err(), "{bad}");
        }
        let mut changed = context("t");
        changed["cwd"] = json!("/changed");
        assert!(context_records(format!("{good}{}", line(changed)).as_bytes(), "t").is_err());
    }

    #[test]
    fn cgroups_only_allow_qualified_user_sessions_or_tmux_scopes() {
        assert!(qualified_cgroup(
            "0::/user.slice/user-999.slice/session-4327.scope\n",
            999
        ));
        assert!(qualified_cgroup(
            "0::/user.slice/user-999.slice/user@999.service/tmux-spawn-8027572a-7aec-43a3-886a-ab37025a83e0.scope\n",
            999
        ));
        for text in [
            "0::/\n",
            "0::/docker/abc\n",
            "0::/user.slice/user-999.slice/user@999.service/codex.service\n",
            "0::/user.slice/user-998.slice/session-1.scope\n",
            "0::/user.slice/user-999.slice/session-1.scope/child\n",
            "0::/user.slice/user-999.slice/session-1.scope\n1:cpu:/\n",
        ] {
            assert!(!qualified_cgroup(text, 999), "{text}");
        }
        assert!(initial_map("0 0 4294967295\n"));
        assert!(!initial_map("0 100000 65536\n"));
    }

    #[test]
    fn security_baseline_refuses_each_restricted_or_unknown_fact() {
        let good = json!({"pid":123,"status":{"Uid":"999 999 999 999","NSpid":"123",
            "NoNewPrivs":"0","Seccomp":"0","Seccomp_filters":"0"},
            "uid_map":"0 0 4294967295","gid_map":"0 0 4294967295",
            "lsm":"unconfined","cgroup":"0::/user.slice/user-999.slice/session-4327.scope\n"});
        assert!(qualified_process(&good, 999).is_ok());
        for (field, value) in [
            ("Uid", "998 998 998 998"),
            ("NSpid", "123 2"),
            ("NoNewPrivs", "1"),
            ("Seccomp", "2"),
            ("Seccomp_filters", "1"),
        ] {
            let mut bad = good.clone();
            bad["status"][field] = json!(value);
            assert!(qualified_process(&bad, 999).is_err(), "{field}");
            bad["status"].as_object_mut().unwrap().remove(field);
            assert!(qualified_process(&bad, 999).is_err());
        }
        for field in ["uid_map", "gid_map", "lsm", "cgroup"] {
            let mut bad = good.clone();
            bad[field] = json!("unknown");
            assert!(qualified_process(&bad, 999).is_err(), "{field}");
        }
        assert!(status_fields("Uid: 1 1 1 1\nUid: 1 1 1 1\n").is_err());
    }

    #[test]
    fn bounded_reader_checks_root_and_refuses_symlink_paths() {
        let home = tempfile::tempdir().unwrap();
        let directory = home.path().join("sessions");
        fs::create_dir(&directory).unwrap();
        let file = directory.join("rollout.jsonl");
        fs::write(
            &file,
            format!(
                "{}\n{}",
                json!({"type":"session_meta","payload":{"id":"root"}}),
                line(context("t"))
            ),
        )
        .unwrap();
        assert!(read_context(home.path(), &file, "root", "t").is_ok());
        assert!(read_context(home.path(), &file, "other", "t").is_err());
        let link = directory.join("link.jsonl");
        std::os::unix::fs::symlink(&file, &link).unwrap();
        assert!(read_context(home.path(), &link, "root", "t").is_err());
        let linked_directory = home.path().join("sessions/alias");
        std::os::unix::fs::symlink(&directory, &linked_directory).unwrap();
        assert!(
            read_context(
                home.path(),
                &linked_directory.join("rollout.jsonl"),
                "root",
                "t"
            )
            .is_err()
        );
        assert!(
            read_context(
                home.path(),
                Path::new("sessions/../sessions/rollout.jsonl"),
                "root",
                "t"
            )
            .is_err()
        );
        assert!(
            read_context(
                home.path(),
                Path::new("/outside/rollout.jsonl"),
                "root",
                "t"
            )
            .is_err()
        );
        fs::write(
            &file,
            format!(
                "{}\n{}",
                json!({"type":"session_meta","payload":{"id":"root"}}),
                " ".repeat(TAIL_BYTES as usize + 1)
            ),
        )
        .unwrap();
        assert!(read_context(home.path(), &file, "root", "t").is_err());
    }
}
