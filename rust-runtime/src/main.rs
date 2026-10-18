use std::ffi::OsString;
use std::path::PathBuf;

fn main() {
    let mut args: Vec<OsString> = std::env::args_os().skip(1).collect();
    let executable = std::env::current_exe().expect("runtime executable path unavailable");
    let parent = executable
        .parent()
        .expect("runtime executable has no directory");
    let mut root: PathBuf = if parent.file_name().is_some_and(|name| name == "bin") {
        parent
            .parent()
            .expect("package directory unavailable")
            .to_path_buf()
    } else {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .to_path_buf()
    };
    if args.first().is_some_and(|value| value == "--package-root") {
        let Some(path) = args.get(1).map(PathBuf::from) else {
            eprintln!("codex-plus: --package-root requires a directory");
            std::process::exit(2);
        };
        root = match path.canonicalize() {
            Ok(path) if path.is_dir() => path,
            _ => {
                eprintln!("codex-plus: package directory unavailable");
                std::process::exit(1);
            }
        };
        args.drain(..2);
    }
    let code = match args.first().and_then(|value| value.to_str()) {
        Some("run") => codex_plus_runtime::runner::main(&args),
        Some("park" | "observe" | "supervise") => {
            codex_plus_runtime::background::main(&args, &root)
        }
        Some("advisor") => codex_plus_runtime::advisor::main(&args, &root),
        Some("goal") => codex_plus_runtime::goal_command::main(&args, &root),
        Some("--version") if args.len() == 1 => match std::fs::read(root.join("codex-plus.json"))
            .map_err(anyhow::Error::from)
            .and_then(|bytes| Ok(serde_json::from_slice::<serde_json::Value>(&bytes)?))
            .and_then(|manifest| {
                anyhow::ensure!(
                    manifest["release"].is_string()
                        && manifest["plus_commit"].is_string()
                        && manifest["upstream"]["version"].is_string(),
                    "invalid package identity"
                );
                Ok(manifest)
            }) {
            Ok(manifest) => {
                let release = manifest["release"].as_str().unwrap_or("unknown");
                let commit = manifest["plus_commit"].as_str().unwrap_or("unknown");
                let upstream = manifest["upstream"]["version"]
                    .as_str()
                    .unwrap_or("unknown");
                println!(
                    "codex-plus {release} (Plus {}; upstream {upstream})",
                    commit.chars().take(12).collect::<String>()
                );
                0
            }
            Err(error) => {
                eprintln!("codex-plus: {error}");
                1
            }
        },
        _ => {
            eprintln!("usage: codex-plus-runtime run|park|goal|advisor|observe|supervise …");
            2
        }
    };
    std::process::exit(code);
}
