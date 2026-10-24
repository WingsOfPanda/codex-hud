//! Tool-free Claude consultations and the SSH cancellation boundary.
use super::*;
use sha2::{Digest, Sha256};

pub(super) const MODEL: &str = "claude-opus-5-5";
const EFFORT: &str = "xhigh";
const MAX_CONTEXT_FILE: usize = 128 * 1024;
const MAX_CONTEXT: usize = 512 * 1024;
const MAX_REQUEST: usize = 4 * 1024 * 1024;

pub(super) struct Options {
    pub brief: String,
    pub use_claude: bool,
    pub host: Option<String>,
    pub remote_command: Option<String>,
    pub context_files: Vec<PathBuf>,
}

impl Options {
    pub fn parse(args: &[OsString]) -> Result<Self> {
        let mut options = Self {
            brief: String::new(),
            use_claude: true,
            host: None,
            remote_command: None,
            context_files: Vec::new(),
        };
        if args.first().is_none_or(|arg| arg != "advisor") {
            bail!("invalid arguments");
        }
        let mut brief_seen = false;
        let mut provider_seen = false;
        let mut index = 1;
        while index < args.len() {
            let argument = args[index]
                .to_str()
                .ok_or_else(|| anyhow::anyhow!("invalid arguments"))?;
            let (flag, value) = if let Some((flag, value)) = argument.split_once('=') {
                (flag, OsString::from(value))
            } else {
                index += 1;
                (
                    argument,
                    args.get(index)
                        .cloned()
                        .ok_or_else(|| anyhow::anyhow!("missing option value"))?,
                )
            };
            match flag {
                "--brief" if !brief_seen => {
                    options.brief = value
                        .into_string()
                        .map_err(|_| anyhow::anyhow!("invalid brief"))?;
                    brief_seen = true;
                }
                "--provider" if !provider_seen => {
                    options.use_claude = match value.to_str() {
                        Some("claude") => true,
                        Some("astra") => false,
                        _ => bail!("invalid provider"),
                    };
                    provider_seen = true;
                }
                "--host" if options.host.is_none() => {
                    let host = value
                        .into_string()
                        .map_err(|_| anyhow::anyhow!("invalid_host"))?;
                    validate_host(&host)?;
                    options.host = Some(host);
                }
                "--remote-command" if options.remote_command.is_none() => {
                    let path = value
                        .into_string()
                        .map_err(|_| anyhow::anyhow!("invalid remote command"))?;
                    if path.is_empty() || path.len() > 4096 || path.chars().any(char::is_control) {
                        bail!("invalid remote command");
                    }
                    options.remote_command = Some(path);
                }
                "--context-file" => options.context_files.push(PathBuf::from(value)),
                _ => bail!("invalid or repeated option"),
            }
            index += 1;
        }
        if !brief_seen
            || (!options.use_claude
                && (options.host.is_some()
                    || options.remote_command.is_some()
                    || !options.context_files.is_empty()))
        {
            bail!("brief required; host and context files require Claude");
        }
        Ok(options)
    }
}

fn validate_host(host: &str) -> Result<()> {
    if host.is_empty()
        || host.len() > 253
        || host.starts_with('-')
        || !host
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
    {
        bail!("invalid_host");
    }
    Ok(())
}

pub(super) fn context(paths: &[PathBuf]) -> Result<String> {
    if paths.len() > 8 {
        bail!("invalid_context");
    }
    let mut files = Vec::new();
    let mut total = 0;
    for path in paths {
        let bytes = local::read_regular(path, MAX_CONTEXT_FILE)
            .map_err(|_| anyhow::anyhow!("invalid_context"))?;
        total += bytes.len();
        if total > MAX_CONTEXT {
            bail!("invalid_context");
        }
        let filename = path
            .to_str()
            .filter(|name| name.len() <= 4096)
            .ok_or_else(|| anyhow::anyhow!("invalid_context"))?;
        let contents =
            std::str::from_utf8(&bytes).map_err(|_| anyhow::anyhow!("invalid_context"))?;
        files.push(json!({"filename":filename,"sha256":format!("{:x}",Sha256::digest(&bytes)),"contents":contents}));
    }
    Ok(serde_json::to_string(&files)?)
}

