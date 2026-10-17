//! Private local files shared by the native commands and coordinator.
use anyhow::{Context, Result, bail};
use serde_json::Value;
use std::{
    collections::BTreeMap,
    env,
    ffi::CString,
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd, RawFd},
        unix::{
            ffi::OsStrExt,
            fs::{OpenOptionsExt, PermissionsExt},
            process::CommandExt,
        },
    },
    path::{Component, Path, PathBuf},
    process::{Child, Command},
    time::{SystemTime, UNIX_EPOCH},
};

struct ExactExec {
    paths: Vec<CString>,
    _arguments: Vec<CString>,
    _environment: Vec<CString>,
    arguments: Vec<*const libc::c_char>,
    environment: Vec<*const libc::c_char>,
}

// The pointers reference immutable CString allocations owned by this same value.
unsafe impl Send for ExactExec {}
unsafe impl Sync for ExactExec {}

impl ExactExec {
    fn capture(command: &Command) -> io::Result<Self> {
        let mut environment: BTreeMap<_, _> = env::vars_os().collect();
        for (name, value) in command.get_envs() {
            if let Some(value) = value {
                environment.insert(name.to_owned(), value.to_owned());
            } else {
                environment.remove(name);
            }
        }
        let program = command.get_program();
        let paths = if program.as_bytes().contains(&b'/') {
            vec![CString::new(program.as_bytes())?]
        } else {
            let search = environment
                .get(std::ffi::OsStr::new("PATH"))
                .map(|p| p.as_bytes())
                .unwrap_or(b"/bin:/usr/bin");
            search
                .split(|b| *b == b':')
                .map(|part| {
                    let mut path = part.to_vec();
                    if !path.is_empty() {
                        path.push(b'/');
                    }
                    path.extend_from_slice(program.as_bytes());
                    CString::new(path).map_err(io::Error::from)
                })
                .collect::<io::Result<Vec<_>>>()?
        };
        let owned_arguments = std::iter::once(program)
            .chain(command.get_args())
            .map(|value| CString::new(value.as_bytes()).map_err(io::Error::from))
            .collect::<io::Result<Vec<_>>>()?;
        let owned_environment = environment
            .into_iter()
            .map(|(name, value)| {
                let mut entry = name.as_bytes().to_vec();
                entry.push(b'=');
                entry.extend_from_slice(value.as_bytes());
                CString::new(entry).map_err(io::Error::from)
            })
            .collect::<io::Result<Vec<_>>>()?;
        let arguments = owned_arguments
            .iter()
            .map(|value| value.as_ptr())
            .chain(std::iter::once(std::ptr::null()))
            .collect();
        let environment = owned_environment
            .iter()
            .map(|value| value.as_ptr())
            .chain(std::iter::once(std::ptr::null()))
            .collect();
        Ok(Self {
            paths,
            _arguments: owned_arguments,
            _environment: owned_environment,
            arguments,
            environment,
        })
    }

    fn exec(&self) -> io::Result<()> {
        let mut first_error = None;
        let mut last_error = libc::ENOENT;
        for path in &self.paths {
            unsafe {
                libc::execve(
                    path.as_ptr(),
                    self.arguments.as_ptr(),
                    self.environment.as_ptr(),
                );
            }
            last_error = io::Error::last_os_error()
                .raw_os_error()
                .unwrap_or(libc::EIO);
            if !matches!(last_error, libc::ENOENT | libc::ENOTDIR) && first_error.is_none() {
                first_error = Some(last_error);
            }
        }
        Err(io::Error::from_raw_os_error(
            first_error.unwrap_or(last_error),
        ))
    }
}

fn prepare_exact(command: &mut Command) -> io::Result<()> {
    let execution = ExactExec::capture(command)?;
    // Run after the caller's hooks and std's descriptor/cwd setup; never reach execvp's shell fallback.
    unsafe {
        command.pre_exec(move || execution.exec());
    }
    Ok(())
}

/// Configure the command fully before calling; product commands inherit their environment.
pub fn spawn_exact(command: &mut Command) -> io::Result<Child> {
    prepare_exact(command)?;
    command.spawn()
}

pub fn exec_exact(command: &mut Command) -> io::Error {
    if let Err(error) = prepare_exact(command) {
        return error;
    }
    command.exec()
}

pub fn state_home() -> PathBuf {
    env::var_os("CODEX_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env::var_os("HOME").unwrap_or_default()).join(".codex"))
}

pub fn state_directory(home: &Path) -> Result<PathBuf> {
    let directory = home.join("codex-plus");
    if fs::symlink_metadata(&directory).is_ok_and(|m| m.file_type().is_symlink()) {
        bail!("background directory must not be a symlink");
    }
    fs::create_dir_all(&directory)?;
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW)
        .open(&directory)?;
    file.set_permissions(fs::Permissions::from_mode(0o700))?;
    Ok(directory)
}

