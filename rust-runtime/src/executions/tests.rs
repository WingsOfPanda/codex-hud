use super::*;

struct TestDescendant(File);
impl TestDescendant {
    fn pin(pid: u32) -> Self {
        let before = identity(pid).unwrap();
        let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) } as i32;
        assert!(fd >= 0);
        let pin = unsafe { File::from_raw_fd(fd) };
        assert_eq!(identity(pid).unwrap(), before);
        Self(pin)
    }
}
impl Drop for TestDescendant {
    fn drop(&mut self) {
        // Assertions may unwind before the normal fixture cleanup runs.
        unsafe {
            libc::syscall(
                libc::SYS_pidfd_send_signal,
                self.0.as_raw_fd(),
                libc::SIGKILL,
                std::ptr::null::<libc::siginfo_t>(),
                0,
            );
        }
    }
}

fn setup(script: &str) -> (tempfile::TempDir, String, Record, Directory) {
    let temp = tempfile::tempdir().unwrap();
    let root = uuid::Uuid::new_v4().to_string();
    let prepared=prepare(temp.path(),&root,&json!({"root":root,"turn":"origin","input_epoch":1,"goal":null}),
        &json!({"schema":1,"topic":"unit execution","argv":["/bin/sh","-c",script],"cwd":temp.path(),"env":{},"exclusive_keys":["device:test"],"cancel_grace_seconds":0})).unwrap();
    let id = prepared["id"].as_str().unwrap();
    let dir = job(temp.path(), &root, id).unwrap();
    let record = load(&dir, &root, id).unwrap();
    (temp, root, record, dir)
}

#[test]
fn prepared_cancellation_never_launches_and_cannot_rearm_observation() {
    let (temp, root, record, dir) = setup("exit 99");
    let cancelled = cancel(temp.path(), &root, &record.id).unwrap();
    assert_eq!(cancelled["state"], "cancelled");
    assert_eq!(cancelled["result"]["workload_attempted"], false);
    mark_observation(
        temp.path(),
        &root,
        &record.id,
        "cancelled",
        &json!({"source":"test"}),
    )
    .unwrap();
    assert!(
        mark_observation(
            temp.path(),
            &root,
            &record.id,
            "armed",
            &json!({"explicit_rearm":true})
        )
        .is_err()
    );
    assert!(!dir.path.join("stdout.log").exists());
}

#[test]
fn unknown_launch_gap_is_idempotent_and_keeps_cross_root_claims() {
    let (temp, root, mut record, dir) = setup("exit 0");
    claim(temp.path(), &record, false).unwrap();
    record.state = State::Launching;
    dir.atomic("state.json", &record).unwrap();
    let unknown = reconcile(temp.path(), &root, &record.id).unwrap();
    assert_eq!(unknown["state"], "unknown");
    let again = launch(
        temp.path(),
        temp.path(),
        &root,
        &record.id,
        &record.review_digest,
        &json!({}),
    )
    .unwrap();
    assert_eq!(again["state"], "unknown");
    assert!(!dir.path.join("stdout.log").exists());
    let mut other = record.clone();
    other.root = uuid::Uuid::new_v4().to_string();
    other.id = uuid::Uuid::new_v4().to_string();
    assert!(claim(temp.path(), &other, false).is_err());
    assert!(
        resolve(
            temp.path(),
            &root,
            &record.id,
            &json!({"root":root,"id":record.id,"cleanup_verified":false,"evidence":"not enough"})
        )
        .is_err()
    );
    resolve(temp.path(),&root,&record.id,&json!({"root":root,"id":record.id,"cleanup_verified":true,"evidence":"verified no attempt crossed durable intent gap"})).unwrap();
    claim(temp.path(), &other, false).unwrap();
}