fn worker_identity() -> String {
    // Source identity also matches workers built for a different CPU architecture.
    format!(
        "{:x}",
        Sha256::digest(concat!(
            include_str!("advisor.rs"),
            include_str!("advisor_claude.rs")
        ))
    )
}

fn request(prompt: &str) -> Value {
    json!({"protocol":1,"worker":worker_identity(),"model":MODEL,"effort":EFFORT,"prompt":prompt,
        "request_id":uuid::Uuid::new_v4().to_string(),"prompt_sha256":format!("{:x}",Sha256::digest(prompt.as_bytes()))})
}

pub(super) struct Binding {
    request_id: Value,
    prompt_sha256: Value,
}

impl Binding {
    fn from_request(request: &Value) -> Self {
        Self {
            request_id: request["request_id"].clone(),
            prompt_sha256: request["prompt_sha256"].clone(),
        }
    }
}

fn validate_request(value: &Value) -> Result<&str> {
    if !exact_keys(
        value,
        &[
            "protocol",
            "worker",
            "model",
            "effort",
            "prompt",
            "request_id",
            "prompt_sha256",
        ],
    ) || value["protocol"].as_u64() != Some(1)
        || value["worker"] != worker_identity()
    {
        bail!("worker_version_mismatch");
    }
    if value["model"] != MODEL || value["effort"] != EFFORT {
        bail!("model_mismatch");
    }
    let prompt = value["prompt"]
        .as_str()
        .filter(|prompt| !prompt.is_empty() && prompt.len() <= MAX_REQUEST / 2)
        .ok_or_else(|| anyhow::anyhow!("invalid_brief"))?;
    let id = value["request_id"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("request_mismatch"))?;
    if uuid::Uuid::parse_str(id)
        .ok()
        .is_none_or(|parsed| parsed.to_string() != id)
        || value["prompt_sha256"] != format!("{:x}", Sha256::digest(prompt.as_bytes()))
    {
        bail!("request_mismatch");
    }
    Ok(prompt)
}

fn local_route(host: Option<&str>) -> Result<bool> {
    if let Some(host) = host {
        validate_host(host)?;
        return Ok(host == "local");
    }
    let mut hostname = [0u8; 256];
    if unsafe { libc::gethostname(hostname.as_mut_ptr().cast(), hostname.len()) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let hostname = hostname.split(|byte| *byte == 0).next().unwrap();
    Ok(hostname.eq_ignore_ascii_case(b"XJP-2card-L20-2"))
}

pub(super) fn spawn(
    prompt: &str,
    host: Option<&str>,
    remote_command: Option<&str>,
    output: File,
    error: File,
) -> Result<(AdvisorChild, UnixStream, Binding)> {
    let mut command = if local_route(host)? {
        let mut command = Command::new(std::env::current_exe()?);
        command.args(["advisor", "--remote-worker"]);
        command
    } else {
        let mut command = Command::new("ssh");
        let executable = remote_command.unwrap_or("codex-plus");
        let remote = format!(
            "'{}' advisor --remote-worker",
            executable.replace('\'', "'\\''")
        );
        command.args([
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=2",
            "--",
            host.unwrap_or("xjp-l20-2"),
            &remote,
        ]);
        command
    };
    command.env_remove("CODEX_THREAD_ID");
    let (reader, mut writer) = UnixStream::pair()?;
    writer.set_write_timeout(Some(Duration::from_secs(15)))?;
    let input = File::from(OwnedFd::from(reader));
    let child = AdvisorChild::spawn(&command, input, output, error)?;
    let request = request(prompt);
    let binding = Binding::from_request(&request);
    let mut bytes = serde_json::to_vec(&request)?;
    bytes.push(b'\n');
    if bytes.len() > MAX_REQUEST {
        bail!("invalid_context");
    }
    writer.write_all(&bytes)?;
    // Keep the writer open: EOF after the request is a cancellation signal.
    Ok((child, writer, binding))
}

fn parse_cli(value: &Value) -> Result<Value> {
    if value["type"] != "result" || value["subtype"] != "success" || value["is_error"] != false {
        bail!("claude_failed");
    }
    let usage = value["modelUsage"]
        .as_object()
        .filter(|models| models.len() == 1)
        .ok_or_else(|| anyhow::anyhow!("model_mismatch"))?;
    if !usage.contains_key(MODEL) {
        bail!("model_mismatch");
    }
    for canonical in [
        value.get("canonicalModel"),
        usage[MODEL].get("canonicalModel"),
    ]
    .into_iter()
    .flatten()
    {
        if canonical != MODEL {
            bail!("model_mismatch");
        }
    }
    validate_result(
        value
            .get("structured_output")
            .cloned()
            .ok_or_else(|| anyhow::anyhow!("invalid_result"))?,
    )
}

fn metadata_type(value: Option<&Value>) -> &'static str {
    match value {
        None => "missing",
        Some(Value::Null) => "null",
        Some(Value::Bool(_)) => "boolean",
        Some(Value::Number(_)) => "number",
        Some(Value::String(_)) => "string",
        Some(Value::Array(_)) => "array",
        Some(Value::Object(_)) => "object",
    }
}

