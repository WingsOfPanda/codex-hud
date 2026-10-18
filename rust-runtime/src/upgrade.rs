//! Select and re-execute an adjacent immutable native coordinator package.
use anyhow::{Context, Result, bail};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    ffi::OsString,
    fs::{self, File, OpenOptions},
    io::{self, Read},
    os::unix::{
        ffi::OsStrExt,
        fs::{MetadataExt, OpenOptionsExt},
    },
    path::{Path, PathBuf},
    process::Command,
};

pub const COORDINATOR_PROTOCOL: u64 = 5;
pub const UPGRADE_EXIT: i32 = 75;
const RUNTIME: &str = "bin/codex-plus-runtime";

fn manifest(root: &Path) -> Result<Value> {
    let value: Value = serde_json::from_slice(&crate::local::read_regular(
        &root.join("codex-plus.json"),
        1024 * 1024,
    )?)?;
    if !value.is_object() {
        bail!("manifest must be an object");
    }
    Ok(value)
}

fn version(manifest: &Value) -> Result<Vec<(usize, String)>> {
    let upstream = manifest["upstream"]["version"]
        .as_str()
        .filter(|s| !s.is_empty())
        .context("missing coordinator upstream version")?;
    let release = manifest["release"]
        .as_str()
        .context("missing coordinator release version")?;
    let rest = release
        .strip_prefix(&format!("{upstream}-plus."))
        .context("invalid coordinator release version")?;
    let components: Vec<_> = rest.split('.').collect();
    if components.len() != 3
        || components.iter().any(|s| {
            s.is_empty()
                || (s.len() > 1 && s.starts_with('0'))
                || !s.bytes().all(|c| c.is_ascii_digit())
        })
    {
        bail!("invalid coordinator release version");
    }
    Ok(components
        .into_iter()
        .map(|s| (s.len(), s.to_owned()))
        .collect())
}

fn identity(info: &fs::Metadata) -> (u64, u64, u64, u32, i64, i64, i64, i64) {
    (
        info.dev(),
        info.ino(),
        info.len(),
        info.mode(),
        info.mtime(),
        info.mtime_nsec(),
        info.ctime(),
        info.ctime_nsec(),
    )
}

fn validate_runtime(candidate: &Path, manifest: &Value) -> Result<()> {
    let files = manifest["files"]
        .as_object()
        .filter(|f| !f.is_empty())
        .context("missing package inventory")?;
    let digest = format!(
        "{:x}",
        Sha256::digest(crate::local::ascii_json(&manifest["files"])?.as_bytes())
    );
    if manifest["tree_sha256"] != digest {
        bail!("invalid package tree identity");
    }
    let entry = files
        .get(RUNTIME)
        .filter(|e| e.is_object())
        .context("missing native runtime inventory")?;
    let directory = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(candidate.join("bin"))?;
    use std::os::fd::AsRawFd;
    let mut file = crate::local::open_at(
        directory.as_raw_fd(),
        "codex-plus-runtime".as_ref(),
        libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK,
        0,
    )?;
    let before = file.metadata()?;
    let mode = entry["mode"]
        .as_str()
        .context("invalid native runtime mode")?;
    if mode.len() != 4 || !mode.bytes().all(|b| (b'0'..=b'7').contains(&b)) {
        bail!("invalid native runtime mode");
    }
    let expected = u32::from_str_radix(mode, 8)?;
    let actual = before.mode() & 0o7777;
    if !before.is_file()
        || actual & 0o111 == 0
        || (actual != expected && actual != expected & !0o222)
        || entry["size"].as_u64() != Some(before.len())
    {
        bail!("native runtime integrity mismatch");
    }
    let mut hash = Sha256::new();
    let mut buffer = [0; 65536];
    let mut header = Vec::new();
    let mut size = 0u64;
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        if header.len() < 20 {
            header.extend_from_slice(&buffer[..count.min(20 - header.len())]);
        }
        hash.update(&buffer[..count]);
        size += count as u64;
    }
    if size != before.len()
        || entry["sha256"] != format!("{:x}", hash.finalize())
        || identity(&file.metadata()?) != identity(&before)
        || identity(&fs::symlink_metadata(candidate.join(RUNTIME))?) != identity(&before)
    {
        bail!("native runtime integrity mismatch");
    }
    let target = manifest["target"]
        .as_str()
        .context("missing native target")?;
    let machine = if target.starts_with("x86_64-") {
        62
    } else if target.starts_with("aarch64-") {
        183
    } else {
        bail!("unsupported native runtime target");
    };
    if header.len() < 20
        || &header[..6] != b"\x7fELF\x02\x01"
        || u16::from_le_bytes([header[18], header[19]]) != machine
    {
        bail!("invalid native runtime ELF architecture");
    }
    Ok(())
}