#[test]
fn real_command_exit_deadline_and_stale_identity() {
    let (temp, _, mut record, dir) = setup("printf hello; exit 7");
    let command = record.manifest.command.clone();
    let ran = run(
        temp.path(),
        &dir,
        &mut record,
        &command,
        "stdout.log",
        true,
        true,
    )
    .unwrap();
    assert_eq!(ran.status.code(), Some(7));
    assert!(ran.log_complete);
    assert_eq!(fs::read(dir.path.join("stdout.log")).unwrap(), b"hello");
    let mut deadline = command.clone();
    deadline.spec.argv = vec![
        deadline.spec.argv[0].clone(),
        "-c".into(),
        "trap '' TERM; while :; do sleep .1; done".into(),
    ];
    deadline.spec.deadline_seconds = Some(1);
    let ran = run(
        temp.path(),
        &dir,
        &mut record,
        &deadline,
        "deadline.log",
        true,
        false,
    )
    .unwrap();
    assert_eq!(ran.termination.as_deref(), Some("deadline"));
    assert_eq!(ran.status.signal(), Some(libc::SIGKILL));
    let mut process = identity(std::process::id()).unwrap();
    process.start_ticks += 1;
    assert!(!alive(&process).unwrap());
}

#[test]
fn snapshot_changes_unknown_fields_and_symlinks_refuse() {
    let temp = tempfile::tempdir().unwrap();
    let root = uuid::Uuid::new_v4().to_string();
    let input = temp.path().join("script.py");
    fs::write(&input, "print(1)\n").unwrap();
    let manifest = json!({"schema":1,"topic":"snapshot","argv":["/usr/bin/python3","{inputs}/nested/script.py"],"cwd":temp.path(),"inputs":[{"source":input,"path":"nested/script.py"}]});
    let prepared = prepare(temp.path(), &root, &json!({"root":root}), &manifest).unwrap();
    let id = prepared["id"].as_str().unwrap();
    let dir = job(temp.path(), &root, id).unwrap();
    let record = load(&dir, &root, id).unwrap();
    validate_inputs(&dir, &record.manifest, false).unwrap();
    fs::write(&input, "print(2)\n").unwrap();
    assert!(validate_inputs(&dir, &record.manifest, false).is_err());
    let mut invalid = manifest.clone();
    invalid["unreviewed_option"] = json!(true);
    assert!(prepare(temp.path(), &root, &json!({"root":root}), &invalid).is_err());
    let state = dir.path.join("state.json");
    fs::remove_file(&state).unwrap();
    std::os::unix::fs::symlink(&input, &state).unwrap();
    assert!(read(temp.path(), &root, id).is_err());
}

#[test]
fn readonly_listing_failed_preparation_and_partial_results() {
    let empty = tempfile::tempdir().unwrap();
    let root = uuid::Uuid::new_v4().to_string();
    assert!(list(empty.path(), &root).unwrap().is_empty());
    assert!(!empty.path().join("codex-plus").exists());
    assert!(
        prepare(
            empty.path(),
            &root,
            &json!({"root":root}),
            &json!({"schema":1,"topic":"bad","argv":["relative"],"cwd":empty.path()})
        )
        .is_err()
    );
    assert!(list(empty.path(), &root).unwrap().is_empty());
    let (temp, root, mut record, dir) = setup("exit 0");
    record.state = State::Running;
    dir.atomic("state.json", &record).unwrap();
    fs::write(dir.path.join("result.json"), b"{\"schema\":1,").unwrap();
    assert_eq!(
        reconcile(temp.path(), &root, &record.id).unwrap()["state"],
        "unknown"
    );
    assert_eq!(
        fs::read(dir.path.join("result.json")).unwrap(),
        b"{\"schema\":1,"
    );
}

#[test]
fn explicit_cancel_is_separate_from_waitpid_and_cleanup_failure_keeps_claims() {
    let (temp, root, mut record, dir) = setup("trap '' TERM; while :; do sleep .1; done");
    claim(temp.path(), &record, false).unwrap();
    record.state = State::Launching;
    save(temp.path(), &dir, &mut record).unwrap();
    let home = temp.path().to_owned();
    let cancel_root = root.clone();
    let cancel_id = record.id.clone();
    let requester = thread::spawn(move || {
        thread::sleep(Duration::from_millis(150));
        let status = cancel(&home, &cancel_root, &cancel_id).unwrap();
        assert_eq!(status["state"], "cancel_requested");
        assert!(status["result"].is_null());
    });
    let command = record.manifest.command.clone();
    let ran = run(
        temp.path(),
        &dir,
        &mut record,
        &command,
        "stdout.log",
        true,
        true,
    )
    .unwrap();
    requester.join().unwrap();
    assert_eq!(ran.termination.as_deref(), Some("cancel"));
    assert_eq!(ran.status.signal(), Some(libc::SIGKILL));
    let outcome = Outcome {
        schema: 1,
        id: record.id.clone(),
        root: root.clone(),
        state: State::Cancelled,
        finished_at: local::now(),
        exit_code: None,
        signal: Some(libc::SIGKILL),
        workload_attempted: true,
        executor: record.executor.clone(),
        workload: record.workload.clone(),
        log_complete: ran.log_complete,
        cleanup: CleanupResult {
            status: "failed".into(),
            exit_code: Some(9),
            signal: None,
            log_complete: true,
            error: None,
        },
        termination: Some("cancel".into()),
        error: None,
    };
    result(temp.path(), &dir, &mut record, outcome).unwrap();
    let claims: Claims = base(temp.path(), false)
        .unwrap()
        .read("resources.json")
        .unwrap();
    assert_eq!(claims.keys["device:test"].id, record.id);
}

