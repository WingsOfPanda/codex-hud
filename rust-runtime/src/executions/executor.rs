use super::*;

#[cfg(test)]
thread_local! { static FAULT:std::cell::Cell<Option<&'static str>>=const{std::cell::Cell::new(None)}; }
pub(super) fn checkpoint(label: &str) {
    #[cfg(test)]
    if FAULT.with(|fault| fault.get() == Some(label)) {
        unsafe {
            libc::_exit(86);
        }
    }
    #[cfg(not(test))]
    let _ = label;
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Claim {
    root: String,
    id: String,
    claimed_at: f64,
}
#[derive(Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Claims {
    schema: u32,
    keys: BTreeMap<String, Claim>,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct RegistryInitialized {
    schema: u32,
}
fn registry(dir: &Directory) -> Result<Option<Claims>> {
    let initialized = dir.optional::<RegistryInitialized>("resources-initialized.json")?;
    ensure!(
        initialized.as_ref().is_none_or(|v| v.schema == 1),
        "unsupported resource registry marker"
    );
    let claims = dir.optional::<Claims>("resources.json")?;
    ensure!(
        claims.is_some() || initialized.is_none(),
        "resource registry is missing after initialization; reservations are unknown"
    );
    if let Some(claims) = &claims {
        ensure!(claims.schema == 1, "unsupported resource registry schema");
        for (key, owner) in &claims.keys {
            ensure!(
                !key.is_empty() && key.len() <= 256 && owner.claimed_at.is_finite(),
                "invalid resource reservation"
            );
            uuid(&owner.root)?;
            uuid(&owner.id)?;
        }
    }
    Ok(claims)
}
pub(super) fn validate_registry(dir: &Directory) -> Result<()> {
    registry(dir)?;
    Ok(())
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Observation {
    schema: u32,
    root: String,
    id: String,
    state: String,
    updated_at: f64,
    evidence: Value,
}

fn observation(dir: &Directory, record: &Record) -> Result<Observation> {
    let status = dir
        .optional::<Observation>("observation.json")?
        .unwrap_or(Observation {
            schema: 1,
            root: record.root.clone(),
            id: record.id.clone(),
            state: "recovery_needed".into(),
            updated_at: local::now(),
            evidence: json!({"error":"observation marker unavailable"}),
        });
    ensure!(
        status.schema == 1
            && status.root == record.root
            && status.id == record.id
            && ["pending", "armed", "cancelled", "recovery_needed"]
                .contains(&status.state.as_str()),
        "invalid observation marker"
    );
    Ok(status)
}
pub fn read_observation(home: &Path, root: &str, id: &str) -> Result<Value> {
    let dir = job(home, root, id)?;
    let record = load(&dir, root, id)?;
    Ok(serde_json::to_value(observation(&dir, &record)?)?)
}
pub fn mark_observation(
    home: &Path,
    root: &str,
    id: &str,
    state: &str,
    evidence: &Value,
) -> Result<Value> {
    ensure!(
        ["pending", "armed", "cancelled", "recovery_needed"].contains(&state),
        "invalid observation state"
    );
    ensure!(
        serde_json::to_vec(evidence)?.len() <= 16384,
        "observation evidence exceeds bound"
    );
    let dir = job(home, root, id)?;
    let _lock = Lock::acquire(&dir, "job.lock", false)?.unwrap();
    let record = load(&dir, root, id)?;
    let prior = observation(&dir, &record)?;
    ensure!(
        prior.state != "cancelled" || state == "cancelled",
        "explicitly cancelled observation cannot be rearmed"
    );
    ensure!(
        state != "pending" || prior.state == "pending",
        "observation cannot silently reset to pending"
    );
    let status = Observation {
        schema: 1,
        root: root.into(),
        id: id.into(),
        state: state.into(),
        updated_at: local::now(),
        evidence: evidence.clone(),
    };
    dir.atomic("observation.json", &status)?;
    if let Err(error) = projection(home, &record) {
        eprintln!("execution projection unavailable: {error:#}");
    }
    Ok(serde_json::to_value(status)?)
}

fn claim(home: &Path, record: &Record, release: bool) -> Result<()> {
    let base = base(home, true)?;
    let _lock = Lock::acquire(&base, "resources.lock", false)?.unwrap();
    let mut claims: Claims = registry(&base)?.unwrap_or(Claims {
        schema: 1,
        keys: BTreeMap::new(),
    });
    ensure!(claims.schema == 1, "unsupported resource registry schema");
    if !release {
        for key in &record.manifest.exclusive_keys {
            if let Some(owner) = claims.keys.get(key) {
                ensure!(
                    owner.root == record.root && owner.id == record.id,
                    "resource conflict: {key} reserved by root={} execution={}",
                    owner.root,
                    owner.id
                );
            }
        }
        for key in &record.manifest.exclusive_keys {
            claims.keys.insert(
                key.clone(),
                Claim {
                    root: record.root.clone(),
                    id: record.id.clone(),
                    claimed_at: local::now(),
                },
            );
        }
    } else {
        claims
            .keys
            .retain(|_, owner| owner.root != record.root || owner.id != record.id);
    }
    base.atomic("resources.json", &claims)?;
    base.atomic(
        "resources-initialized.json",
        &RegistryInitialized { schema: 1 },
    )
}

fn projection(home: &Path, record: &Record) -> Result<()> {
    let dir = Directory::open(&local::state_directory(home)?)?
        .child("execution-status", true)?
        .child(&record.root, true)?;
    let name = format!("{}.json", record.id);
    if record.started_at.is_none() {
        return dir.remove(&name);
    }
    let jobdir = job(home, &record.root, &record.id)?;
    let status = observation(&jobdir, record).unwrap_or_else(|_| Observation {
        schema: 1,
        root: record.root.clone(),
        id: record.id.clone(),
        state: "recovery_needed".into(),
        updated_at: local::now(),
        evidence: json!({"error":"invalid observation marker"}),
    });
    let unresolved =
        record.resolution.is_none() && record.result.as_ref().is_some_and(|r| !r.cleanup.safe());
    let monitoring_gap =
        record.state.terminal() && ["pending", "recovery_needed"].contains(&status.state.as_str());
    if record.state.terminal()
        && !unresolved
        && ["armed", "cancelled"].contains(&status.state.as_str())
    {
        return dir.remove(&name);
    }
    let (stage, stage_error) = reported_stage(&jobdir);
    let mut error = record
        .error
        .clone()
        .or_else(|| match status.state.as_str() {
            "pending" => Some("Completion monitoring pending".into()),
            "recovery_needed" => {
                Some("Completion monitoring unavailable; explicit recovery required".into())
            }
            _ => None,
        });
    if let Some(warning) = &stage_error {
        error = Some(match error {
            Some(prior) => format!("{}; {warning}", prior.chars().take(512).collect::<String>()),
            None => warning.clone(),
        });
    }
    if unresolved {
        error = Some(
            record
                .result
                .as_ref()
                .and_then(|r| r.cleanup.error.clone())
                .unwrap_or_else(|| {
                    "Execution cleanup remains unresolved; resources stay reserved".into()
                }),
        );
    }
    if monitoring_gap {
        let warning = if status.state == "pending" {
            "Completion monitoring pending"
        } else {
            "Completion monitoring unavailable; explicit recovery required"
        };
        error = Some(match error {
            Some(prior) if prior != warning => {
                format!("{warning}; {}", prior.chars().take(512).collect::<String>())
            }
            _ => warning.into(),
        });
    }
    let projected_state = if unresolved || monitoring_gap {
        State::Unknown
    } else {
        record.state
    };
    dir.atomic(&name,&json!({"schema":1,"id":record.id,"root":record.root,"topic":record.manifest.topic,
        "state":projected_state,"started_at":record.started_at,"updated_at":local::now(),"stage":stage,"stage_error":stage_error,"observation_state":status.state,"error":error}))
}
fn reported_stage(dir: &Directory) -> (Option<String>, Option<String>) {
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Stage {
        stage: String,
    }
    match dir.read_bounded::<Stage>("stage.json", 1024) {
        Ok(stage)
            if !stage.stage.is_empty()
                && stage.stage.len() <= 200
                && !stage.stage.chars().any(char::is_control) =>
        {
            (Some(stage.stage), None)
        }
        Err(error)
            if error
                .downcast_ref::<io::Error>()
                .is_some_and(|e| e.kind() == io::ErrorKind::NotFound) =>
        {
            (None, None)
        }
        _ => (
            None,
            Some("Reported stage unavailable: invalid private stage.json".into()),
        ),
    }
}
fn save(home: &Path, dir: &Directory, record: &mut Record) -> Result<()> {
    record.updated_at = local::now();
    dir.atomic("state.json", record)?;
    if let Err(error) = projection(home, record) {
        eprintln!("execution HUD projection unavailable: {error:#}");
    }
    Ok(())
}

fn identity(pid: u32) -> Result<ProcessIdentity> {
    let text = fs::read_to_string(format!("/proc/{pid}/stat"))?;
    let fields: Vec<_> = text
        .rsplit_once(')')
        .context("invalid process identity")?
        .1
        .split_whitespace()
        .collect();
    ensure!(fields.len() > 19, "truncated process identity");
    Ok(ProcessIdentity {
        boot_id: fs::read_to_string("/proc/sys/kernel/random/boot_id")?
            .trim()
            .into(),
        pid,
        start_ticks: fields[19].parse()?,
        pgid: fields[2].parse()?,
        sid: fields[3].parse()?,
    })
}
fn alive(expected: &ProcessIdentity) -> Result<bool> {
    if fs::read_to_string("/proc/sys/kernel/random/boot_id")?.trim() != expected.boot_id {
        return Ok(false);
    }
    match identity(expected.pid) {
        Ok(actual) => {
            let stat = match fs::read_to_string(format!("/proc/{}/stat", expected.pid)) {
                Ok(s) => s,
                Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(false),
                Err(e) => return Err(e.into()),
            };
            let state = stat
                .rsplit_once(')')
                .context("invalid process identity")?
                .1
                .split_whitespace()
                .next()
                .unwrap_or("");
            Ok(actual == *expected && !matches!(state, "Z" | "X"))
        }
        Err(error)
            if error
                .downcast_ref::<io::Error>()
                .is_some_and(|e| e.kind() == io::ErrorKind::NotFound) =>
        {
            Ok(false)
        }
        Err(error) => Err(error),
    }
}
fn signal_child(child: &Child, expected: &ProcessIdentity, signal: i32) -> Result<()> {
    ensure!(
        child.id() == expected.pid && identity(expected.pid)? == *expected,
        "cannot signal stale workload identity"
    );
    ensure!(
        expected.pgid == expected.pid as i32 && expected.sid == unsafe { libc::getsid(0) },
        "workload group does not belong to this executor"
    );
    if unsafe { libc::kill(-expected.pgid, signal) } < 0 {
        let error = io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::ESRCH) {
            return Err(error.into());
        }
    }
    Ok(())
}

fn result(home: &Path, dir: &Directory, record: &mut Record, outcome: Outcome) -> Result<()> {
    ensure!(
        outcome.state.terminal(),
        "nonterminal outcome cannot be committed"
    );
    dir.atomic("result.json", &outcome)?;
    checkpoint("result_commit");
    record.state = outcome.state;
    record.error = outcome.error.clone();
    record.result = Some(outcome);
    save(home, dir, record)?;
    if record.result.as_ref().unwrap().cleanup.safe() {
        claim(home, record, true)?;
    }
    Ok(())
}
fn unknown(home: &Path, dir: &Directory, record: &mut Record, message: String) -> Result<()> {
    record.state = State::Unknown;
    record.error = Some(message);
    save(home, dir, record)
}

/// Resolve durable evidence only. In particular, missing acknowledgements never spawn again.
pub fn reconcile(home: &Path, root: &str, id: &str) -> Result<Value> {
    let dir = job(home, root, id)?;
    let _lock = Lock::acquire(&dir, "job.lock", false)?.unwrap();
    let mut record = load(&dir, root, id)?;
    match dir.optional::<Outcome>("result.json") {
        Ok(Some(outcome)) => {
            if !(outcome.schema == 1
                && outcome.id == id
                && outcome.root == root
                && outcome.state.terminal()
                && outcome.finished_at.is_finite()
                && !(outcome.exit_code.is_some() && outcome.signal.is_some())
                && outcome.exit_code.is_none_or(|n| (0..=255).contains(&n))
                && outcome.signal.is_none_or(|n| (1..128).contains(&n))
                && ["not_required", "completed", "failed", "unknown"]
                    .contains(&outcome.cleanup.status.as_str())
                && (outcome.state != State::Completed
                    || (outcome.workload_attempted
                        && outcome.exit_code == Some(0)
                        && outcome.signal.is_none()
                        && outcome.log_complete
                        && outcome.cleanup.safe()))
                && record
                    .executor
                    .as_ref()
                    .is_none_or(|p| outcome.executor.as_ref() == Some(p)))
            {
                unknown(
                    home,
                    &dir,
                    &mut record,
                    "terminal receipt identity/schema mismatch; outcome unknown".into(),
                )?;
                return value(&record);
            }
            record.state = outcome.state;
            record.executor = outcome.executor.clone();
            record.workload = outcome.workload.clone();
            record.error = outcome.error.clone();
            record.result = Some(outcome);
            save(home, &dir, &mut record)?;
            if record.result.as_ref().unwrap().cleanup.safe() {
                claim(home, &record, true)?;
            }
        }
        Err(error) => unknown(
            home,
            &dir,
            &mut record,
            format!("terminal receipt is invalid; outcome unknown: {error:#}"),
        )?,
        Ok(None) if record.state != State::Prepared && record.state != State::Unknown => {
            let living = record
                .executor
                .as_ref()
                .map(alive)
                .transpose()?
                .unwrap_or(false);
            if !living && Lock::acquire(&dir, "executor.lock", true)?.is_some() {
                unknown(home,&dir,&mut record,"executor unavailable without a durable terminal receipt; never replay this UUID".into())?;
            }
        }
        _ => {}
    }
    value(&record)
}

pub fn cancel(home: &Path, root: &str, id: &str) -> Result<Value> {
    let dir = job(home, root, id)?;
    let _lock = Lock::acquire(&dir, "job.lock", false)?.unwrap();
    let mut record = load(&dir, root, id)?;
    if record.state.terminal() || record.state == State::Unknown {
        return value(&record);
    }
    dir.atomic(
        "cancel.json",
        &json!({"schema":1,"root":root,"id":id,"requested_at":local::now()}),
    )?;
    if record.state == State::Prepared {
        let outcome = Outcome {
            schema: 1,
            id: id.into(),
            root: root.into(),
            state: State::Cancelled,
            finished_at: local::now(),
            exit_code: None,
            signal: None,
            workload_attempted: false,
            executor: record.executor.clone(),
            workload: record.workload.clone(),
            log_complete: true,
            cleanup: CleanupResult::none(),
            termination: Some("cancel".into()),
            error: None,
        };
        result(home, &dir, &mut record, outcome)?;
    } else {
        record.state = State::CancelRequested;
        save(home, &dir, &mut record)?;
    }
    value(&record)
}

/// Explicit root cleanup evidence can release reservations, without inventing an exit status.
pub fn resolve(home: &Path, root: &str, id: &str, evidence: &Value) -> Result<Value> {
    let dir = job(home, root, id)?;
    let _lock = Lock::acquire(&dir, "job.lock", false)?.unwrap();
    let mut record = load(&dir, root, id)?;
    ensure!(
        record.state == State::Unknown || record.result.as_ref().is_some_and(|r| !r.cleanup.safe()),
        "execution has no unresolved cleanup"
    );
    ensure!(
        evidence["root"] == root
            && evidence["id"] == id
            && evidence["cleanup_verified"] == true
            && evidence["evidence"]
                .as_str()
                .is_some_and(|s| !s.is_empty() && s.len() <= 8192),
        "resolution requires explicit root-bound verified cleanup evidence"
    );
    ensure!(
        Lock::acquire(&dir, "executor.lock", true)?.is_some(),
        "executor still owns this execution"
    );
    for process in record.executor.iter().chain(record.processes.iter()) {
        ensure!(!alive(process)?, "recorded execution process still exists");
    }
    for process in &record.processes {
        if fs::read_to_string("/proc/sys/kernel/random/boot_id")?.trim() == process.boot_id {
            for entry in fs::read_dir("/proc")? {
                let Ok(pid) = entry?.file_name().to_string_lossy().parse::<u32>() else {
                    continue;
                };
                match identity(pid) {
                    Ok(other) => ensure!(
                        other.pgid != process.pgid || other.sid != process.sid,
                        "recorded workload group still has processes; cleanup is unconfirmed"
                    ),
                    Err(error)
                        if error
                            .downcast_ref::<io::Error>()
                            .is_some_and(|e| e.kind() == io::ErrorKind::NotFound) => {}
                    Err(error) => return Err(error),
                }
            }
        }
    }
    dir.atomic("resolution.json", evidence)?;
    record.resolution = Some(evidence.clone());
    save(home, &dir, &mut record)?;
    claim(home, &record, true)?;
    value(&record)
}

fn close_inherited() -> io::Result<()> {
    // Mark rather than close: std's spawn-error pipe must remain valid until exec.
    if unsafe {
        libc::syscall(
            libc::SYS_close_range,
            3u32,
            u32::MAX,
            libc::CLOSE_RANGE_CLOEXEC,
        )
    } == 0
    {
        return Ok(());
    }
    let error = io::Error::last_os_error();
    if !matches!(error.raw_os_error(), Some(libc::ENOSYS | libc::EINVAL)) {
        return Err(error);
    }
    let maximum = unsafe { libc::sysconf(libc::_SC_OPEN_MAX) };
    if maximum < 0 {
        return Err(io::Error::last_os_error());
    }
    for fd in 3..maximum {
        unsafe {
            libc::fcntl(fd as i32, libc::F_SETFD, libc::FD_CLOEXEC);
        }
    }
    Ok(())
}

pub fn launch(
    home: &Path,
    package_root: &Path,
    root: &str,
    id: &str,
    review_digest: &str,
    admission: &Value,
) -> Result<Value> {
    let dir = job(home, root, id)?;
    let lock = Lock::acquire(&dir, "job.lock", false)?.unwrap();
    let mut record = load(&dir, root, id)?;
    ensure!(
        record.review_digest == review_digest,
        "execution review digest mismatch"
    );
    if record.state != State::Prepared {
        drop(lock);
        return reconcile(home, root, id);
    }
    ensure!(
        admission["root"] == root && admission["turn"] == record.origin["turn"],
        "admission does not match execution review binding"
    );
    ensure!(
        digest(
            &json!({"schema":1,"id":record.id,"root":record.root,"origin":record.origin,"manifest":record.manifest})
        )? == record.review_digest,
        "stored review was modified"
    );
    validate_inputs(&dir, &record.manifest, false)?;
    let package = package_root.canonicalize()?;
    let runtime = env::current_exe()?.canonicalize()?;
    ensure!(
        runtime.starts_with(&package),
        "executor binary must belong to its pinned package"
    );
    let (_, runtime_identity) = hash_file(&runtime, 1024 * 1024 * 1024)?;
    claim(home, &record, false)?;
    dir.atomic(
        "observation.json",
        &Observation {
            schema: 1,
            root: root.into(),
            id: id.into(),
            state: "pending".into(),
            updated_at: local::now(),
            evidence: json!({"source":"launch"}),
        },
    )?;
    record.state = State::Launching;
    record.started_at = Some(local::now());
    record.package = Some(package.clone());
    record.runtime = Some(runtime_identity);
    record.admission = Some(admission.clone());
    save(home, &dir, &mut record)?;
    checkpoint("intent");
    let output = dir.open_file(
        "executor.log",
        libc::O_WRONLY | libc::O_CREAT | libc::O_APPEND,
    )?;
    let mut command = Command::new(runtime);
    command
        .args(["--package-root"])
        .arg(&package)
        .args(["execution-executor", "--parent", root, "--id", id])
        .env_clear()
        .env("CODEX_HOME", home.canonicalize()?)
        .current_dir(&package)
        .stdin(Stdio::null())
        .stdout(output.try_clone()?)
        .stderr(output);
    for spec in std::iter::once(&record.manifest.command)
        .chain(record.manifest.cleanup.iter())
        .chain(
            record
                .manifest
                .external_dependencies
                .iter()
                .filter_map(|d| d.precondition.as_ref()),
        )
    {
        // References avoid persisted secret values; same-UID /proc readers can inspect live environments.
        for reference in spec
            .spec
            .env_refs
            .values()
            .chain(spec.spec.secret_refs.values())
        {
            command.env(
                reference_key(reference),
                env::var_os(reference)
                    .with_context(|| format!("missing environment reference {reference}"))?,
            );
        }
    }
    unsafe {
        command.pre_exec(|| {
            if libc::setsid() < 0 || libc::prctl(libc::PR_SET_PDEATHSIG, 0) < 0 {
                return Err(io::Error::last_os_error());
            }
            close_inherited()
        });
    }
    match local::spawn_exact_clean(&mut command) {
        Ok(child) => {
            checkpoint("executor_spawn");
            // Executor also publishes its identity under this lock before attempting work.
            record.executor = identity(child.id()).ok();
            save(home, &dir, &mut record)?;
            drop(child);
            drop(lock);
            let deadline = Instant::now() + Duration::from_secs(5);
            loop {
                let current = reconcile(home, root, id)?;
                if current["state"] != "launching" || Instant::now() >= deadline {
                    return Ok(current);
                }
                thread::sleep(Duration::from_millis(20));
            }
        }
        Err(error) => {
            let outcome = Outcome {
                schema: 1,
                id: id.into(),
                root: root.into(),
                state: State::Failed,
                finished_at: local::now(),
                exit_code: None,
                signal: None,
                workload_attempted: false,
                executor: record.executor.clone(),
                workload: record.workload.clone(),
                log_complete: true,
                cleanup: CleanupResult::none(),
                termination: None,
                error: Some(format!("executor spawn failed before workload: {error}")),
            };
            result(home, &dir, &mut record, outcome)?;
            value(&record)
        }
    }
}

struct Capture {
    stop: Arc<AtomicBool>,
    handle: thread::JoinHandle<(bool, bool)>,
}
impl Capture {
    fn finish(self) -> (bool, bool) {
        self.stop.store(true, Ordering::Release);
        self.handle.join().unwrap_or((false, false))
    }
}
fn spawn(
    command: &FrozenCommand,
    dir: &Directory,
    log_name: &str,
) -> Result<(Child, Result<ProcessIdentity>, Capture)> {
    let (executable, actual) = hash_file(&command.executable.path, 1024 * 1024 * 1024)?;
    ensure!(
        actual == command.executable,
        "executable changed after review"
    );
    let environment = environment(command, true)?;
    ensure!(
        !command.spec.argv.is_empty() && command.spec.argv.len() <= 256 && environment.len() <= 256,
        "execution argv/environment exceeds frozen bounds"
    );
    let cwd = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(&command.spec.cwd)?;
    ensure!(
        cwd.metadata()?.dev() == command.cwd_dev && cwd.metadata()?.ino() == command.cwd_ino,
        "command cwd changed since review"
    );
    let arguments: Vec<CString> = command
        .spec
        .argv
        .iter()
        .map(|s| CString::new(s.as_bytes()))
        .collect::<std::result::Result<_, _>>()?;
    let variables: Vec<CString> = environment
        .iter()
        .map(|(k, v)| CString::new(format!("{k}={v}")))
        .collect::<std::result::Result<_, _>>()?;
    let mut pipes = [0; 2];
    ensure!(
        unsafe { libc::pipe2(pipes.as_mut_ptr(), libc::O_CLOEXEC) } == 0,
        "cannot create execution log pipe"
    );
    let input = unsafe { File::from_raw_fd(pipes[0]) };
    let output = unsafe { File::from_raw_fd(pipes[1]) };
    let flags = unsafe { libc::fcntl(input.as_raw_fd(), libc::F_GETFL) };
    ensure!(
        flags >= 0
            && unsafe { libc::fcntl(input.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) }
                >= 0,
        "cannot configure execution log reader"
    );
    let log = dir.open_file(log_name, libc::O_CREAT | libc::O_EXCL | libc::O_WRONLY)?;
    let mut child = Command::new(&command.executable.path);
    child
        .args(&command.spec.argv[1..])
        .env_clear()
        .envs(&environment)
        .stdin(Stdio::null())
        .stdout(output.try_clone()?)
        .stderr(output);
    let fd = executable.as_raw_fd();
    let cwd_fd = cwd.as_raw_fd();
    unsafe {
        child.pre_exec(move || {
            if libc::setpgid(0, 0) < 0 || libc::fchdir(cwd_fd) < 0 {
                return Err(io::Error::last_os_error());
            }
            libc::signal(libc::SIGPIPE, libc::SIG_DFL);
            let mut args = [std::ptr::null(); 257];
            let mut env = [std::ptr::null(); 257];
            for (index, value) in arguments.iter().enumerate() {
                args[index] = value.as_ptr();
            }
            for (index, value) in variables.iter().enumerate() {
                env[index] = value.as_ptr();
            }
            checkpoint("workload_exec");
            libc::fexecve(fd, args.as_ptr(), env.as_ptr());
            Err(io::Error::last_os_error())
        });
    }
    let child = child.spawn()?;
    drop(executable);
    let capture = start_capture(input, log);
    let process = spawn_identity(child.id());
    Ok((child, process, capture))
}
fn spawn_identity(pid: u32) -> Result<ProcessIdentity> {
    #[cfg(test)]
    ensure!(
        !FAULT.with(|f| f.get() == Some("post_spawn_identity")),
        "injected post-spawn identity failure"
    );
    identity(pid)
}
fn start_capture(input: File, log: File) -> Capture {
    start_capture_with_limit(input, log, 64 * 1024 * 1024)
}
fn start_capture_with_limit(input: File, mut log: File, limit: usize) -> Capture {
    let stop = Arc::new(AtomicBool::new(false));
    let reader_stop = stop.clone();
    let handle = thread::spawn(move || {
        let mut input = input;
        let mut complete = true;
        let mut eof = false;
        let mut bytes = [0; 65536];
        let mut drained_after_exit = 0;
        let mut written = 0;
        loop {
            let stopping = reader_stop.load(Ordering::Acquire);
            // A live descendant must not keep successful reads ahead of reviewed cleanup forever.
            if stopping && drained_after_exit >= 1024 * 1024 {
                break;
            }
            match input.read(&mut bytes) {
                Ok(0) => {
                    eof = true;
                    break;
                }
                Ok(n) => {
                    if stopping {
                        drained_after_exit += n;
                    }
                    if complete {
                        let keep = n.min(limit - written);
                        if log.write_all(&bytes[..keep]).is_err() {
                            complete = false;
                        }
                        written += keep;
                        if keep < n {
                            complete = false;
                        }
                    }
                }
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                    if reader_stop.load(Ordering::Acquire) {
                        break;
                    }
                    let mut poll = libc::pollfd {
                        fd: input.as_raw_fd(),
                        events: libc::POLLIN,
                        revents: 0,
                    };
                    unsafe {
                        libc::poll(&mut poll, 1, 100);
                    }
                }
                Err(_) => {
                    complete = false;
                    break;
                }
            }
        }
        let synced = log.sync_all().is_ok();
        (complete && eof && synced, eof)
    });
    Capture { stop, handle }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Cancellation {
    schema: u32,
    root: String,
    id: String,
    requested_at: f64,
}
fn cancelled(dir: &Directory, record: &Record) -> Result<bool> {
    if let Some(request) = dir.optional::<Cancellation>("cancel.json")? {
        ensure!(
            request.schema == 1
                && request.root == record.root
                && request.id == record.id
                && request.requested_at.is_finite(),
            "invalid cancellation identity"
        );
        return Ok(true);
    }
    Ok(false)
}
struct Ran {
    status: ExitStatus,
    log_complete: bool,
    termination: Option<String>,
}
impl Ran {
    fn state(&self, cleanup: &CleanupResult) -> State {
        if self.termination.as_deref() == Some("cancel") {
            State::Cancelled
        } else if self.status.success()
            && self.log_complete
            && cleanup.safe()
            && self.termination.is_none()
        {
            State::Completed
        } else {
            State::Failed
        }
    }
}
fn run(
    home: &Path,
    dir: &Directory,
    record: &mut Record,
    command: &FrozenCommand,
    log_name: &str,
    main: bool,
    accept_cancel: bool,
) -> Result<Ran> {
    let (mut child, process, capture) = spawn(command, dir, log_name)?;
    checkpoint("workload_spawn");
    let process = match process {
        Ok(process) => process,
        Err(error) => {
            // Spawn is irrevocable even when /proc identity cannot be read. Never guess a group.
            let message = format!(
                "command spawned as owned child {} but identity is unavailable: {error:#}",
                child.id()
            );
            let _ = unknown(home, dir, record, message.clone());
            let mut evidence = json!({"schema":1,"root":record.root,"id":record.id,"pid":child.id(),"phase":log_name,"identity_verified":false,"error":message,"exit_code":null,"signal":null,"log_complete":false});
            let _ = dir.atomic("unverified-child.json", &evidence);
            let mut heartbeat = Instant::now();
            let status = loop {
                match child.try_wait() {
                    Ok(Some(status)) => break Ok(status),
                    Err(error) => break Err(error),
                    Ok(None) => {
                        if heartbeat.elapsed() >= Duration::from_secs(5) {
                            let _ = projection(home, record);
                            heartbeat = Instant::now();
                        }
                        thread::sleep(Duration::from_millis(50));
                    }
                }
            };
            let (complete, eof) = capture.finish();
            record.log_complete &= complete;
            record.output_closed &= eof;
            if let Ok(status) = &status {
                evidence["exit_code"] = json!(status.code());
                evidence["signal"] = json!(status.signal());
            }
            evidence["log_complete"] = json!(complete);
            let _ = dir.atomic("unverified-child.json", &evidence);
            // Positive waitpid evidence remains partial until the process identity is reconciled.
            let _ = unknown(home, dir, record, message.clone());
            return Err(anyhow::anyhow!(message));
        }
    };
    record.active_process = Some(process.clone());
    record.processes.push(process.clone());
    if main {
        record.workload = Some(process.clone());
        record.state = State::Running;
        // Once spawn succeeded, persistence/display failure cannot justify replay or abandoning waitpid.
    }
    if let Err(e) = save(home, dir, record) {
        eprintln!("cannot publish process identity: {e:#}");
    }
    checkpoint("workload_identity");
    let start = Instant::now();
    let mut last_heartbeat = Instant::now();
    let mut requested: Option<(Instant, String)> = None;
    let mut escalated: Option<Instant> = None;
    let mut cancel_error = None;
    let status = loop {
        let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
        ensure!(
            unsafe {
                libc::waitid(
                    libc::P_PID,
                    child.id(),
                    &mut info,
                    libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
                )
            } == 0,
            "cannot inspect owned child exit: {}",
            io::Error::last_os_error()
        );
        if unsafe { info.si_pid() } != 0 {
            let has_members = if requested.is_some() {
                live_groups(&BTreeSet::from([(process.pgid, process.sid)]))?
            } else {
                false
            };
            let observe_more = requested.is_some()
                && has_members
                && match escalated {
                    Some(at) => at.elapsed() < Duration::from_secs(1),
                    None => record.manifest.cancel_grace_seconds.is_some(),
                };
            if !observe_more {
                break child.wait()?;
            }
        }
        if requested.is_none() {
            let request = match cancelled(dir, record) {
                Ok(v) => v,
                Err(e) => {
                    let error = format!("Invalid cancellation request; execution continues: {e:#}");
                    if cancel_error.as_ref() != Some(&error) {
                        eprintln!("{error}");
                        cancel_error = Some(error.clone());
                        record.error = Some(error);
                        if let Err(e) = save(home, dir, record) {
                            eprintln!("cannot publish cancellation warning: {e:#}");
                        }
                    }
                    false
                }
            };
            let cause = if accept_cancel && request {
                Some("cancel")
            } else if command
                .spec
                .deadline_seconds
                .is_some_and(|n| start.elapsed() >= Duration::from_secs(n))
            {
                Some("deadline")
            } else {
                None
            };
            if let Some(cause) = cause {
                signal_child(&child, &process, libc::SIGTERM)?;
                requested = Some((Instant::now(), cause.into()));
                if accept_cancel {
                    record.state = State::CancelRequested;
                    if let Err(e) = save(home, dir, record) {
                        eprintln!("cannot publish cancellation: {e:#}");
                    }
                }
            }
        }
        if let Some((at, _)) = &requested {
            // Escalation exists only when its grace was part of the reviewed manifest.
            if escalated.is_none()
                && record
                    .manifest
                    .cancel_grace_seconds
                    .is_some_and(|n| at.elapsed() >= Duration::from_secs(n))
            {
                signal_child(&child, &process, libc::SIGKILL)?;
                escalated = Some(Instant::now());
            }
        }
        if last_heartbeat.elapsed() >= Duration::from_secs(5) {
            if let Err(e) = projection(home, record) {
                eprintln!("execution heartbeat unavailable: {e:#}");
            }
            last_heartbeat = Instant::now();
        }
        thread::sleep(Duration::from_millis(50));
    };
    checkpoint("child_exit");
    record.active_process = None;
    let (log_complete, eof) = capture.finish();
    record.log_complete &= log_complete;
    record.output_closed &= eof;
    Ok(Ran {
        status,
        log_complete,
        termination: requested.map(|(_, s)| s),
    })
}

fn cleanup(home: &Path, dir: &Directory, record: &mut Record) -> CleanupResult {
    let Some(command) = record.manifest.cleanup.clone() else {
        return CleanupResult::none();
    };
    if let Err(error) = validate_snapshots(dir, &record.manifest) {
        return CleanupResult {
            status: "unknown".into(),
            exit_code: None,
            signal: None,
            log_complete: false,
            error: Some(format!("frozen cleanup inputs changed: {error:#}")),
        };
    }
    match run(home, dir, record, &command, "cleanup.log", false, false) {
        Ok(r) => CleanupResult {
            status: if r.status.success() && r.termination.is_none() && r.log_complete {
                "completed"
            } else {
                "failed"
            }
            .into(),
            exit_code: r.status.code(),
            signal: r.status.signal(),
            log_complete: r.log_complete,
            error: r.termination.map(|s| format!("cleanup {s}")),
        },
        Err(e) => CleanupResult {
            status: "unknown".into(),
            exit_code: None,
            signal: None,
            log_complete: false,
            error: Some(format!("cleanup outcome unknown: {e:#}")),
        },
    }
}

fn remaining_groups(record: &Record) -> Result<bool> {
    let boot = fs::read_to_string("/proc/sys/kernel/random/boot_id")?;
    let groups: BTreeSet<_> = record
        .processes
        .iter()
        .filter(|p| p.boot_id == boot.trim())
        .map(|p| (p.pgid, p.sid))
        .collect();
    live_groups(&groups)
}
fn live_groups(groups: &BTreeSet<(i32, i32)>) -> Result<bool> {
    live_groups_at(groups, Path::new("/proc"))
}
fn live_groups_at(groups: &BTreeSet<(i32, i32)>, proc: &Path) -> Result<bool> {
    if groups.is_empty() {
        return Ok(false);
    }
    let mut count = 0;
    for entry in fs::read_dir(proc)? {
        count += 1;
        ensure!(count <= 100000, "process group inspection exceeds bound");
        let Ok(pid) = entry?.file_name().to_string_lossy().parse::<u32>() else {
            continue;
        };
        let text = match fs::read_to_string(proc.join(pid.to_string()).join("stat")) {
            Ok(text) => text,
            // An opened proc stat file can return ESRCH after its process exits.
            Err(e)
                if e.kind() == io::ErrorKind::NotFound || e.raw_os_error() == Some(libc::ESRCH) =>
            {
                continue;
            }
            Err(e) => return Err(e.into()),
        };
        let fields: Vec<_> = text
            .rsplit_once(')')
            .context("invalid process group metadata")?
            .1
            .split_whitespace()
            .collect();
        ensure!(fields.len() > 3, "truncated process group metadata");
        if !matches!(fields[0], "Z" | "X")
            && groups.contains(&(fields[2].parse()?, fields[3].parse()?))
        {
            return Ok(true);
        }
    }
    Ok(false)
}

fn execute_inner(home: &Path, package: &Path, root: &str, id: &str) -> Result<()> {
    let dir = job(home, root, id)?;
    let _executor = Lock::acquire(&dir, "executor.lock", true)?
        .context("executor already owns this execution")?;
    let mut record;
    {
        let _lock = Lock::acquire(&dir, "job.lock", false)?.unwrap();
        record = load(&dir, root, id)?;
        ensure!(
            matches!(record.state, State::Launching | State::CancelRequested),
            "execution UUID is already consumed"
        );
        ensure!(
            record.package.as_deref() == Some(package),
            "executor package binding changed"
        );
        let own = identity(std::process::id())?;
        ensure!(
            record.executor.as_ref().is_none_or(|old| old == &own),
            "executor process identity changed"
        );
        record.executor = Some(own);
        save(home, &dir, &mut record)?;
        checkpoint("executor_identity");
    }
    // Both cwd and open descriptors retain the original package independently of coordinator upgrades.
    let _package_pin = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW)
        .open(package)?;
    let (runtime_pin, actual) = hash_file(&env::current_exe()?, 1024 * 1024 * 1024)?;
    ensure!(
        record.runtime.as_ref() == Some(&actual),
        "executor binary differs from launch binding"
    );
    let _runtime_pin = runtime_pin;
    let mut attempted = false;
    let execution = (|| -> Result<Option<Ran>> {
        validate_inputs(&dir, &record.manifest, true)?;
        ensure!(
            digest(
                &json!({"schema":1,"id":record.id,"root":record.root,"origin":record.origin,"manifest":record.manifest})
            )? == record.review_digest,
            "reviewed inputs or binding changed after launch"
        );
        execution_admission::verify_executor_environment(
            record
                .admission
                .as_ref()
                .context("missing admission evidence")?,
        )?;
        if cancelled(&dir, &record)? {
            return Ok(None);
        }
        let checks: Vec<_> = record
            .manifest
            .external_dependencies
            .iter()
            .filter_map(|d| d.precondition.clone())
            .collect();
        for (i, check) in checks.iter().enumerate() {
            let checked = run(
                home,
                &dir,
                &mut record,
                check,
                &format!("precondition-{i}.log"),
                false,
                true,
            )?;
            if checked.termination.as_deref() == Some("cancel") {
                return Ok(None);
            }
            ensure!(
                checked.status.success() && checked.termination.is_none() && checked.log_complete,
                "external dependency precondition {i} failed (exit={:?}, signal={:?}, log_complete={})",
                checked.status.code(),
                checked.status.signal(),
                checked.log_complete
            );
        }
        if cancelled(&dir, &record)? {
            return Ok(None);
        }
        if !checks.is_empty() {
            validate_inputs(&dir, &record.manifest, true)?;
        }
        // Recheck containment immediately before the single main workload spawn.
        execution_admission::verify_executor_environment(record.admission.as_ref().unwrap())?;
        let command = record.manifest.command.clone();
        attempted = true;
        Ok(Some(run(
            home,
            &dir,
            &mut record,
            &command,
            "stdout.log",
            true,
            true,
        )?))
    })();
    if execution.is_err() && (record.active_process.is_some() || record.state == State::Unknown) {
        let _lock = Lock::acquire(&dir, "job.lock", false)?.unwrap();
        unknown(
            home,
            &dir,
            &mut record,
            format!(
                "process outcome unavailable: {:#}",
                execution.err().unwrap()
            ),
        )?;
        return Ok(());
    }
    let mut cleanup = if record.processes.is_empty() {
        CleanupResult::none()
    } else {
        cleanup(home, &dir, &mut record)
    };
    if cleanup.safe() && !record.output_closed {
        cleanup.status = "unknown".into();
        cleanup.error = Some(
            "output remains open outside the completed command; cleanup is unconfirmed".into(),
        );
    }
    if cleanup.safe() {
        match remaining_groups(&record) {
            Ok(false) => {}
            state => {
                cleanup.status = "unknown".into();
                cleanup.error = Some(match state {
                    Ok(true) => {
                        "workload/check/cleanup process group still has live members".into()
                    }
                    Err(e) => format!("cannot verify remaining process groups: {e:#}"),
                    _ => unreachable!(),
                });
            }
        }
    }
    let (state, exit_code, signal, log_complete, termination, error) = match execution {
        Ok(Some(r)) => {
            let state = r.state(&cleanup);
            (
                state,
                r.status.code(),
                r.status.signal(),
                r.log_complete,
                r.termination,
                None,
            )
        }
        Ok(None) => (
            State::Cancelled,
            None,
            None,
            true,
            Some("cancel".into()),
            None,
        ),
        Err(e) if record.workload.is_some() => {
            let _lock = Lock::acquire(&dir, "job.lock", false)?.unwrap();
            unknown(
                home,
                &dir,
                &mut record,
                format!("workload outcome unavailable: {e:#}"),
            )?;
            return Ok(());
        }
        Err(e) => (
            State::Failed,
            None,
            None,
            true,
            None,
            Some(format!("workload not started: {e:#}")),
        ),
    };
    let outcome = Outcome {
        schema: 1,
        id: id.into(),
        root: root.into(),
        state,
        finished_at: local::now(),
        exit_code,
        signal,
        workload_attempted: attempted,
        executor: record.executor.clone(),
        workload: record.workload.clone(),
        log_complete: log_complete && record.log_complete,
        cleanup,
        termination,
        error,
    };
    let _lock = Lock::acquire(&dir, "job.lock", false)?.unwrap();
    result(home, &dir, &mut record, outcome)
}

pub fn execute(home: &Path, package_root: &Path, root: &str, id: &str) -> i32 {
    match execute_inner(home, package_root, root, id) {
        Ok(()) => 0,
        Err(error) => {
            eprintln!("detached executor failed; no automatic replay: {error:#}");
            // A failed executor may have crossed an unrecorded launch/result boundary.
            if let Ok(dir) = job(home, root, id) {
                if let Ok(Some(_executor)) = Lock::acquire(&dir, "executor.lock", true) {
                    if let Ok(Some(_lock)) = Lock::acquire(&dir, "job.lock", true) {
                        if let Ok(mut record) = load(&dir, root, id) {
                            let ours = record
                                .executor
                                .as_ref()
                                .is_none_or(|p| p.pid == std::process::id());
                            if ours
                                && matches!(
                                    record.state,
                                    State::Launching | State::Running | State::CancelRequested
                                )
                            {
                                let _ = unknown(
                                    home,
                                    &dir,
                                    &mut record,
                                    format!("executor failure: {error:#}"),
                                );
                            }
                        }
                    }
                }
            }
            1
        }
    }
}

#[cfg(test)]
#[path = "tests.rs"]
mod tests;
