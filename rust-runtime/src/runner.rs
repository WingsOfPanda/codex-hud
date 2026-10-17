//! Byte-preserving, noninteractive command capture in the native executor's group.
use crate::local;
use std::ffi::OsString;
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, IntoRawFd, RawFd};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{Child, Command, Stdio};
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};
use std::time::{Duration, Instant};

pub(crate) struct Signals {
    pub requested: Arc<AtomicUsize>,
    registrations: Vec<signal_hook::SigId>,
}

impl Signals {
    pub fn new() -> io::Result<Self> {
        let mut this = Self {
            requested: Arc::new(AtomicUsize::new(0)),
            registrations: Vec::new(),
        };
        for signal in [libc::SIGINT, libc::SIGTERM, libc::SIGHUP] {
            let requested = this.requested.clone();
            // Keep the first cancellation; forwarding a group signal also reaches us.
            let id = unsafe {
                signal_hook::low_level::register(signal, move || {
                    let _ = requested.compare_exchange(
                        0,
                        signal as usize,
                        Ordering::SeqCst,
                        Ordering::SeqCst,
                    );
                })
            }?;
            this.registrations.push(id);
        }
        Ok(this)
    }
    pub fn get(&self) -> i32 {
        self.requested.load(Ordering::SeqCst) as i32
    }
    fn request(&self, signal: i32) {
        let _ =
            self.requested
                .compare_exchange(0, signal as usize, Ordering::SeqCst, Ordering::SeqCst);
    }
}

impl Drop for Signals {
    fn drop(&mut self) {
        for id in self.registrations.drain(..) {
            signal_hook::low_level::unregister(id);
        }
    }
}

struct OutputFlags(i32);
impl OutputFlags {
    fn nonblocking() -> io::Result<Self> {
        let flags = unsafe { libc::fcntl(1, libc::F_GETFL) };
        if flags < 0 || unsafe { libc::fcntl(1, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Self(flags))
    }
}
impl Drop for OutputFlags {
    fn drop(&mut self) {
        unsafe {
            libc::fcntl(1, libc::F_SETFL, self.0);
        }
    }
}

struct Cancellation<'a> {
    signals: &'a Signals,
    child: Option<i32>,
    interrupted: Option<Instant>,
}
impl Cancellation<'_> {
    fn tick(&mut self) -> bool {
        let signal = self.signals.get();
        if signal != 0 && self.interrupted.is_none() {
            self.interrupted = Some(Instant::now());
            if let Some(pid) = self.child {
                unsafe {
                    if libc::getpgrp() == libc::getpid() {
                        libc::kill(-libc::getpgrp(), signal);
                    } else {
                        libc::kill(pid, signal);
                    }
                }
            }
        }
        if let Some(start) = self.interrupted {
            if start.elapsed() >= Duration::from_secs(2) {
                if let Some(pid) = self.child {
                    unsafe {
                        if libc::getpgrp() == libc::getpid() {
                            libc::kill(-libc::getpgrp(), libc::SIGKILL);
                        } else {
                            libc::kill(pid, libc::SIGKILL);
                        }
                    }
                }
                return start.elapsed() >= Duration::from_secs(3);
            }
        }
        false
    }
    fn emit(&mut self, mut bytes: &[u8]) {
        while !bytes.is_empty() {
            self.tick();
            let count = unsafe { libc::write(1, bytes.as_ptr().cast(), bytes.len()) };
            if count > 0 {
                bytes = &bytes[count as usize..];
                continue;
            }
            let error = io::Error::last_os_error();
            if count < 0 && error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            if count < 0 && error.kind() == io::ErrorKind::WouldBlock {
                if self.interrupted.is_some() {
                    return;
                }
                poll(1, libc::POLLOUT);
                continue;
            }
            self.signals.request(libc::SIGTERM);
            self.tick();
            return;
        }
    }
}

fn poll(fd: RawFd, events: i16) -> bool {
    let mut entry = libc::pollfd {
        fd,
        events,
        revents: 0,
    };
    unsafe { libc::poll(&mut entry, 1, 100) > 0 }
}