#[test]
fn cancelling_launch_intent_before_workload_publication_preserves_reservation() {
    let (temp, root, mut record, dir) = setup("exit 0");
    claim(temp.path(), &record, false).unwrap();
    record.state = State::Launching;
    save(temp.path(), &dir, &mut record).unwrap();
    let request = cancel(temp.path(), &root, &record.id).unwrap();
    assert_eq!(request["state"], "cancel_requested");
    assert!(request["result"].is_null());
    let claims: Claims = base(temp.path(), false)
        .unwrap()
        .read("resources.json")
        .unwrap();
    assert_eq!(claims.keys["device:test"].id, record.id);
}

#[test]
fn log_disk_failure_drains_without_cancelling_child() {
    let mut pipes = [0; 2];
    assert_eq!(
        unsafe { libc::pipe2(pipes.as_mut_ptr(), libc::O_CLOEXEC | libc::O_NONBLOCK) },
        0
    );
    let input = unsafe { File::from_raw_fd(pipes[0]) };
    let output = unsafe { File::from_raw_fd(pipes[1]) };
    let capture = start_capture(
        input,
        OpenOptions::new().write(true).open("/dev/full").unwrap(),
    );
    let mut child = Command::new("/bin/sh")
        .args(["-c", "printf 'not lost to SIGPIPE'; exit 0"])
        .stdout(output.try_clone().unwrap())
        .stderr(output)
        .spawn()
        .unwrap();
    assert_eq!(child.wait().unwrap().code(), Some(0));
    assert!(!capture.finish().0);
}