pub(crate) fn open_at(
    directory: RawFd,
    name: &std::ffi::OsStr,
    flags: i32,
    mode: u32,
) -> io::Result<File> {
    let name = CString::new(name.as_bytes())?;
    let fd = unsafe {
        libc::openat(
            directory,
            name.as_ptr(),
            flags | libc::O_CLOEXEC,
            mode as libc::mode_t,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}

pub fn open_log(home: &Path) -> Result<(PathBuf, File)> {
    let absolute = if home.is_absolute() {
        home.to_owned()
    } else {
        env::current_dir()?.join(home)
    };
    let directory = absolute.join("codex-plus/logs");
    let parts: Vec<_> = directory
        .components()
        .filter(|p| !matches!(p, Component::RootDir | Component::CurDir))
        .collect();
    let mut descriptor = File::open("/")?;
    for (index, part) in parts.iter().enumerate() {
        let name = CString::new(part.as_os_str().as_bytes())?;
        let result = unsafe { libc::mkdirat(descriptor.as_raw_fd(), name.as_ptr(), 0o700) };
        if result < 0 && io::Error::last_os_error().kind() != io::ErrorKind::AlreadyExists {
            return Err(io::Error::last_os_error().into());
        }
        descriptor = open_at(
            descriptor.as_raw_fd(),
            part.as_os_str(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW,
            0,
        )?;
        if index + 2 >= parts.len() {
            descriptor.set_permissions(fs::Permissions::from_mode(0o700))?;
        }
    }
    let name = format!("{}.log", uuid::Uuid::new_v4());
    let file = open_at(
        descriptor.as_raw_fd(),
        name.as_ref(),
        libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW,
        0o600,
    )?;
    file.set_permissions(fs::Permissions::from_mode(0o600))?;
    Ok((directory.join(name), file))
}

pub fn write_all(fd: RawFd, mut bytes: &[u8]) -> io::Result<()> {
    while !bytes.is_empty() {
        let count = unsafe { libc::write(fd, bytes.as_ptr().cast(), bytes.len()) };
        if count < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(error);
        }
        if count == 0 {
            return Err(io::Error::new(
                io::ErrorKind::WriteZero,
                "zero-byte log write",
            ));
        }
        bytes = &bytes[count as usize..];
    }
    Ok(())
}

pub fn atomic_json(path: &Path, value: &Value) -> Result<()> {
    let parent = path.parent().context("JSON path has no parent")?;
    let mut temporary = tempfile::Builder::new()
        .prefix(".status-")
        .suffix(".tmp")
        .tempfile_in(parent)?;
    temporary
        .as_file()
        .set_permissions(fs::Permissions::from_mode(0o600))?;
    temporary.write_all(ascii_json(value)?.as_bytes())?;
    temporary.as_file().sync_all()?;
    temporary.persist(path)?;
    File::open(parent)?.sync_all()?;
    Ok(())
}

pub fn read_regular(path: &Path, max: usize) -> Result<Vec<u8>> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    if !file.metadata()?.is_file() {
        bail!("local input must be a regular file");
    }
    let mut bytes = Vec::new();
    file.take(max.saturating_add(1) as u64)
        .read_to_end(&mut bytes)?;
    if bytes.len() > max {
        bail!("local input exceeds read bound");
    }
    Ok(bytes)
}

pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

pub fn ascii_json(value: &Value) -> Result<String> {
    let text = serde_json::to_string(value)?;
    let mut result = String::with_capacity(text.len());
    for character in text.chars() {
        if character >= '\u{7f}' {
            use std::fmt::Write;
            for unit in character.encode_utf16(&mut [0; 2]) {
                write!(&mut result, "\\u{unit:04x}")?;
            }
        } else {
            result.push(character);
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::process::Stdio;
    #[test]
    fn exact_spawn_rejects_text_and_preserves_shebang_arguments_env_cwd_and_hooks() {
        const CHILD: &str = "CODEX_PLUS_EXACT_SPAWN_TEST";
        if env::var_os(CHILD).is_none() {
            // Other test threads can fork while a fixture is writable, transiently causing ETXTBSY.
            let output=Command::new(env::current_exe().unwrap()).args(["--exact","local::tests::exact_spawn_rejects_text_and_preserves_shebang_arguments_env_cwd_and_hooks"]).env(CHILD,"1").output().unwrap();
            assert!(
                output.status.success(),
                "stdout={} stderr={}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            );
            return;
        }
        let temp = tempfile::tempdir().unwrap();
        let executable = temp.path().join("script");
        let marker = temp.path().join("unexpected-shell");
        fs::write(
            &executable,
            format!("printf executed > {}\n", marker.display()),
        )
        .unwrap();
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o755)).unwrap();
        let error = spawn_exact(&mut Command::new(&executable)).unwrap_err();
        assert_eq!(error.raw_os_error(), Some(libc::ENOEXEC));
        assert!(!marker.exists());
        fs::write(
            &executable,
            b"#!/bin/sh\nprintf '%s\\n' \"$1\" \"$EXACT_KEEP\" \"${HOME-unset}\"\npwd\n",
        )
        .unwrap();
        let mut command = Command::new(&executable);
        command
            .arg(std::ffi::OsStr::from_bytes(b"argument\xff"))
            .env("EXACT_KEEP", "present")
            .env_remove("HOME")
            .current_dir(temp.path())
            .stdout(Stdio::piped());
        unsafe {
            command.pre_exec(|| {
                let bytes = b"hook\n";
                if libc::write(libc::STDOUT_FILENO, bytes.as_ptr().cast(), bytes.len())
                    != bytes.len() as isize
                {
                    return Err(io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let output = spawn_exact(&mut command)
            .unwrap()
            .wait_with_output()
            .unwrap();
        assert!(output.status.success());
        let mut expected = b"hook\nargument\xff\npresent\nunset\n".to_vec();
        expected.extend_from_slice(temp.path().as_os_str().as_bytes());
        expected.push(b'\n');
        assert_eq!(output.stdout, expected);
        let mut command = Command::new("/bin/true");
        assert!(spawn_exact(&mut command).unwrap().wait().unwrap().success());
    }
    #[test]
    fn exact_path_search_continues_after_enoexec_and_retains_first_real_error() {
        let temp = tempfile::tempdir().unwrap();
        let first = temp.path().join("first");
        let second = temp.path().join("second");
        fs::create_dir(&first).unwrap();
        fs::create_dir(&second).unwrap();
        fs::write(first.join("program"), b"exit 99\n").unwrap();
        fs::set_permissions(first.join("program"), fs::Permissions::from_mode(0o755)).unwrap();
        std::os::unix::fs::symlink("/bin/true", second.join("program")).unwrap();
        let mut search = first.as_os_str().to_owned();
        search.push(":");
        search.push(&second);
        let mut command = Command::new("program");
        command.env("PATH", &search);
        assert!(spawn_exact(&mut command).unwrap().wait().unwrap().success());
        fs::remove_file(second.join("program")).unwrap();
        let mut command = Command::new("program");
        command.env("PATH", &search);
        assert_eq!(
            spawn_exact(&mut command).unwrap_err().raw_os_error(),
            Some(libc::ENOEXEC)
        );
        fs::set_permissions(first.join("program"), fs::Permissions::from_mode(0o600)).unwrap();
        let mut command = Command::new("program");
        command.env("PATH", search);
        assert_eq!(
            spawn_exact(&mut command).unwrap_err().raw_os_error(),
            Some(libc::EACCES)
        );
    }
    #[test]
    fn inventory_json_matches_python() {
        assert_eq!(
            ascii_json(&json!({"é/😀": {"size": 12, "mode": "0755"}, "a": "\u{7f}\n"})).unwrap(),
            "{\"a\":\"\\u007f\\n\",\"\\u00e9/\\ud83d\\ude00\":{\"mode\":\"0755\",\"size\":12}}"
        );
    }
    #[test]
    fn logs_are_private_and_refuse_symlinks() {
        let temp = tempfile::tempdir().unwrap();
        let (path, file) = open_log(temp.path()).unwrap();
        assert_eq!(file.metadata().unwrap().permissions().mode() & 0o777, 0o600);
        assert_eq!(
            path.parent()
                .unwrap()
                .metadata()
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        write_all(file.as_raw_fd(), b"binary\0\xff").unwrap();
        assert_eq!(fs::read(path).unwrap(), b"binary\0\xff");
        let other = tempfile::tempdir().unwrap();
        std::os::unix::fs::symlink(temp.path(), other.path().join("linked")).unwrap();
        assert!(open_log(&other.path().join("linked")).is_err());
    }
    #[test]
    fn bounded_regular_and_atomic_json() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("state");
        atomic_json(&path, &json!({"ok":true})).unwrap();
        assert!(read_regular(&path, 1).is_err());
        assert_eq!(read_regular(&path, 100).unwrap(), b"{\"ok\":true}");
        let link = temp.path().join("link");
        std::os::unix::fs::symlink(&path, &link).unwrap();
        assert!(read_regular(&link, 100).is_err());
    }
}