pub fn select_package(root: &Path) -> Result<Option<PathBuf>> {
    let root = root.canonicalize()?;
    let parent = root.parent().context("package has no parent")?;
    let current = parent.join("current");
    if !root.join("codex-plus.json").exists() {
        return Ok(None);
    }
    if !fs::symlink_metadata(&current).is_ok_and(|m| m.file_type().is_symlink()) {
        if current.exists() {
            bail!("current must be a symlink");
        }
        return Ok(None);
    }
    let installed = manifest(&root)?;
    let link = fs::read_link(&current)?;
    let candidate = if link.is_absolute() {
        link
    } else {
        parent.join(link)
    };
    if candidate.parent() != Some(parent)
        || fs::symlink_metadata(&candidate)?.file_type().is_symlink()
        || !candidate.is_dir()
    {
        bail!("current must point directly to a sibling package");
    }
    if candidate == root {
        return Ok(None);
    }
    let manifest = manifest(&candidate)?;
    if manifest["schema_version"].as_u64() != Some(1)
        || manifest["coordinator_protocol"].as_u64() != Some(COORDINATOR_PROTOCOL)
        || installed["coordinator_protocol"].as_u64() != Some(COORDINATOR_PROTOCOL)
        || manifest["execution_record_format"].as_u64() != Some(1)
        || installed["execution_record_format"].as_u64() != Some(1)
        || installed["upstream"]
            .as_object()
            .is_none_or(|u| u.is_empty())
        || manifest["upstream"] != installed["upstream"]
        || installed["target"].as_str().is_none_or(str::is_empty)
        || manifest["target"] != installed["target"]
    {
        bail!("incompatible coordinator package");
    }
    if version(&manifest)? <= version(&installed)? {
        bail!("coordinator release must be newer than the running package");
    }
    validate_runtime(&candidate, &manifest)?;
    Ok(Some(candidate))
}

pub fn package_command(
    root: &Path,
    home: &Path,
    action: &str,
    parent: &str,
    argv: Option<&[OsString]>,
) -> Command {
    let fallback = [
        OsString::from(action),
        OsString::from("--parent"),
        OsString::from(parent),
    ];
    let mut arguments = Vec::new();
    let mut source = argv.unwrap_or(&fallback).iter();
    while let Some(value) = source.next() {
        if value == "--package-root" {
            source.next();
        } else if !value.as_bytes().starts_with(b"--package-root=") {
            arguments.push(value.clone());
        }
    }
    if action != "park" {
        let mut pin = root.as_os_str().to_owned();
        pin.push("/");
        arguments.push("--package-root".into());
        arguments.push(pin);
    }
    let installed = root.join(RUNTIME);
    let executable = if installed.is_file() {
        installed
    } else {
        root.join(".build/codex-plus-runtime")
    };
    let mut command = Command::new(executable);
    command
        .args(arguments)
        .current_dir(root)
        .env("CODEX_HOME", home);
    command
}