#[test]
fn capped_log_keeps_draining_and_publishes_incomplete_failure() {
    let (temp, root, mut record, dir) = setup("exit 0");
    let mut pipes = [0; 2];
    assert_eq!(
        unsafe { libc::pipe2(pipes.as_mut_ptr(), libc::O_CLOEXEC) },
        0
    );
    let input = unsafe { File::from_raw_fd(pipes[0]) };
    let output = unsafe { File::from_raw_fd(pipes[1]) };
    let flags = unsafe { libc::fcntl(input.as_raw_fd(), libc::F_GETFL) };
    assert!(
        flags >= 0
            && unsafe { libc::fcntl(input.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) }
                >= 0
    );
    let capture = start_capture_with_limit(
        input,
        dir.open_file("stdout.log", libc::O_CREAT | libc::O_EXCL | libc::O_WRONLY)
            .unwrap(),
        128,
    );
    let mut command = Command::new("/usr/bin/python3");
    command.args(["-c","import os; from pathlib import Path; os.write(1,b'x'*1048576); Path('side-effect').write_text('once')"])
        .env_clear().current_dir(temp.path()).stdout(output.try_clone().unwrap()).stderr(output);
    unsafe {
        command.pre_exec(|| {
            if libc::setpgid(0, 0) < 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = command.spawn().unwrap();
    drop(command);
    record.workload = Some(identity(child.id()).unwrap());
    let status = child.wait().unwrap();
    let (complete, eof) = capture.finish();
    assert!(status.success());
    assert!(eof);
    assert!(!complete);
    assert_eq!(
        fs::metadata(dir.path.join("stdout.log")).unwrap().len(),
        128
    );
    assert_eq!(
        fs::read_to_string(temp.path().join("side-effect")).unwrap(),
        "once"
    );
    let ran = Ran {
        status,
        log_complete: complete,
        termination: None,
    };
    let cleanup = CleanupResult::none();
    let outcome = Outcome {
        schema: 1,
        id: record.id.clone(),
        root: root.clone(),
        state: ran.state(&cleanup),
        finished_at: local::now(),
        exit_code: ran.status.code(),
        signal: ran.status.signal(),
        workload_attempted: true,
        executor: None,
        workload: record.workload.clone(),
        log_complete: ran.log_complete,
        cleanup,
        termination: None,
        error: None,
    };
    result(temp.path(), &dir, &mut record, outcome).unwrap();
    let receipt = read(temp.path(), &root, &record.id).unwrap();
    assert_eq!(receipt["state"], "failed");
    assert_eq!(receipt["result"]["exit_code"], 0);
    assert_eq!(receipt["result"]["log_complete"], false);
}

#[test]
fn reported_stage_is_bounded_regular_private_and_never_changes_outcome() {
    let (temp, root, mut record, dir) = setup("exit 0");
    record.started_at = Some(local::now());
    assert_eq!(reported_stage(&dir), (None, None));
    dir.atomic("stage.json", &json!({"stage":"training epoch 2"}))
        .unwrap();
    assert_eq!(reported_stage(&dir).0.as_deref(), Some("training epoch 2"));
    dir.atomic("stage.json", &json!({"stage":"\u{1b}[31munsafe"}))
        .unwrap();
    assert!(reported_stage(&dir).1.is_some());
    dir.atomic("stage.json", &json!({"stage":"x".repeat(1500)}))
        .unwrap();
    assert!(reported_stage(&dir).1.is_some());
    dir.remove("stage.json").unwrap();
    std::os::unix::fs::symlink("/dev/zero", dir.path.join("stage.json")).unwrap();
    assert!(reported_stage(&dir).1.is_some());
    fs::remove_file(dir.path.join("stage.json")).unwrap();
    let path = CString::new(dir.path.join("stage.json").to_str().unwrap()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o600) }, 0);
    assert!(reported_stage(&dir).1.is_some());
    projection(temp.path(), &record).unwrap();
    let projected: Value = local::read_regular(
        &temp.path().join(format!(
            "codex-plus/execution-status/{root}/{}.json",
            record.id
        )),
        4096,
    )
    .and_then(|b| Ok(serde_json::from_slice(&b)?))
    .unwrap();
    assert!(
        projected["error"]
            .as_str()
            .unwrap()
            .contains("Reported stage unavailable")
    );
    assert_eq!(
        read(temp.path(), &root, &record.id).unwrap()["state"],
        "prepared"
    );
}

#[test]
fn terminal_projections_remove_settled_outcomes_and_surface_monitoring_gaps() {
    for state in [State::Completed, State::Failed, State::Cancelled] {
        let (temp, root, mut record, dir) = setup("exit 0");
        record.state = state;
        record.started_at = Some(local::now());
        record.error = (state == State::Failed).then(|| "known command failure".into());
        record.result = Some(Outcome {
            schema: 1,
            id: record.id.clone(),
            root: root.clone(),
            state,
            finished_at: local::now(),
            exit_code: if state == State::Cancelled {
                None
            } else {
                Some(if state == State::Completed { 0 } else { 7 })
            },
            signal: if state == State::Cancelled {
                Some(libc::SIGTERM)
            } else {
                None
            },
            workload_attempted: true,
            executor: None,
            workload: None,
            log_complete: true,
            cleanup: CleanupResult::none(),
            termination: None,
            error: record.error.clone(),
        });
        dir.atomic("state.json", &record).unwrap();
        dir.atomic("result.json", record.result.as_ref().unwrap())
            .unwrap();
        let path = temp.path().join(format!(
            "codex-plus/execution-status/{root}/{}.json",
            record.id
        ));
        for observation_state in ["pending", "armed", "recovery_needed", "cancelled"] {
            dir.atomic(
                "observation.json",
                &Observation {
                    schema: 1,
                    root: root.clone(),
                    id: record.id.clone(),
                    state: observation_state.into(),
                    updated_at: local::now(),
                    evidence: json!({"source":"projection fixture"}),
                },
            )
            .unwrap();
            projection(temp.path(), &record).unwrap();
            if ["armed", "cancelled"].contains(&observation_state) {
                assert!(!path.exists());
            } else {
                let projected: Value =
                    serde_json::from_slice(&local::read_regular(&path, 4096).unwrap()).unwrap();
                assert_eq!(projected["state"], "unknown");
                assert!(
                    projected["error"]
                        .as_str()
                        .unwrap()
                        .contains("Completion monitoring")
                );
            }
            assert_eq!(
                read(temp.path(), &root, &record.id).unwrap()["state"],
                serde_json::to_value(state).unwrap()
            );
        }
        if state != State::Completed {
            record.result.as_mut().unwrap().cleanup.status = "unknown".into();
            projection(temp.path(), &record).unwrap();
            let projected: Value =
                serde_json::from_slice(&local::read_regular(&path, 4096).unwrap()).unwrap();
            assert_eq!(projected["state"], "unknown");
            assert!(projected["error"].as_str().unwrap().contains("cleanup"));
        }
    }
}

#[test]
fn continuous_descendant_output_cannot_starve_reviewed_cleanup() {
    let (temp, root, mut record, dir) = setup("exit 0");
    let producer = r#"import json, os, signal, time
from pathlib import Path
if os.fork() == 0:
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    ticks=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]
    Path('writer.json').write_text(json.dumps({'pid':os.getpid(),'start':ticks,'boot':Path('/proc/sys/kernel/random/boot_id').read_text()}))
    while True:
        try: os.write(1,b'x'*65536)
        except BrokenPipeError: time.sleep(.01)
else:
    while not Path('writer.json').exists(): time.sleep(.005)
"#;
    let cleanup_script = r#"import json, os, signal
from pathlib import Path
saved=json.loads(Path('writer.json').read_text())
assert Path('/proc/sys/kernel/random/boot_id').read_text()==saved['boot']
def ticks(): return Path('/proc/'+str(saved['pid'])+'/stat').read_text().rsplit(')',1)[1].split()[19]
assert ticks()==saved['start']
pin=os.pidfd_open(saved['pid'])
assert ticks()==saved['start']
signal.pidfd_send_signal(pin,signal.SIGKILL)
os.close(pin)
Path('cleanup-marker').write_text('reviewed cleanup ran')
"#;
    let spec = |script: &str, deadline| CommandSpec {
        argv: vec!["/usr/bin/python3".into(), "-c".into(), script.into()],
        cwd: temp.path().to_owned(),
        env: BTreeMap::new(),
        env_refs: BTreeMap::new(),
        secret_refs: BTreeMap::new(),
        deadline_seconds: deadline,
    };
    record.manifest.command =
        freeze_command(spec(producer, None), &dir.path.join("inputs")).unwrap();
    record.manifest.cleanup =
        Some(freeze_command(spec(cleanup_script, Some(2)), &dir.path.join("inputs")).unwrap());
    record.state = State::Launching;
    record.started_at = Some(local::now());
    save(temp.path(), &dir, &mut record).unwrap();
    let (sender, receiver) = std::sync::mpsc::channel();
    let home = temp.path().to_owned();
    let thread_root = root.clone();
    let id = record.id.clone();
    let driver = thread::spawn(move || {
        let dir = job(&home, &thread_root, &id).unwrap();
        let command = record.manifest.command.clone();
        let ran = run(&home, &dir, &mut record, &command, "stdout.log", true, true);
        sender.send((ran, record)).unwrap();
    });
    let deadline = Instant::now() + Duration::from_secs(3);
    let writer = loop {
        if let Some(writer) = fs::read(temp.path().join("writer.json"))
            .ok()
            .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
        {
            break writer;
        }
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(10));
    };
    let writer_guard = TestDescendant::pin(writer["pid"].as_u64().unwrap() as u32);
    let (ran, mut record) = receiver
        .recv_timeout(Duration::from_secs(2))
        .expect("continuous output prevented reviewed cleanup");
    driver.join().unwrap();
    let ran = ran.unwrap();
    assert!(ran.status.success());
    assert!(!ran.log_complete);
    assert!(!record.output_closed);
    let cleaned = cleanup(temp.path(), &dir, &mut record);
    assert_eq!(cleaned.status, "completed");
    assert!(temp.path().join("cleanup-marker").exists());
    drop(writer_guard);
}

