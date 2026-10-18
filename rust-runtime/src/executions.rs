//! Version-one execution truth. Watches and callbacks never own these records.
use crate::{execution_admission, local};
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    env,
    ffi::{CString, OsStr},
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::{
            fs::{FileExt, MetadataExt, OpenOptionsExt, PermissionsExt},
            process::{CommandExt, ExitStatusExt},
        },
    },
    path::{Component, Path, PathBuf},
    process::{Child, Command, ExitStatus, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

const JSON_LIMIT: usize = 2 * 1024 * 1024;
const INPUT_LIMIT: usize = 32 * 1024 * 1024;
const RECORD_LIMIT: usize = 4096;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CommandSpec {
    pub argv: Vec<String>,
    pub cwd: PathBuf,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    #[serde(default)]
    pub env_refs: BTreeMap<String, String>,
    #[serde(default)]
    pub secret_refs: BTreeMap<String, String>,
    pub deadline_seconds: Option<u64>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InputSpec {
    pub source: PathBuf,
    pub path: PathBuf,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Dependency {
    pub identity: String,
    pub precondition: Option<CommandSpec>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub schema: u32,
    pub topic: String,
    pub argv: Vec<String>,
    pub cwd: PathBuf,
    #[serde(default)]
    pub inputs: Vec<InputSpec>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    #[serde(default)]
    pub env_refs: BTreeMap<String, String>,
    #[serde(default)]
    pub secret_refs: BTreeMap<String, String>,
    #[serde(default)]
    pub external_dependencies: Vec<Dependency>,
    #[serde(default)]
    pub exclusive_keys: Vec<String>,
    pub expected_duration_seconds: Option<u64>,
    pub deadline_seconds: Option<u64>,
    pub cancel_grace_seconds: Option<u64>,
    pub cleanup: Option<CommandSpec>,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct FileIdentity {
    path: PathBuf,
    dev: u64,
    ino: u64,
    size: u64,
    sha256: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct FrozenCommand {
    spec: CommandSpec,
    executable: FileIdentity,
    cwd_dev: u64,
    cwd_ino: u64,
    env_hashes: BTreeMap<String, String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct FrozenInput {
    source: FileIdentity,
    path: PathBuf,
    sha256: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct FrozenDependency {
    identity: String,
    precondition: Option<FrozenCommand>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct FrozenManifest {
    schema: u32,
    topic: String,
    command: FrozenCommand,
    inputs: Vec<FrozenInput>,
    external_dependencies: Vec<FrozenDependency>,
    exclusive_keys: Vec<String>,
    expected_duration_seconds: Option<u64>,
    cancel_grace_seconds: Option<u64>,
    cleanup: Option<FrozenCommand>,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProcessIdentity {
    pub boot_id: String,
    pub pid: u32,
    pub start_ticks: u64,
    pub pgid: i32,
    pub sid: i32,
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum State {
    Prepared,
    Launching,
    Running,
    CancelRequested,
    Completed,
    Failed,
    Cancelled,
    Unknown,
}
impl State {
    fn terminal(self) -> bool {
        matches!(self, Self::Completed | Self::Failed | Self::Cancelled)
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct CleanupResult {
    status: String,
    exit_code: Option<i32>,
    signal: Option<i32>,
    log_complete: bool,
    error: Option<String>,
}
impl CleanupResult {
    fn none() -> Self {
        Self {
            status: "not_required".into(),
            exit_code: None,
            signal: None,
            log_complete: true,
            error: None,
        }
    }
    fn safe(&self) -> bool {
        self.status == "not_required" || self.status == "completed"
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Outcome {
    schema: u32,
    id: String,
    root: String,
    state: State,
    finished_at: f64,
    exit_code: Option<i32>,
    signal: Option<i32>,
    workload_attempted: bool,
    executor: Option<ProcessIdentity>,
    workload: Option<ProcessIdentity>,
    log_complete: bool,
    cleanup: CleanupResult,
    termination: Option<String>,
    error: Option<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Record {
    schema: u32,
    id: String,
    root: String,
    origin: Value,
    review_digest: String,
    manifest: FrozenManifest,
    state: State,
    created_at: f64,
    updated_at: f64,
    started_at: Option<f64>,
    executor: Option<ProcessIdentity>,
    workload: Option<ProcessIdentity>,
    active_process: Option<ProcessIdentity>,
    processes: Vec<ProcessIdentity>,
    log_complete: bool,
    output_closed: bool,
    package: Option<PathBuf>,
    runtime: Option<FileIdentity>,
    admission: Option<Value>,
    result: Option<Outcome>,
    resolution: Option<Value>,
    error: Option<String>,
}

struct Directory {
    path: PathBuf,
    file: File,
}
struct Preparation {
    path: PathBuf,
    committed: bool,
}
impl Drop for Preparation {
    fn drop(&mut self) {
        if !self.committed {
            let _ = fs::remove_dir_all(&self.path);
        }
    }
}
impl Directory {
    fn open(path: &Path) -> Result<Self> {
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)?;
        ensure!(
            file.metadata()?.uid() == unsafe { libc::geteuid() },
            "execution directory is not owned by this user"
        );
        ensure!(
            file.metadata()?.mode() & 0o077 == 0,
            "execution directory must be private"
        );
        Ok(Self {
            path: path.to_owned(),
            file,
        })
    }
    fn child(&self, name: &str, create: bool) -> Result<Self> {
        component(name)?;
        let name_c = CString::new(name)?;
        if create && unsafe { libc::mkdirat(self.file.as_raw_fd(), name_c.as_ptr(), 0o700) } < 0 {
            let error = io::Error::last_os_error();
            if error.kind() != io::ErrorKind::AlreadyExists {
                return Err(error.into());
            }
        }
        let file = local::open_at(
            self.file.as_raw_fd(),
            OsStr::new(name),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW,
            0,
        )?;
        let metadata = file.metadata()?;
        ensure!(
            metadata.uid() == unsafe { libc::geteuid() } && metadata.mode() & 0o077 == 0,
            "execution directory must be privately owned"
        );
        Ok(Self {
            path: self.path.join(name),
            file,
        })
    }
    fn read<T: DeserializeOwned>(&self, name: &str) -> Result<T> {
        self.read_bounded(name, JSON_LIMIT)
    }
    fn read_bounded<T: DeserializeOwned>(&self, name: &str, max: usize) -> Result<T> {
        let file = self.open_file(name, libc::O_RDONLY | libc::O_NONBLOCK)?;
        ensure!(
            file.metadata()?.is_file(),
            "execution record must be regular"
        );
        ensure!(
            file.metadata()?.len() <= max as u64,
            "execution record exceeds bound"
        );
        let mut bytes = Vec::new();
        file.take((max + 1) as u64).read_to_end(&mut bytes)?;
        ensure!(bytes.len() <= max, "execution record exceeds bound");
        Ok(serde_json::from_slice(&bytes)?)
    }
    fn optional<T: DeserializeOwned>(&self, name: &str) -> Result<Option<T>> {
        match self.read(name) {
            Ok(v) => Ok(Some(v)),
            Err(e)
                if e.downcast_ref::<io::Error>()
                    .is_some_and(|e| e.kind() == io::ErrorKind::NotFound) =>
            {
                Ok(None)
            }
            Err(e) => Err(e),
        }
    }
    fn open_file(&self, name: &str, flags: i32) -> Result<File> {
        component(name)?;
        let file = local::open_at(
            self.file.as_raw_fd(),
            OsStr::new(name),
            flags | libc::O_NOFOLLOW | libc::O_NONBLOCK,
            0o600,
        )?;
        let m = file.metadata()?;
        ensure!(
            m.is_file() && m.uid() == unsafe { libc::geteuid() } && m.mode() & 0o077 == 0,
            "execution file must be privately owned and regular"
        );
        Ok(file)
    }
    fn atomic<T: Serialize>(&self, name: &str, value: &T) -> Result<()> {
        component(name)?;
        let bytes = serde_json::to_vec(value)?;
        ensure!(bytes.len() <= JSON_LIMIT, "execution record exceeds bound");
        let temporary = format!(".{}.tmp", uuid::Uuid::new_v4());
        let mut file = self.open_file(&temporary, libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL)?;
        file.write_all(&bytes)?;
        file.sync_all()?;
        let from = CString::new(temporary)?;
        let to = CString::new(name)?;
        ensure!(
            unsafe {
                libc::renameat(
                    self.file.as_raw_fd(),
                    from.as_ptr(),
                    self.file.as_raw_fd(),
                    to.as_ptr(),
                )
            } == 0,
            "cannot commit execution record: {}",
            io::Error::last_os_error()
        );
        self.file.sync_all()?;
        Ok(())
    }
    fn remove(&self, name: &str) -> Result<()> {
        component(name)?;
        let name = CString::new(name)?;
        if unsafe { libc::unlinkat(self.file.as_raw_fd(), name.as_ptr(), 0) } < 0 {
            let error = io::Error::last_os_error();
            if error.kind() != io::ErrorKind::NotFound {
                return Err(error.into());
            }
        }
        self.file.sync_all()?;
        Ok(())
    }
    fn names(&self) -> Result<Vec<String>> {
        let mut names = Vec::new();
        for entry in fs::read_dir(format!("/proc/self/fd/{}", self.file.as_raw_fd()))? {
            names.push(
                entry?
                    .file_name()
                    .into_string()
                    .map_err(|_| anyhow::anyhow!("invalid execution filename"))?,
            );
            ensure!(
                names.len() <= RECORD_LIMIT,
                "execution directory enumeration exceeds bound"
            );
        }
        names.sort();
        Ok(names)
    }
}
struct Lock(File);
impl Lock {
    fn acquire(dir: &Directory, name: &str, nonblocking: bool) -> Result<Option<Self>> {
        let file = dir.open_file(name, libc::O_CREAT | libc::O_RDWR)?;
        let flags = libc::LOCK_EX | if nonblocking { libc::LOCK_NB } else { 0 };
        loop {
            if unsafe { libc::flock(file.as_raw_fd(), flags) } == 0 {
                return Ok(Some(Self(file)));
            }
            let error = io::Error::last_os_error();
            if nonblocking && error.kind() == io::ErrorKind::WouldBlock {
                return Ok(None);
            }
            if error.kind() != io::ErrorKind::Interrupted {
                return Err(error.into());
            }
        }
    }
}
impl Drop for Lock {
    fn drop(&mut self) {
        unsafe {
            libc::flock(self.0.as_raw_fd(), libc::LOCK_UN);
        }
    }
}

fn component(value: &str) -> Result<()> {
    ensure!(
        !value.is_empty() && value != "." && value != ".." && !value.contains(['/', '\0']),
        "invalid execution path component"
    );
    Ok(())
}
fn uuid(value: &str) -> Result<()> {
    ensure!(
        uuid::Uuid::parse_str(value)?.to_string() == value,
        "identity must be a canonical UUID"
    );
    Ok(())
}
fn base(home: &Path, create: bool) -> Result<Directory> {
    let path = if create {
        local::state_directory(home)?
    } else {
        home.join("codex-plus")
    };
    Directory::open(&path)?
        .child("executions", create)?
        .child("v1", create)
}
fn jobs(home: &Path, root: &str, create: bool) -> Result<Directory> {
    uuid(root)?;
    base(home, create)?.child(root, create)
}
fn job(home: &Path, root: &str, id: &str) -> Result<Directory> {
    uuid(id)?;
    jobs(home, root, false)?.child(id, false)
}
fn load(dir: &Directory, root: &str, id: &str) -> Result<Record> {
    let record: Record = dir.read("state.json")?;
    ensure!(
        record.schema == 1 && record.id == id && record.root == root,
        "execution identity/schema mismatch"
    );
    Ok(record)
}
fn value(record: &Record) -> Result<Value> {
    Ok(serde_json::to_value(record)?)
}
fn digest<T: Serialize>(input: &T) -> Result<String> {
    Ok(format!("{:x}", Sha256::digest(serde_json::to_vec(input)?)))
}
fn hash_file(path: &Path, max: u64) -> Result<(File, FileIdentity)> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)?;
    let identity = identify_file(&file, path, max)?;
    Ok((file, identity))
}
fn identify_file(file: &File, path: &Path, max: u64) -> Result<FileIdentity> {
    let metadata = file.metadata()?;
    ensure!(
        metadata.is_file() && metadata.len() <= max,
        "input is not a bounded regular file"
    );
    let mut hash = Sha256::new();
    let mut count = 0;
    let mut buffer = [0; 65536];
    loop {
        let n = file.read_at(&mut buffer, count)?;
        if n == 0 {
            break;
        }
        count += n as u64;
        ensure!(count <= max, "input exceeds bound");
        hash.update(&buffer[..n]);
    }
    ensure!(count == metadata.len(), "input changed while hashing");
    let after = file.metadata()?;
    ensure!(
        (
            after.len(),
            after.mtime(),
            after.mtime_nsec(),
            after.ctime(),
            after.ctime_nsec()
        ) == (
            metadata.len(),
            metadata.mtime(),
            metadata.mtime_nsec(),
            metadata.ctime(),
            metadata.ctime_nsec()
        ),
        "input changed while hashing"
    );
    Ok(FileIdentity {
        path: path.to_owned(),
        dev: metadata.dev(),
        ino: metadata.ino(),
        size: metadata.len(),
        sha256: format!("{:x}", hash.finalize()),
    })
}
fn valid_environment(name: &str) -> Result<()> {
    ensure!(
        !name.is_empty() && !name.contains(['=', '\0']),
        "invalid environment name"
    );
    Ok(())
}
fn reference_key(reference: &str) -> String {
    format!(
        "CODEX_PLUS_EXEC_REF_{:x}",
        Sha256::digest(reference.as_bytes())
    )
}
fn environment(command: &FrozenCommand, transported: bool) -> Result<BTreeMap<String, String>> {
    let mut result = command.spec.env.clone();
    for (name, reference) in command
        .spec
        .env_refs
        .iter()
        .chain(command.spec.secret_refs.iter())
    {
        let contents = env::var(if transported {
            reference_key(reference)
        } else {
            reference.clone()
        })
        .with_context(|| format!("required environment reference {reference} is unavailable"))?;
        if let Some(expected) = command.env_hashes.get(name) {
            ensure!(
                digest(&contents)? == *expected,
                "environment reference changed since review: {name}"
            );
        }
        result.insert(name.clone(), contents);
    }
    Ok(result)
}
fn freeze_command(mut spec: CommandSpec, inputs: &Path) -> Result<FrozenCommand> {
    ensure!(
        !spec.argv.is_empty() && spec.argv.len() <= 256,
        "command requires bounded argv"
    );
    let input_path = inputs.to_str().context("input path is not UTF-8")?;
    let execution_path = inputs
        .parent()
        .unwrap()
        .to_str()
        .context("execution path is not UTF-8")?;
    let expand = |text: &str| {
        text.replace("{inputs}", input_path)
            .replace("{execution}", execution_path)
    };
    spec.cwd = PathBuf::from(expand(spec.cwd.to_str().context("cwd must be UTF-8")?));
    ensure!(
        spec.cwd.is_absolute() && spec.cwd.is_dir(),
        "command cwd must be an existing absolute directory"
    );
    ensure!(
        spec.argv.iter().map(String::len).sum::<usize>() <= 65536,
        "command arguments exceed bound"
    );
    spec.cwd = spec.cwd.canonicalize()?;
    let cwd = fs::metadata(&spec.cwd)?;
    for argument in &mut spec.argv {
        ensure!(!argument.contains('\0'), "argument contains NUL");
        *argument = expand(argument);
    }
    ensure!(
        spec.argv.iter().map(String::len).sum::<usize>() <= 65536,
        "expanded command arguments exceed bound"
    );
    ensure!(
        Path::new(&spec.argv[0]).is_absolute(),
        "argv[0] must be an absolute executable/interpreter"
    );
    let path = Path::new(&spec.argv[0]).canonicalize()?;
    let (_, executable) = hash_file(&path, 1024 * 1024 * 1024)?;
    let mut header = [0u8; 4];
    File::open(&path)?.read_exact(&mut header)?;
    ensure!(
        &header == b"\x7fELF",
        "argv[0] must be a native executable; use an explicit absolute interpreter for scripts"
    );
    ensure!(
        fs::metadata(&path)?.mode() & 0o111 != 0,
        "entrypoint is not executable"
    );
    spec.argv[0] = path
        .to_str()
        .context("executable path is not UTF-8")?
        .into();
    let mut names = BTreeSet::new();
    let mut total = 0;
    for value in spec.env.values_mut() {
        *value = expand(value);
    }
    for (name, value) in &spec.env {
        valid_environment(name)?;
        ensure!(!value.contains('\0'), "environment contains NUL");
        names.insert(name);
        total += name.len() + value.len();
    }
    let mut env_hashes = BTreeMap::new();
    for (name, reference) in spec.env_refs.iter().chain(spec.secret_refs.iter()) {
        valid_environment(name)?;
        valid_environment(reference)?;
        ensure!(
            names.insert(name),
            "environment destination is duplicated: {name}"
        );
        total += name.len() + reference.len();
        if spec.env_refs.contains_key(name) {
            env_hashes.insert(
                name.clone(),
                digest(
                    &env::var(reference)
                        .with_context(|| format!("missing environment reference {reference}"))?,
                )?,
            );
        }
    }
    ensure!(
        names.len() <= 256 && total <= 65536,
        "environment declaration exceeds bound"
    );
    ensure!(
        spec.deadline_seconds != Some(0),
        "deadline must be positive"
    );
    Ok(FrozenCommand {
        spec,
        executable,
        cwd_dev: cwd.dev(),
        cwd_ino: cwd.ino(),
        env_hashes,
    })
}
fn validate_inputs(dir: &Directory, manifest: &FrozenManifest, transported: bool) -> Result<()> {
    for input in &manifest.inputs {
        let (_, source) = hash_file(&input.source.path, INPUT_LIMIT as u64)?;
        ensure!(
            source == input.source,
            "declared source changed since review: {}",
            input.source.path.display()
        );
    }
    validate_snapshots(dir, manifest)?;
    for command in std::iter::once(&manifest.command)
        .chain(manifest.cleanup.iter())
        .chain(
            manifest
                .external_dependencies
                .iter()
                .filter_map(|d| d.precondition.as_ref()),
        )
    {
        let (_, actual) = hash_file(&command.executable.path, 1024 * 1024 * 1024)?;
        ensure!(
            actual == command.executable,
            "executable changed since review"
        );
        let cwd = fs::metadata(&command.spec.cwd)?;
        ensure!(
            cwd.is_dir() && cwd.dev() == command.cwd_dev && cwd.ino() == command.cwd_ino,
            "command cwd identity changed since review"
        );
        environment(command, transported)?;
    }
    Ok(())
}
fn validate_snapshots(dir: &Directory, manifest: &FrozenManifest) -> Result<()> {
    for input in &manifest.inputs {
        let mut parent = dir.child("inputs", false)?;
        let parts: Vec<_> = input.path.components().collect();
        ensure!(
            !parts.is_empty() && parts.iter().all(|p| matches!(p, Component::Normal(_))),
            "invalid frozen snapshot path"
        );
        for part in &parts[..parts.len() - 1] {
            parent = parent.child(
                part.as_os_str().to_str().context("invalid snapshot path")?,
                false,
            )?;
        }
        let name = parts
            .last()
            .context("empty snapshot path")?
            .as_os_str()
            .to_str()
            .context("invalid snapshot name")?;
        let file = parent.open_file(name, libc::O_RDONLY)?;
        ensure!(
            identify_file(&file, &parent.path.join(name), INPUT_LIMIT as u64)?.sha256
                == input.sha256,
            "frozen input changed since review"
        );
    }
    Ok(())
}

pub fn prepare(home: &Path, root: &str, origin: &Value, manifest: &Value) -> Result<Value> {
    uuid(root)?;
    ensure!(
        origin.is_object() && origin["root"] == root && serde_json::to_vec(origin)?.len() <= 65536,
        "review binding must name the owning root and fit its bound"
    );
    let manifest: Manifest = serde_json::from_value(manifest.clone())?;
    ensure!(
        manifest.schema == 1
            && !manifest.topic.is_empty()
            && manifest.topic.len() <= 200
            && !manifest.topic.chars().any(char::is_control),
        "invalid manifest schema/topic"
    );
    ensure!(
        manifest.inputs.len() <= 256
            && manifest.external_dependencies.len() <= 64
            && manifest.exclusive_keys.len() <= 64,
        "manifest exceeds declaration bounds"
    );
    ensure!(
        manifest.cancel_grace_seconds.is_some()
            || (manifest.deadline_seconds.is_none()
                && manifest.cleanup.is_none()
                && manifest
                    .external_dependencies
                    .iter()
                    .all(|d| d.precondition.is_none())),
        "finite deadlines/checks/cleanup require a reviewed cancellation grace"
    );
    let id = uuid::Uuid::new_v4().to_string();
    let parent = jobs(home, root, true)?;
    let name = CString::new(id.as_str())?;
    ensure!(
        unsafe { libc::mkdirat(parent.file.as_raw_fd(), name.as_ptr(), 0o700) } == 0,
        "cannot create new execution identity: {}",
        io::Error::last_os_error()
    );
    let mut preparation = Preparation {
        path: parent.path.join(&id),
        committed: false,
    };
    let dir = parent.child(&id, false)?;
    let snapshots = dir.child("inputs", true)?;
    let mut inputs = Vec::new();
    let mut seen = BTreeSet::new();
    let mut size = 0;
    for input in &manifest.inputs {
        ensure!(input.source.is_absolute(), "input source must be absolute");
        ensure!(
            !input.path.as_os_str().is_empty()
                && input
                    .path
                    .components()
                    .all(|c| matches!(c, Component::Normal(_))),
            "snapshot path must be relative without traversal"
        );
        ensure!(seen.insert(input.path.clone()), "duplicate snapshot path");
        let (source, identity) = hash_file(&input.source, INPUT_LIMIT as u64)?;
        size += identity.size;
        ensure!(
            size <= 128 * 1024 * 1024,
            "declared snapshots exceed total bound"
        );
        let components: Vec<_> = input.path.components().collect();
        let mut destination = Directory::open(&snapshots.path)?;
        for component in &components[..components.len() - 1] {
            destination = destination.child(
                component
                    .as_os_str()
                    .to_str()
                    .context("snapshot path must be UTF-8")?,
                true,
            )?;
        }
        let name = components
            .last()
            .unwrap()
            .as_os_str()
            .to_str()
            .context("snapshot path must be UTF-8")?;
        let mut copy =
            destination.open_file(name, libc::O_CREAT | libc::O_EXCL | libc::O_WRONLY)?;
        let copied = io::copy(&mut source.take((INPUT_LIMIT + 1) as u64), &mut copy)?;
        ensure!(copied == identity.size, "input changed while copying");
        copy.sync_all()?;
        copy.set_permissions(fs::Permissions::from_mode(0o400))?;
        destination.file.sync_all()?;
        let (_, actual) = hash_file(&snapshots.path.join(&input.path), INPUT_LIMIT as u64)?;
        ensure!(
            actual.sha256 == identity.sha256,
            "input changed while copying"
        );
        inputs.push(FrozenInput {
            source: identity,
            path: input.path.clone(),
            sha256: actual.sha256,
        });
    }
    let command = freeze_command(
        CommandSpec {
            argv: manifest.argv,
            cwd: manifest.cwd,
            env: manifest.env,
            env_refs: manifest.env_refs,
            secret_refs: manifest.secret_refs,
            deadline_seconds: manifest.deadline_seconds,
        },
        &snapshots.path,
    )?;
    let cleanup = manifest
        .cleanup
        .map(|s| {
            ensure!(
                s.deadline_seconds.is_some(),
                "cleanup needs a reviewed finite deadline"
            );
            freeze_command(s, &snapshots.path)
        })
        .transpose()?;
    let mut dependencies = Vec::new();
    for dependency in manifest.external_dependencies {
        ensure!(
            !dependency.identity.is_empty() && dependency.identity.len() <= 4096,
            "invalid dependency identity"
        );
        let precondition = dependency
            .precondition
            .map(|s| {
                ensure!(
                    s.deadline_seconds.is_some(),
                    "precondition needs a reviewed finite deadline"
                );
                freeze_command(s, &snapshots.path)
            })
            .transpose()?;
        dependencies.push(FrozenDependency {
            identity: dependency.identity,
            precondition,
        });
    }
    let mut keys = manifest.exclusive_keys;
    ensure!(
        keys.iter()
            .all(|k| !k.is_empty() && k.len() <= 256 && !k.chars().any(char::is_control)),
        "invalid exclusive resource key"
    );
    keys.sort();
    keys.dedup();
    let frozen = FrozenManifest {
        schema: 1,
        topic: manifest.topic,
        command,
        inputs,
        external_dependencies: dependencies,
        exclusive_keys: keys,
        expected_duration_seconds: manifest.expected_duration_seconds,
        cancel_grace_seconds: manifest.cancel_grace_seconds,
        cleanup,
    };
    let review_digest =
        digest(&json!({"schema":1,"id":id,"root":root,"origin":origin,"manifest":frozen}))?;
    let now = local::now();
    let record = Record {
        schema: 1,
        id,
        root: root.into(),
        origin: origin.clone(),
        review_digest,
        manifest: frozen,
        state: State::Prepared,
        created_at: now,
        updated_at: now,
        started_at: None,
        executor: None,
        workload: None,
        active_process: None,
        processes: Vec::new(),
        log_complete: true,
        output_closed: true,
        package: None,
        runtime: None,
        admission: None,
        result: None,
        resolution: None,
        error: None,
    };
    dir.atomic("manifest.json", &record)?;
    dir.atomic("state.json", &record)?;
    parent.file.sync_all()?;
    executor::checkpoint("prepared");
    preparation.committed = true;
    value(&record)
}

pub fn read(home: &Path, root: &str, id: &str) -> Result<Value> {
    value(&load(&job(home, root, id)?, root, id)?)
}
pub fn roots(home: &Path) -> Result<Vec<String>> {
    let parent = match base(home, false) {
        Ok(dir) => dir,
        Err(error)
            if error
                .downcast_ref::<io::Error>()
                .is_some_and(|e| e.kind() == io::ErrorKind::NotFound) =>
        {
            return Ok(Vec::new());
        }
        Err(error) => return Err(error),
    };
    executor::validate_registry(&parent)?;
    let mut roots = Vec::new();
    for name in parent.names()? {
        if [
            "resources.json",
            "resources.lock",
            "resources-initialized.json",
        ]
        .contains(&name.as_str())
        {
            continue;
        }
        if let Some(temporary) = name.strip_prefix('.').and_then(|v| v.strip_suffix(".tmp")) {
            uuid(temporary)?;
            parent.open_file(&name, libc::O_RDONLY)?;
            continue;
        }
        uuid(&name)?;
        parent.child(&name, false)?;
        roots.push(name);
    }
    Ok(roots)
}
pub fn list(home: &Path, root: &str) -> Result<Vec<Value>> {
    let parent = match jobs(home, root, false) {
        Ok(v) => v,
        Err(e)
            if e.downcast_ref::<io::Error>()
                .is_some_and(|e| e.kind() == io::ErrorKind::NotFound) =>
        {
            return Ok(Vec::new());
        }
        Err(e) => return Err(e),
    };
    let mut result = Vec::new();
    for id in parent.names()? {
        uuid(&id)?;
        result.push(read(home, root, &id)?);
    }
    Ok(result)
}

mod executor;
pub use executor::{
    cancel, execute, launch, mark_observation, read_observation, reconcile, resolve,
};