pub fn exec_package(
    root: &Path,
    home: &Path,
    action: &str,
    parent: &str,
    argv: Option<&[OsString]>,
) -> io::Error {
    // Command's exec changes this process's cwd before execve; retain a directory fd for failure.
    let previous = match File::open(".") {
        Ok(file) => file,
        Err(error) => return error,
    };
    let error = crate::local::exec_exact(&mut package_command(root, home, action, parent, argv));
    use std::os::fd::AsRawFd;
    if unsafe { libc::fchdir(previous.as_raw_fd()) } < 0 {
        return io::Error::new(
            io::Error::last_os_error().kind(),
            format!(
                "{error}; cannot restore working directory: {}",
                io::Error::last_os_error()
            ),
        );
    }
    error
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::os::unix::fs::PermissionsExt;
    fn seal(root: &Path, release: &str, payload: &[u8]) {
        fs::create_dir_all(root.join("bin")).unwrap();
        fs::write(root.join(RUNTIME), payload).unwrap();
        fs::set_permissions(root.join(RUNTIME), fs::Permissions::from_mode(0o755)).unwrap();
        let files = json!({RUNTIME:{"size":payload.len(),"mode":"0755","sha256":format!("{:x}",Sha256::digest(payload))}});
        let manifest = json!({"schema_version":1,"coordinator_protocol":COORDINATOR_PROTOCOL,"execution_record_format":1,"release":release,"upstream":{"version":"fixture"},"target":"x86_64-unknown-linux-gnu","tree_sha256":format!("{:x}",Sha256::digest(crate::local::ascii_json(&files).unwrap().as_bytes())),"files":files});
        fs::write(
            root.join("codex-plus.json"),
            serde_json::to_vec(&manifest).unwrap(),
        )
        .unwrap();
    }
    fn elf() -> Vec<u8> {
        let mut bytes = vec![0; 64];
        bytes[..6].copy_from_slice(b"\x7fELF\x02\x01");
        bytes[18] = 62;
        bytes
    }
    #[test]
    fn native_upgrade_integrity_and_newer_version() {
        let temp = tempfile::tempdir().unwrap();
        let old = temp.path().join("old");
        let new = temp.path().join("new");
        seal(&old, "fixture-plus.0.3.9", &elf());
        let mut large = elf();
        large.resize(3 * 1024 * 1024, 0);
        seal(&new, "fixture-plus.0.3.10", &large);
        std::os::unix::fs::symlink("new", temp.path().join("current")).unwrap();
        assert_eq!(select_package(&old).unwrap(), Some(new.clone()));
        fs::set_permissions(new.join(RUNTIME), fs::Permissions::from_mode(0o555)).unwrap();
        assert!(select_package(&old).unwrap().is_some());
        fs::set_permissions(new.join(RUNTIME), fs::Permissions::from_mode(0o755)).unwrap();
        fs::write(new.join(RUNTIME), b"modified").unwrap();
        assert!(select_package(&old).is_err());
    }
    #[test]
    fn refuses_symlink_runtime() {
        let temp = tempfile::tempdir().unwrap();
        let old = temp.path().join("old");
        let new = temp.path().join("new");
        seal(&old, "fixture-plus.0.3.0", &elf());
        seal(&new, "fixture-plus.0.3.1", &elf());
        std::os::unix::fs::symlink("new", temp.path().join("current")).unwrap();
        fs::remove_file(new.join(RUNTIME)).unwrap();
        std::os::unix::fs::symlink(old.join(RUNTIME), new.join(RUNTIME)).unwrap();
        assert!(select_package(&old).is_err());
    }
    #[test]
    fn protocol_three_requires_a_quiet_cutover_before_detached_observation() {
        let temp = tempfile::tempdir().unwrap();
        let old = temp.path().join("old");
        let new = temp.path().join("new");
        seal(&old, "fixture-plus.0.3.0", &elf());
        seal(&new, "fixture-plus.0.3.1", &elf());
        std::os::unix::fs::symlink("new", temp.path().join("current")).unwrap();
        let mut installed = manifest(&old).unwrap();
        installed["coordinator_protocol"] = json!(3);
        fs::write(
            old.join("codex-plus.json"),
            serde_json::to_vec(&installed).unwrap(),
        )
        .unwrap();
        assert!(select_package(&old).is_err());
    }
    #[test]
    fn refuses_incompatible_identity_and_noncanonical_or_older_releases() {
        let temp = tempfile::tempdir().unwrap();
        let old = temp.path().join("old");
        let new = temp.path().join("new");
        seal(&old, "fixture-plus.0.3.0", &elf());
        seal(&new, "fixture-plus.0.3.1", &elf());
        std::os::unix::fs::symlink("new", temp.path().join("current")).unwrap();
        let valid = manifest(&new).unwrap();
        for (key, value) in [
            ("coordinator_protocol", json!(1)),
            ("coordinator_protocol", json!(2)),
            ("coordinator_protocol", json!(3)),
            ("coordinator_protocol", json!(true)),
            ("execution_record_format", json!(2)),
            ("execution_record_format", Value::Null),
            ("target", json!("different")),
            ("upstream", json!({"version":"other"})),
            ("release", json!("fixture-plus.0.3.0")),
            ("release", json!("fixture-plus.0.03.1")),
            ("release", json!("fixture-plus.0.3.1\n")),
            ("tree_sha256", json!("wrong")),
        ] {
            let mut bad = valid.clone();
            bad[key] = value;
            fs::write(
                new.join("codex-plus.json"),
                serde_json::to_vec(&bad).unwrap(),
            )
            .unwrap();
            assert!(select_package(&old).is_err(), "{key}");
        }
    }
    #[test]
    fn command_pins_package_and_preserves_environment() {
        let command = package_command(
            Path::new("/pkg"),
            Path::new("/home"),
            "observe",
            "parent",
            Some(&[
                "observe".into(),
                "--package-root=stale".into(),
                "--parent".into(),
                "parent".into(),
            ]),
        );
        assert_eq!(command.get_program(), "/pkg/.build/codex-plus-runtime");
        assert_eq!(
            command.get_args().collect::<Vec<_>>(),
            ["observe", "--parent", "parent", "--package-root", "/pkg/"]
        );
        assert_eq!(command.get_current_dir(), Some(Path::new("/pkg")));
        assert_eq!(command.get_envs().count(), 1);
    }
    #[test]
    fn failed_exec_restores_cwd() {
        const CHILD: &str = "CODEX_PLUS_EXEC_FAILURE_TEST";
        if std::env::var_os(CHILD).is_some() {
            let temp = tempfile::tempdir().unwrap();
            let previous = std::env::current_dir().unwrap();
            let error = exec_package(temp.path(), temp.path(), "observe", "parent", None);
            assert_eq!(error.kind(), io::ErrorKind::NotFound);
            assert_eq!(std::env::current_dir().unwrap(), previous);
            return;
        }
        let output = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "upgrade::tests::failed_exec_restores_cwd"])
            .env(CHILD, "1")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stdout)
        );
    }
    #[test]
    fn header_valid_invalid_elf_returns_to_same_observer_and_preserves_cwd() {
        const CHILD: &str = "CODEX_PLUS_INVALID_ELF_TEST";
        if std::env::var_os(CHILD).is_some() {
            let temp = tempfile::tempdir().unwrap();
            let old = temp.path().join("old");
            let new = temp.path().join("new");
            seal(&old, "fixture-plus.0.3.0", &elf());
            seal(&new, "fixture-plus.0.3.1", &elf());
            std::os::unix::fs::symlink("new", temp.path().join("current")).unwrap();
            let candidate = select_package(&old).unwrap().unwrap();
            let previous = std::env::current_dir().unwrap();
            let pid = std::process::id();
            let error = exec_package(&candidate, temp.path(), "observe", "parent", None);
            assert_eq!(error.raw_os_error(), Some(libc::ENOEXEC));
            assert_eq!(std::process::id(), pid);
            assert_eq!(std::env::current_dir().unwrap(), previous);
            return;
        }
        let output=Command::new(std::env::current_exe().unwrap()).args(["--exact","upgrade::tests::header_valid_invalid_elf_returns_to_same_observer_and_preserves_cwd"]).env(CHILD,"1").output().unwrap();
        assert!(
            output.status.success(),
            "stdout={} stderr={}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
    }
}