#[test]
fn execution_placeholders_and_environment_transport_are_explicit() {
    let temp = tempfile::tempdir().unwrap();
    let root = uuid::Uuid::new_v4().to_string();
    let prepared=prepare(temp.path(),&root,&json!({"root":root}),&json!({"schema":1,"topic":"explicit env","argv":["/bin/sh","-c","printf '%s:%s' \"$STAGE_FILE\" \"${HOME-unset}\""],"cwd":"{execution}","env":{"STAGE_FILE":"{execution}/stage.json"}})).unwrap();
    let dir = job(temp.path(), &root, prepared["id"].as_str().unwrap()).unwrap();
    let mut record = load(&dir, &root, prepared["id"].as_str().unwrap()).unwrap();
    assert_eq!(record.manifest.command.spec.cwd, dir.path);
    assert_eq!(
        record.manifest.command.spec.env["STAGE_FILE"],
        dir.path.join("stage.json").to_str().unwrap()
    );
    let command = record.manifest.command.clone();
    let ran = run(
        temp.path(),
        &dir,
        &mut record,
        &command,
        "stdout.log",
        true,
        true,
    )
    .unwrap();
    assert!(ran.status.success());
    assert_eq!(
        fs::read_to_string(dir.path.join("stdout.log")).unwrap(),
        format!("{}:unset", dir.path.join("stage.json").display())
    );
    let mut command = command;
    command
        .spec
        .env_refs
        .insert("EXPLICIT_HOME".into(), "HOME".into());
    assert!(environment(&command, true).is_err());
    assert!(
        environment(&command, false)
            .unwrap()
            .contains_key("EXPLICIT_HOME")
    );
    assert_ne!(reference_key("LD_PRELOAD"), "LD_PRELOAD");
}