fn metadata_string(value: Option<&Value>) -> Option<String> {
    value.and_then(Value::as_str).map(bounded_model_name)
}

fn bounded_model_name(value: &str) -> String {
    value.chars().take(256).collect()
}

fn parse_cli_with_diagnostics(value: Value, diagnostic: &mut impl Write) -> Result<Value> {
    let result = parse_cli(&value);
    if result
        .as_ref()
        .is_err_and(|error| error.to_string() == "model_mismatch")
    {
        let usage = value.get("modelUsage");
        let models: Vec<Value> = usage
            .and_then(Value::as_object)
            .into_iter()
            .flat_map(|models| models.iter().take(8))
            .map(|(model, usage)| {
                json!({"model":bounded_model_name(model),"usage_type":metadata_type(Some(usage)),
                    "canonical_model":metadata_string(usage.get("canonicalModel")),
                    "canonical_model_type":metadata_type(usage.get("canonicalModel"))})
            })
            .collect();
        let metadata = json!({"expected_model":MODEL,
            "model_usage_type":metadata_type(usage),
            "model_count":usage.and_then(Value::as_object).map(|models| models.len()),
            "models":models,
            "canonical_model":metadata_string(value.get("canonicalModel")),
            "canonical_model_type":metadata_type(value.get("canonicalModel"))});
        let _ = writeln!(diagnostic, "advisor model metadata: {metadata}");
    }
    result
}

pub(super) fn validate_envelope(value: Value, binding: &Binding) -> Result<Value> {
    if !exact_keys(
        &value,
        &[
            "protocol",
            "worker",
            "model",
            "effort",
            "result",
            "error",
            "request_id",
            "prompt_sha256",
        ],
    ) || value["protocol"].as_u64() != Some(1)
        || value["worker"] != worker_identity()
    {
        bail!("worker_version_mismatch");
    }
    if value["model"] != MODEL || value["effort"] != EFFORT {
        bail!("model_mismatch");
    }
    if value["request_id"] != binding.request_id || value["prompt_sha256"] != binding.prompt_sha256
    {
        bail!("request_mismatch");
    }
    if !value["error"].is_null() {
        let code = value["error"].as_str().unwrap_or("invalid_result");
        bail!("{}", stable_error(&anyhow::anyhow!("{code}")));
    }
    validate_result(value["result"].clone())
}