fn combined_child(command: &[OsString]) -> io::Result<(Child, std::fs::File)> {
    let mut descriptors = [0; 2];
    if unsafe { libc::pipe2(descriptors.as_mut_ptr(), libc::O_CLOEXEC) } != 0 {
        return Err(io::Error::last_os_error());
    }
    let input = unsafe { std::fs::File::from_raw_fd(descriptors[0]) };
    let output = unsafe { std::fs::File::from_raw_fd(descriptors[1]) };
    let mut child = Command::new(&command[0]);
    child
        .args(&command[1..])
        .stdin(Stdio::null())
        .stdout(Stdio::from(output.try_clone()?))
        .stderr(Stdio::from(output));
    // The Rust runtime ignores SIGPIPE itself; child programs retain ordinary Unix semantics.
    unsafe {
        child.pre_exec(|| {
            libc::signal(libc::SIGPIPE, libc::SIG_DFL);
            Ok(())
        });
    }
    let child = local::spawn_exact(&mut child)?;
    let flags = unsafe { libc::fcntl(input.as_raw_fd(), libc::F_GETFL) };
    if flags < 0
        || unsafe { libc::fcntl(input.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok((child, input))
}

pub fn main(args: &[OsString]) -> i32 {
    if args.len() < 3 || args[0] != "run" || args[1] != "--" {
        eprintln!("usage: codex-plus run -- <program> [args...]");
        return 2;
    }
    run_command(&args[2..])
}

fn run_command(command: &[OsString]) -> i32 {
    let (path, mut log) = match local::open_log(&local::state_home()) {
        Ok(value) => value,
        Err(error) => {
            eprintln!("codex-plus error: cannot open log: {error}");
            return 74;
        }
    };
    let _flags = match OutputFlags::nonblocking() {
        Ok(flags) => flags,
        Err(error) => {
            eprintln!("codex-plus error: cannot stream output: {error}");
            return 74;
        }
    };
    let signals = match Signals::new() {
        Ok(signals) => signals,
        Err(error) => {
            eprintln!("codex-plus error: cannot install signals: {error}");
            return 74;
        }
    };
    let mut cancel = Cancellation {
        signals: &signals,
        child: None,
        interrupted: None,
    };
    cancel.emit(format!("codex-plus log: {}\n", path.display()).as_bytes());
    if signals.get() != 0 {
        let code = 128 + signals.get();
        cancel.emit(format!("codex-plus result: exit={code} log={}\n", path.display()).as_bytes());
        return code;
    }
    let mut last_byte = b'\n';
    let mut log_error = None;
    let code = match combined_child(command) {
        Err(error) => {
            eprintln!("codex-plus error: cannot launch command: {error}");
            if error.kind() == io::ErrorKind::NotFound {
                127
            } else {
                126
            }
        }
        Ok((mut child, mut input)) => {
            cancel.child = Some(child.id() as i32);
            let mut buffer = [0; 65536];
            loop {
                if cancel.tick() {
                    break;
                }
                if !poll(input.as_raw_fd(), libc::POLLIN) {
                    continue;
                }
                match input.read(&mut buffer) {
                    Ok(0) => break,
                    Ok(count) => {
                        last_byte = buffer[count - 1];
                        if log_error.is_none() {
                            if let Err(error) = log.write_all(&buffer[..count]) {
                                log_error = Some(error);
                            }
                        }
                        cancel.emit(&buffer[..count]);
                    }
                    Err(error)
                        if matches!(
                            error.kind(),
                            io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                        ) => {}
                    Err(error) => {
                        log_error = Some(error);
                        break;
                    }
                }
            }
            drop(input);
            loop {
                cancel.tick();
                match child.try_wait() {
                    Ok(Some(status)) => {
                        break status
                            .code()
                            .unwrap_or_else(|| 128 + status.signal().unwrap_or(0));
                    }
                    Ok(None) => std::thread::sleep(Duration::from_millis(100)),
                    Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
                    Err(error) => {
                        log_error = Some(error);
                        break 74;
                    }
                }
            }
        }
    };
    let descriptor = log.into_raw_fd();
    if unsafe { libc::close(descriptor) } != 0 && log_error.is_none() {
        log_error = Some(io::Error::last_os_error());
    }
    if let Some(error) = &log_error {
        eprintln!("codex-plus error: log incomplete: {error}; command exit={code}");
    }
    let prefix = if last_byte == b'\n' { "" } else { "\n" };
    let suffix = if log_error.is_some() {
        " log_incomplete=true"
    } else {
        ""
    };
    cancel.emit(
        format!(
            "{prefix}codex-plus result: exit={code} log={}{suffix}\n",
            path.display()
        )
        .as_bytes(),
    );
    if log_error.is_some() { 74 } else { code }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::ffi::OsStringExt;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn executable_text_without_shebang_never_runs_as_shell() {
        let directory = tempfile::tempdir().unwrap();
        let program = directory.path().join("program");
        let marker = directory.path().join("executed");
        std::fs::write(&program, format!("touch '{}'\n", marker.display())).unwrap();
        std::fs::set_permissions(&program, std::fs::Permissions::from_mode(0o700)).unwrap();
        let error = combined_child(&[program.into_os_string()]).unwrap_err();
        assert_eq!(error.raw_os_error(), Some(libc::ENOEXEC));
        assert!(!marker.exists());
    }
    #[test]
    fn combined_stream_preserves_argument_and_output_bytes() {
        let bytes = OsString::from_vec(vec![b'x', 0xff]);
        let command = [
            OsString::from("/usr/bin/python3"),
            OsString::from("-c"),
            OsString::from(
                "import os,sys;os.write(1,os.fsencode(sys.argv[1]));os.write(2,b'\\xfe');assert sys.stdin.read()==''",
            ),
            bytes,
        ];
        let (mut child, mut output) = combined_child(&command).unwrap();
        let mut bytes = Vec::new();
        loop {
            let mut chunk = [0; 64];
            match output.read(&mut chunk) {
                Ok(0) => break,
                Ok(count) => bytes.extend_from_slice(&chunk[..count]),
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                    poll(output.as_raw_fd(), libc::POLLIN);
                }
                Err(error) => panic!("{error}"),
            }
        }
        assert!(child.wait().unwrap().success());
        assert_eq!(bytes, b"x\xff\xfe");
    }
}