#[test]
fn malformed_cancel_does_not_kill_and_live_descendants_require_cleanup() {
    let (temp, _, mut record, dir) = setup("sleep .15; exit 0");
    dir.atomic("cancel.json", &json!({"id":"not the owning UUID"}))
        .unwrap();
    let command = record.manifest.command.clone();
    let ran = run(
        temp.path(),
        &dir,
        &mut record,
        &command,
        "stdout.log",
        true,
        true,
    )
    .unwrap();
    assert!(ran.status.success());
    assert!(
        record
            .error
            .as_deref()
            .unwrap()
            .contains("Invalid cancellation request")
    );
    dir.remove("cancel.json").unwrap();
    let mut command = command;
    command.spec.argv[2] = "sleep .4 >/dev/null 2>&1 & exit 0".into();
    let ran = run(
        temp.path(),
        &dir,
        &mut record,
        &command,
        "descendant.log",
        true,
        false,
    )
    .unwrap();
    assert!(ran.status.success());
    assert!(remaining_groups(&record).unwrap());
    thread::sleep(Duration::from_millis(500));
    assert!(!remaining_groups(&record).unwrap());
}

#[test]
fn duplicate_executor_entry_cannot_corrupt_prepared_or_live_execution() {
    let (temp, root, mut record, dir) = setup("exit 0");
    assert_eq!(execute(temp.path(), temp.path(), &root, &record.id), 1);
    assert_eq!(
        read(temp.path(), &root, &record.id).unwrap()["state"],
        "prepared"
    );
    record.state = State::Running;
    record.executor = Some(identity(std::process::id()).unwrap());
    dir.atomic("state.json", &record).unwrap();
    let _owner = Lock::acquire(&dir, "executor.lock", false)
        .unwrap()
        .unwrap();
    assert_eq!(execute(temp.path(), temp.path(), &root, &record.id), 1);
    assert_eq!(
        read(temp.path(), &root, &record.id).unwrap()["state"],
        "running"
    );
}

#[test]
fn lost_initialized_resource_registry_refuses_new_claims_and_maintenance_scan() {
    let (temp, _, record, _) = setup("exit 0");
    claim(temp.path(), &record, false).unwrap();
    fs::remove_file(
        base(temp.path(), false)
            .unwrap()
            .path
            .join("resources.json"),
    )
    .unwrap();
    assert!(claim(temp.path(), &record, false).is_err());
    assert!(roots(temp.path()).is_err());
}