fn read_request(mut input: File, deadline: Instant) -> Result<Value> {
    let mut bytes = Vec::new();
    loop {
        if Instant::now() >= deadline {
            bail!("timeout");
        }
        let mut ready = libc::pollfd {
            fd: input.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        if unsafe { libc::poll(&mut ready, 1, 100) } <= 0 {
            continue;
        }
        let mut byte = [0];
        if input.read(&mut byte)? == 0 {
            bail!("interrupted");
        }
        if byte == [b'\n'] {
            break;
        }
        bytes.push(byte[0]);
        if bytes.len() >= MAX_REQUEST {
            bail!("invalid_context");
        }
    }
    Ok(serde_json::from_slice(&bytes)?)
}

fn worker_request() -> Result<Value> {
    // Stdin's userspace buffer would hide request bytes from the cancellation poll.
    let input_fd = unsafe { libc::fcntl(0, libc::F_DUPFD_CLOEXEC, 3) };
    if input_fd < 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let input = unsafe { File::from_raw_fd(input_fd) };
    read_request(input, Instant::now() + Duration::from_secs(30))
}

fn run_worker(request: &Value) -> Result<Value> {
    let prompt = validate_request(request)?;
    let signals = Signals::new()?;
    let temporary = tempfile::tempdir()?;
    let mut input = tempfile::tempfile()?;
    input.write_all(prompt.as_bytes())?;
    input.seek(SeekFrom::Start(0))?;
    let output = tempfile::tempfile()?;
    let mut command = Command::new("claude");
    command
        .args([
            "--print",
            "--safe-mode",
            "--restricted",
            "--strict-mcp-config",
            "--tools",
            "",
            "--no-session-persistence",
            "--model",
            MODEL,
            "--effort",
            EFFORT,
            "--output-format",
            "json",
            "--json-schema",
            &schema().to_string(),
        ])
        .current_dir(temporary.path())
        .env("CODEX_PLUS_ADVISOR_ACTIVE", "1")
        .env_remove("CODEX_THREAD_ID");
    let mut child = AdvisorChild::spawn(
        &command,
        input,
        output.try_clone()?,
        File::options().write(true).open("/dev/stderr")?,
    )?;
    let started = Instant::now();
    loop {
        let mut lifeline = libc::pollfd {
            fd: 0,
            events: libc::POLLIN,
            revents: 0,
        };
        let ready = unsafe { libc::poll(&mut lifeline, 1, 0) };
        if ready > 0 || signals.get() != 0 {
            bail!("interrupted");
        }
        if ready < 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        if output.metadata()?.len() > MAX_RESULT as u64 {
            bail!("invalid_result");
        }
        if let Some(status) = child.try_wait()? {
            if !status.success() {
                bail!("claude_failed");
            }
            let mut output = output;
            output.seek(SeekFrom::Start(0))?;
            let mut bytes = Vec::new();
            output.take(MAX_RESULT as u64 + 1).read_to_end(&mut bytes)?;
            if bytes.len() > MAX_RESULT {
                bail!("invalid_result");
            }
            return parse_cli_with_diagnostics(
                serde_json::from_slice(&bytes).map_err(|_| anyhow::anyhow!("invalid_result"))?,
                &mut std::io::stderr(),
            );
        }
        if started.elapsed() >= TIMEOUT {
            bail!("timeout");
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

pub(super) fn worker_main() -> i32 {
    let request = worker_request();
    let binding = Binding::from_request(request.as_ref().unwrap_or(&Value::Null));
    let result = request.and_then(|request| run_worker(&request));
    let (value, error) = match result {
        Ok(value) => (value, Value::Null),
        Err(error) => {
            let _ = writeln!(std::io::stderr(), "advisor worker: {error}");
            (Value::Null, json!(stable_error(&error)))
        }
    };
    if writeln!(
        std::io::stdout(),
        "{}",
        json!({"protocol":1,"worker":worker_identity(),"model":MODEL,"effort":EFFORT,"result":value,"error":error,
            "request_id":binding.request_id,"prompt_sha256":binding.prompt_sha256})
    )
    .is_ok()
    {
        0
    } else {
        1
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn result() -> Value {
        json!({"verdict":"approve","summary":"Evidence checked.","findings":[],"next_steps":[]})
    }

    #[test]
    fn parses_explicit_provider_and_context_without_shell_interpolation() {
        let args = [
            "advisor",
            "--brief=a $(touch surprise)",
            "--context-file",
            "space name.rs",
            "--host",
            "local",
        ]
        .map(OsString::from);
        let options = Options::parse(&args).unwrap();
        assert!(options.use_claude);
        assert_eq!(options.brief, "a $(touch surprise)");
        assert_eq!(options.context_files, [PathBuf::from("space name.rs")]);
        for host in ["-oProxyCommand=bad", "host;bad", "x y", "user@host", ""] {
            assert!(validate_host(host).is_err());
        }
        assert!(
            Options::parse(&["advisor", "--brief", "x", "--provider", "astra"].map(OsString::from))
                .is_ok()
        );
    }

    #[test]
    fn context_is_explicit_bounded_hashed_utf8_and_regular() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("source.rs");
        fs::write(&path, "technical 汉 constraints").unwrap();
        let framed: Value = serde_json::from_str(&context(&[path.clone()]).unwrap()).unwrap();
        assert_eq!(framed[0]["contents"], "technical 汉 constraints");
        assert_eq!(
            framed[0]["sha256"],
            format!("{:x}", Sha256::digest(fs::read(&path).unwrap()))
        );
        let symlink = directory.path().join("link");
        std::os::unix::fs::symlink(&path, &symlink).unwrap();
        assert!(context(&[symlink]).is_err());
        fs::write(&path, [0xff]).unwrap();
        assert!(context(&[path.clone()]).is_err());
        fs::write(&path, vec![0; MAX_CONTEXT_FILE + 1]).unwrap();
        assert!(context(&[path]).is_err());
    }

    #[test]
    fn worker_identity_and_cli_model_fail_closed() {
        let mut payload = request("brief");
        assert_eq!(validate_request(&payload).unwrap(), "brief");
        payload["worker"] = json!("old");
        assert!(validate_request(&payload).is_err());
        let mut cli = json!({"type":"result","subtype":"success","is_error":false,"modelUsage":{MODEL:{}},"structured_output":result()});
        assert_eq!(parse_cli(&cli).unwrap(), result());
        cli["modelUsage"][MODEL]["canonicalModel"] = json!(MODEL);
        assert_eq!(parse_cli(&cli).unwrap(), result());
        cli["modelUsage"][MODEL]["canonicalModel"] = json!("claude-sonnet-4-6");
        assert_eq!(parse_cli(&cli).unwrap_err().to_string(), "model_mismatch");
        cli["modelUsage"] = json!({"claude-sonnet-4-6":{}});
        assert_eq!(parse_cli(&cli).unwrap_err().to_string(), "model_mismatch");
        cli["is_error"] = json!(true);
        assert_eq!(parse_cli(&cli).unwrap_err().to_string(), "claude_failed");
    }

    fn cli_result() -> Value {
        json!({"type":"result","subtype":"success","is_error":false,
            "modelUsage":{MODEL:{}},"structured_output":result()})
    }

    fn mismatch_metadata(cli: Value) -> (String, Value) {
        let mut output = Vec::new();
        assert_eq!(
            parse_cli_with_diagnostics(cli, &mut output)
                .unwrap_err()
                .to_string(),
            "model_mismatch"
        );
        let line = String::from_utf8(output).unwrap();
        assert_eq!(line.lines().count(), 1);
        let metadata = serde_json::from_str(
            line.trim_end()
                .strip_prefix("advisor model metadata: ")
                .unwrap(),
        )
        .unwrap();
        (line, metadata)
    }

    #[test]
    fn mismatch_diagnostics_explain_wrong_multiple_missing_and_malformed_usage() {
        let mut cli = cli_result();
        cli["modelUsage"] = json!({"wrong-model":{"canonicalModel":"returned-model"}});
        let (_, metadata) = mismatch_metadata(cli.clone());
        assert_eq!(metadata["expected_model"], MODEL);
        assert_eq!(metadata["model_usage_type"], "object");
        assert_eq!(metadata["model_count"], 1);
        assert_eq!(metadata["models"][0]["model"], "wrong-model");
        assert_eq!(metadata["models"][0]["canonical_model"], "returned-model");
        cli["modelUsage"][MODEL] = json!({});
        let (_, metadata) = mismatch_metadata(cli.clone());
        assert_eq!(metadata["model_count"], 2);
        assert_eq!(metadata["models"].as_array().unwrap().len(), 2);
        cli.as_object_mut().unwrap().remove("modelUsage");
        let (_, metadata) = mismatch_metadata(cli.clone());
        assert_eq!(metadata["model_usage_type"], "missing");
        assert!(metadata["model_count"].is_null());
        assert_eq!(metadata["models"], json!([]));
        for (value, kind) in [
            (Value::Null, "null"),
            (json!([]), "array"),
            (json!(true), "boolean"),
            (json!(42), "number"),
            (json!("secret"), "string"),
        ] {
            cli["modelUsage"] = value;
            let (line, metadata) = mismatch_metadata(cli.clone());
            assert_eq!(metadata["model_usage_type"], kind);
            assert!(!line.contains("secret"));
        }
    }

    #[test]
    fn mismatch_diagnostics_preserve_canonical_field_types_without_disclosing_objects() {
        for field_at_top in [true, false] {
            for (canonical, kind, expected) in [
                (json!("wrong-model"), "string", json!("wrong-model")),
                (json!({"private":"secret"}), "object", Value::Null),
                (Value::Null, "null", Value::Null),
            ] {
                let mut cli = cli_result();
                if field_at_top {
                    cli["canonicalModel"] = canonical;
                } else {
                    cli["modelUsage"][MODEL]["canonicalModel"] = canonical;
                }
                let (line, metadata) = mismatch_metadata(cli);
                let metadata = if field_at_top {
                    &metadata
                } else {
                    &metadata["models"][0]
                };
                assert_eq!(metadata["canonical_model"], expected);
                assert_eq!(metadata["canonical_model_type"], kind);
                assert!(!line.contains("private"));
                assert!(!line.contains("secret"));
            }
        }
        let mut cli = cli_result();
        cli["modelUsage"] = json!({"wrong-model":["private"]});
        let (_, metadata) = mismatch_metadata(cli);
        assert_eq!(metadata["models"][0]["usage_type"], "array");
        assert_eq!(metadata["models"][0]["canonical_model_type"], "missing");
        assert_eq!(metadata["canonical_model_type"], "missing");
    }

    #[test]
    fn mismatch_diagnostics_are_bounded_escaped_and_exclude_result_and_usage() {
        let mut cli = cli_result();
        let long_name = format!("\n\r\t\u{1b}{}", "汉".repeat(300));
        cli["canonicalModel"] = json!(long_name);
        cli["modelUsage"] = Value::Object(
            (0..20)
                .map(|index| {
                    (
                        format!("{index:02}{long_name}"),
                        json!({"canonicalModel":long_name,"inputTokens":"PRIVATE_USAGE"}),
                    )
                })
                .collect(),
        );
        cli["structured_output"] = json!({"summary":"PRIVATE_RESULT"});
        cli["result"] = json!("PRIVATE_RESULT");
        cli["prompt"] = json!("PRIVATE_PROMPT");
        let (line, metadata) = mismatch_metadata(cli);
        assert_eq!(metadata["model_count"], 20);
        assert_eq!(metadata["models"].as_array().unwrap().len(), 8);
        assert_eq!(
            metadata["canonical_model"]
                .as_str()
                .unwrap()
                .chars()
                .count(),
            256
        );
        for model in metadata["models"].as_array().unwrap() {
            assert_eq!(model["model"].as_str().unwrap().chars().count(), 256);
            assert_eq!(
                model["canonical_model"].as_str().unwrap().chars().count(),
                256
            );
        }
        assert!(!line.contains("PRIVATE"));
        assert!(!line.contains("inputTokens"));
        assert!(!line[..line.len() - 1].chars().any(char::is_control));
        assert!(line.len() < 16000);
    }

    #[test]
    fn diagnostics_do_not_change_results_or_errors_even_if_writer_fails() {
        struct BrokenWriter;
        impl Write for BrokenWriter {
            fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
                Err(std::io::Error::other("broken diagnostic sink"))
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let mut output = Vec::new();
        assert_eq!(
            parse_cli_with_diagnostics(cli_result(), &mut output).unwrap(),
            result()
        );
        assert!(output.is_empty());
        let mut cli = cli_result();
        cli["is_error"] = json!(true);
        assert_eq!(
            parse_cli_with_diagnostics(cli, &mut output)
                .unwrap_err()
                .to_string(),
            "claude_failed"
        );
        assert!(output.is_empty());
        let mut cli = cli_result();
        cli["structured_output"] = Value::Null;
        assert_eq!(
            parse_cli_with_diagnostics(cli, &mut output)
                .unwrap_err()
                .to_string(),
            "invalid_result"
        );
        assert!(output.is_empty());
        let mut cli = cli_result();
        cli.as_object_mut().unwrap().remove("modelUsage");
        assert_eq!(
            parse_cli_with_diagnostics(cli, &mut BrokenWriter)
                .unwrap_err()
                .to_string(),
            "model_mismatch"
        );
    }

    #[test]
    fn request_and_response_are_bound_to_id_and_prompt_hash() {
        let request = request("specific evidence");
        let binding = Binding::from_request(&request);
        let envelope = json!({"protocol":1,"worker":worker_identity(),"model":MODEL,"effort":EFFORT,
            "request_id":binding.request_id,"prompt_sha256":binding.prompt_sha256,"result":result(),"error":null});
        assert_eq!(
            validate_envelope(envelope.clone(), &binding).unwrap(),
            result()
        );
        for field in ["request_id", "prompt_sha256"] {
            let mut invalid = request.clone();
            invalid[field] = json!("wrong");
            assert_eq!(
                validate_request(&invalid).unwrap_err().to_string(),
                "request_mismatch"
            );
            let mut invalid = envelope.clone();
            invalid[field] = json!("wrong");
            assert_eq!(
                validate_envelope(invalid, &binding)
                    .unwrap_err()
                    .to_string(),
                "request_mismatch"
            );
        }
        let mut invalid = request.clone();
        invalid["prompt"] = json!("changed evidence");
        assert_eq!(
            validate_request(&invalid).unwrap_err().to_string(),
            "request_mismatch"
        );
    }

    #[test]
    fn request_newline_completes_without_eof_and_disconnect_cancels() {
        let (reader, mut writer) = UnixStream::pair().unwrap();
        let payload = request("large explicit context".repeat(1000).as_str());
        writeln!(writer, "{payload}").unwrap();
        let input = File::from(OwnedFd::from(reader));
        assert_eq!(
            read_request(input, Instant::now() + Duration::from_secs(2)).unwrap(),
            payload
        );
        let (reader, writer) = UnixStream::pair().unwrap();
        drop(writer);
        assert_eq!(
            read_request(
                File::from(OwnedFd::from(reader)),
                Instant::now() + Duration::from_secs(1)
            )
            .unwrap_err()
            .to_string(),
            "interrupted"
        );
    }
}