#[test]
fn cancellation_escalates_owned_descendants_or_keeps_visible_cleanup_unknown() {
    for grace in [Some(1), None] {
        let (temp, root, mut record, dir) = setup(
            "trap 'exit 0' TERM; sh -c 'trap \"\" TERM; echo $$ > grandchild.pid; while :; do sleep .1; done' & while [ ! -s grandchild.pid ]; do sleep .01; done; while :; do sleep .1; done",
        );
        record.manifest.cancel_grace_seconds = grace;
        record.started_at = Some(local::now());
        claim(temp.path(), &record, false).unwrap();
        record.state = State::Launching;
        save(temp.path(), &dir, &mut record).unwrap();
        let home = temp.path().to_owned();
        let child_root = root.clone();
        let id = record.id.clone();
        let requester = thread::spawn(move || {
            let path = home.join("grandchild.pid");
            let deadline = Instant::now() + Duration::from_secs(3);
            while !fs::read_to_string(&path)
                .ok()
                .is_some_and(|s| s.trim().parse::<u32>().is_ok())
            {
                assert!(Instant::now() < deadline);
                thread::sleep(Duration::from_millis(10));
            }
            let pid = fs::read_to_string(&path).unwrap().trim().parse().unwrap();
            let cleanup = TestDescendant::pin(pid);
            assert_ne!(read(&home, &child_root, &id).unwrap()["state"], "prepared");
            cancel(&home, &child_root, &id).unwrap();
            cleanup
        });
        let command = record.manifest.command.clone();
        let ran = run(
            temp.path(),
            &dir,
            &mut record,
            &command,
            "stdout.log",
            true,
            true,
        )
        .unwrap();
        let cleanup = requester.join().unwrap();
        assert_eq!(ran.status.code(), Some(0));
        assert_eq!(ran.termination.as_deref(), Some("cancel"));
        if grace.is_some() {
            assert!(
                !remaining_groups(&record).unwrap(),
                "reviewed escalation left a live group"
            );
            assert!(record.output_closed);
        } else {
            assert!(remaining_groups(&record).unwrap());
            assert!(!record.output_closed);
            let outcome = Outcome {
                schema: 1,
                id: record.id.clone(),
                root: root.clone(),
                state: State::Cancelled,
                finished_at: local::now(),
                exit_code: Some(0),
                signal: None,
                workload_attempted: true,
                executor: record.executor.clone(),
                workload: record.workload.clone(),
                log_complete: ran.log_complete,
                cleanup: CleanupResult {
                    status: "unknown".into(),
                    exit_code: None,
                    signal: None,
                    log_complete: true,
                    error: Some("workload group still has live members".into()),
                },
                termination: Some("cancel".into()),
                error: None,
            };
            result(temp.path(), &dir, &mut record, outcome).unwrap();
            let projected: Value = serde_json::from_slice(
                &local::read_regular(
                    &temp.path().join(format!(
                        "codex-plus/execution-status/{root}/{}.json",
                        record.id
                    )),
                    4096,
                )
                .unwrap(),
            )
            .unwrap();
            assert_eq!(projected["state"], "unknown");
            assert!(projected["error"].is_string());
            assert_eq!(
                read(temp.path(), &root, &record.id).unwrap()["result"]["exit_code"],
                0
            );
            let claims: Claims = base(temp.path(), false)
                .unwrap()
                .read("resources.json")
                .unwrap();
            assert_eq!(claims.keys["device:test"].id, record.id);
            drop(cleanup);
            let until = Instant::now() + Duration::from_secs(2);
            while remaining_groups(&record).unwrap() {
                assert!(Instant::now() < until);
                thread::sleep(Duration::from_millis(20));
            }
        }
    }
}

#[test]
fn fault_child() {
    let Ok(home) = env::var("CODEX_PLUS_TEST_EXECUTION_HOME") else {
        return;
    };
    let root = env::var("CODEX_PLUS_TEST_EXECUTION_ROOT").unwrap();
    let id = env::var("CODEX_PLUS_TEST_EXECUTION_ID").unwrap();
    let point = env::var("CODEX_PLUS_TEST_EXECUTION_FAULT").unwrap();
    let point: &'static str = Box::leak(point.into_boxed_str());
    FAULT.with(|f| f.set(Some(point)));
    let home = Path::new(&home);
    let dir = job(home, &root, &id).unwrap();
    let mut record = load(&dir, &root, &id).unwrap();
    let _executor = Lock::acquire(&dir, "executor.lock", false)
        .unwrap()
        .unwrap();
    claim(home, &record, false).unwrap();
    record.state = State::Launching;
    record.executor = Some(identity(std::process::id()).unwrap());
    save(home, &dir, &mut record).unwrap();
    // Startup edges deliberately use the same durable helpers without a model/daemon fixture.
    checkpoint("intent");
    checkpoint("executor_spawn");
    checkpoint("executor_identity");
    let command = record.manifest.command.clone();
    let running = run(home, &dir, &mut record, &command, "stdout.log", true, true);
    if point == "post_spawn_identity" {
        assert!(running.is_err());
        assert_eq!(record.state, State::Unknown);
        let partial: Value = dir.read("unverified-child.json").unwrap();
        assert_eq!(partial["exit_code"], 0);
        assert_eq!(partial["identity_verified"], false);
        let claims: Claims = base(home, false).unwrap().read("resources.json").unwrap();
        assert_eq!(claims.keys["device:test"].id, id);
        unsafe {
            libc::_exit(88);
        }
    }
    let ran = running.unwrap();
    let outcome = Outcome {
        schema: 1,
        id: id.clone(),
        root: root.clone(),
        state: if ran.status.success() {
            State::Completed
        } else {
            State::Failed
        },
        finished_at: local::now(),
        exit_code: ran.status.code(),
        signal: ran.status.signal(),
        workload_attempted: true,
        executor: record.executor.clone(),
        workload: record.workload.clone(),
        log_complete: ran.log_complete,
        cleanup: CleanupResult::none(),
        termination: ran.termination,
        error: None,
    };
    if point == "result_write_failure" {
        // The private test child alone gets a real regular-file write failure after waitpid.
        let limit = libc::rlimit {
            rlim_cur: 0,
            rlim_max: 0,
        };
        unsafe {
            libc::signal(libc::SIGXFSZ, libc::SIG_IGN);
            assert_eq!(libc::setrlimit(libc::RLIMIT_FSIZE, &limit), 0);
        }
        assert!(result(home, &dir, &mut record, outcome).is_err());
        unsafe {
            libc::_exit(87);
        }
    }
    result(home, &dir, &mut record, outcome).unwrap();
}

#[test]
fn fault_boundaries_never_replay_and_receipt_commit_recovers() {
    for point in [
        "intent",
        "executor_spawn",
        "executor_identity",
        "workload_spawn",
        "post_spawn_identity",
        "workload_exec",
        "workload_identity",
        "child_exit",
        "result_commit",
        "result_write_failure",
    ] {
        let (temp, root, record, dir) = setup("printf 'run\\n' >> side-effects; sleep .15; exit 0");
        let output = Command::new(env::current_exe().unwrap())
            .args([
                "--exact",
                "executions::executor::tests::fault_child",
                "--nocapture",
            ])
            .env("CODEX_PLUS_TEST_EXECUTION_HOME", temp.path())
            .env("CODEX_PLUS_TEST_EXECUTION_ROOT", &root)
            .env("CODEX_PLUS_TEST_EXECUTION_ID", &record.id)
            .env("CODEX_PLUS_TEST_EXECUTION_FAULT", point)
            .output()
            .unwrap();
        if point != "workload_exec" {
            assert_eq!(
                output.status.code(),
                Some(if point == "post_spawn_identity" {
                    88
                } else if point == "result_write_failure" {
                    87
                } else {
                    86
                }),
                "point={point} stderr={}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
        thread::sleep(Duration::from_millis(250));
        let observed = reconcile(temp.path(), &root, &record.id).unwrap();
        let expected = match point {
            "result_commit" => "completed",
            "workload_exec" => "failed",
            _ => "unknown",
        };
        assert_eq!(observed["state"], expected, "point={point}");
        let again = launch(
            temp.path(),
            temp.path(),
            &root,
            &record.id,
            &record.review_digest,
            &json!({}),
        )
        .unwrap();
        assert_eq!(again["state"], expected);
        let marker = temp.path().join("side-effects");
        let effects = fs::read_to_string(marker)
            .unwrap_or_default()
            .lines()
            .count();
        assert_eq!(
            effects,
            if [
                "intent",
                "executor_spawn",
                "executor_identity",
                "workload_exec"
            ]
            .contains(&point)
            {
                0
            } else {
                1
            },
            "point={point}"
        );
        assert_eq!(
            dir.path.join("result.json").exists(),
            ["result_commit", "workload_exec"].contains(&point)
        );
    }
}
