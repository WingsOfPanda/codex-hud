//! Deadline-bounded JSON RPC over the stock daemon's Unix WebSocket.
use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use std::{
    fs,
    io::{self, Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::{ffi::OsStrExt, net::UnixStream},
    },
    path::Path,
    time::{Duration, Instant},
};
use tungstenite::{Message, WebSocket, protocol::WebSocketConfig};

const MESSAGE_BYTES: usize = 16 * 1024 * 1024;

#[derive(Debug)]
pub struct RpcError {
    pub method: String,
    pub error: Value,
}
impl std::fmt::Display for RpcError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{}: {}",
            self.method,
            self.error["message"].as_str().unwrap_or("RPC error")
        )
    }
}
impl std::error::Error for RpcError {}

struct DeadlineStream {
    socket: UnixStream,
    deadline: Instant,
    header: Option<Vec<u8>>,
}
impl DeadlineStream {
    fn remaining(&self) -> io::Result<Duration> {
        self.deadline
            .checked_duration_since(Instant::now())
            .filter(|d| !d.is_zero())
            .ok_or_else(|| io::Error::new(io::ErrorKind::TimedOut, "daemon response timed out"))
    }
}
impl Read for DeadlineStream {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        self.socket.set_read_timeout(Some(self.remaining()?))?;
        let count = self.socket.read(bytes)?;
        if let Some(header) = &mut self.header {
            header.extend_from_slice(&bytes[..count]);
            let end = header.windows(4).position(|w| w == b"\r\n\r\n");
            if end.unwrap_or(header.len()) > 8192 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "oversized handshake",
                ));
            }
            if end.is_some() {
                self.header = None;
            }
        }
        Ok(count)
    }
}
impl Write for DeadlineStream {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.socket.set_write_timeout(Some(self.remaining()?))?;
        self.socket.write(bytes)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.remaining()?;
        self.socket.flush()
    }
}

fn connect_socket(path: &Path, deadline: Instant) -> io::Result<UnixStream> {
    let bytes = path.as_os_str().as_bytes();
    let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    if bytes.len() >= address.sun_path.len() || bytes.contains(&0) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "invalid Unix socket path",
        ));
    }
    address.sun_family = libc::AF_UNIX as libc::sa_family_t;
    for (to, from) in address.sun_path.iter_mut().zip(bytes) {
        *to = *from as libc::c_char;
    }
    let fd = unsafe {
        libc::socket(
            libc::AF_UNIX,
            libc::SOCK_STREAM | libc::SOCK_CLOEXEC | libc::SOCK_NONBLOCK,
            0,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let socket = unsafe { UnixStream::from_raw_fd(fd) };
    let result = unsafe {
        libc::connect(
            fd,
            (&address as *const libc::sockaddr_un).cast(),
            (std::mem::offset_of!(libc::sockaddr_un, sun_path) + bytes.len() + 1)
                as libc::socklen_t,
        )
    };
    if result < 0 {
        let error = io::Error::last_os_error();
        // AF_UNIX reports a full listener queue as EAGAIN rather than EINPROGRESS.
        if error.raw_os_error() != Some(libc::EINPROGRESS) {
            return Err(error);
        }
        loop {
            let remaining = deadline
                .checked_duration_since(Instant::now())
                .ok_or_else(|| {
                    io::Error::new(io::ErrorKind::TimedOut, "daemon connect timed out")
                })?;
            let mut poll = libc::pollfd {
                fd,
                events: libc::POLLOUT,
                revents: 0,
            };
            let result = unsafe {
                libc::poll(
                    &mut poll,
                    1,
                    remaining.as_millis().max(1).min(i32::MAX as u128) as i32,
                )
            };
            if result == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "daemon connect timed out",
                ));
            }
            if result < 0 {
                let error = io::Error::last_os_error();
                if error.kind() == io::ErrorKind::Interrupted {
                    continue;
                }
                return Err(error);
            }
            if let Some(error) = socket.take_error()? {
                return Err(error);
            }
            break;
        }
    }
    socket.set_nonblocking(false)?;
    Ok(socket)
}

fn peer_generation(socket: &UnixStream) -> Result<String> {
    let mut credentials: libc::ucred = unsafe { std::mem::zeroed() };
    let mut length = std::mem::size_of_val(&credentials) as libc::socklen_t;
    if unsafe {
        libc::getsockopt(
            socket.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            (&mut credentials as *mut libc::ucred).cast(),
            &mut length,
        )
    } < 0
    {
        return Err(io::Error::last_os_error().into());
    }
    let boot = fs::read_to_string("/proc/sys/kernel/random/boot_id")?;
    let stat = fs::read_to_string(format!("/proc/{}/stat", credentials.pid))?;
    let start = stat
        .rsplit_once(')')
        .context("invalid daemon process identity")?
        .1
        .split_whitespace()
        .nth(19)
        .context("missing daemon process start")?;
    Ok(format!("{}:{}:{}", boot.trim(), credentials.pid, start))
}

pub struct Client {
    socket: WebSocket<DeadlineStream>,
    timeout: Duration,
    sequence: u64,
    pub generation: String,
    pub initialize_result: Value,
}
impl Client {
    pub fn connect(path: &Path, timeout: Duration) -> Result<Self> {
        let deadline = Instant::now() + timeout;
        let socket = connect_socket(path, deadline)?;
        let generation = peer_generation(&socket)?;
        let stream = DeadlineStream {
            socket,
            deadline,
            header: Some(Vec::new()),
        };
        let config = WebSocketConfig::default()
            .read_buffer_size(65536)
            .write_buffer_size(0)
            .max_message_size(Some(MESSAGE_BYTES))
            .max_frame_size(Some(MESSAGE_BYTES))
            .max_write_buffer_size(MESSAGE_BYTES + 1024);
        let (socket, _) =
            tungstenite::client::client_with_config("ws://localhost/", stream, Some(config))
                .map_err(|e| anyhow::anyhow!("websocket handshake: {e}"))?;
        let mut client = Self {
            socket,
            timeout,
            sequence: 0,
            generation,
            initialize_result: Value::Null,
        };
        client.initialize_result = client.call("initialize",json!({"clientInfo":{"name":"codex_plus_background","version":"0.1"},"capabilities":{"experimentalApi":true}}))?;
        client.send(json!({"method":"initialized"}))?;
        Ok(client)
    }
    fn send(&mut self, value: Value) -> Result<()> {
        let text = serde_json::to_string(&value)?;
        if text.len() > MESSAGE_BYTES {
            bail!("oversized daemon request");
        }
        self.socket.send(Message::Text(text.into()))?;
        Ok(())
    }
    pub fn call(&mut self, method: &str, params: Value) -> Result<Value> {
        let deadline = Instant::now() + self.timeout;
        self.socket.get_mut().deadline = deadline;
        self.sequence += 1;
        let id = self.sequence;
        self.send(json!({"id":id,"method":method,"params":params}))?;
        loop {
            if Instant::now() >= deadline {
                bail!("daemon response timed out");
            }
            let response = match self.socket.read()? {
                Message::Text(text) => serde_json::from_str::<Value>(&text)?,
                Message::Ping(_) => {
                    self.socket.flush()?;
                    continue;
                }
                Message::Pong(_) => continue,
                Message::Close(_) => bail!("daemon closed connection"),
                _ => bail!("unexpected daemon frame"),
            };
            if !response.is_object() {
                bail!("malformed daemon response");
            }
            if response["id"].as_u64() != Some(id) {
                continue;
            }
            if let Some(error) = response.get("error") {
                return Err(RpcError {
                    method: method.to_owned(),
                    error: error.clone(),
                }
                .into());
            }
            let result = response
                .get("result")
                .filter(|value| value.is_object())
                .context("missing daemon result")?;
            return Ok(result.clone());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{os::unix::net::UnixListener, thread};
    fn raw_response(send: impl FnOnce(&mut UnixStream) + Send + 'static) -> String {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("sock");
        let listener = UnixListener::bind(&path).unwrap();
        let server = thread::spawn(move || {
            let (socket, _) = listener.accept().unwrap();
            let mut ws = tungstenite::accept(socket).unwrap();
            ws.read().unwrap();
            ws.send(Message::text("{\"id\":1,\"result\":{}}")).unwrap();
            ws.read().unwrap();
            ws.read().unwrap();
            send(ws.get_mut());
        });
        let mut client = Client::connect(&path, Duration::from_secs(2)).unwrap();
        let error = client.call("test", json!({})).unwrap_err().to_string();
        drop(client);
        server.join().unwrap();
        error
    }
    #[test]
    fn rejects_masked_binary_extensions_control_and_continuation_frames() {
        for bytes in [
            vec![0x81, 0x82, 0, 0, 0, 0, b'{', b'}'],
            vec![0x82, 2, b'{', b'}'],
            vec![0xc1, 0],
            vec![0x09, 0],
            vec![0x80, 0],
        ] {
            let error = raw_response(move |socket| {
                let _ = socket.write_all(&bytes);
            });
            assert!(!error.is_empty());
        }
    }
    #[test]
    fn bounds_frames_and_fragmented_message_totals() {
        let error = raw_response(|socket| {
            let mut frame = vec![0x81, 127];
            frame.extend_from_slice(&((MESSAGE_BYTES + 1) as u64).to_be_bytes());
            let _ = socket.write_all(&frame);
        });
        assert!(error.contains("Space limit exceeded"), "{error}");
        let error = raw_response(|socket| {
            let payload = vec![b'x'; MESSAGE_BYTES / 2 + 1];
            for opcode in [0x01, 0x80] {
                let mut frame = vec![opcode, 127];
                frame.extend_from_slice(&(payload.len() as u64).to_be_bytes());
                if socket.write_all(&frame).is_err() || socket.write_all(&payload).is_err() {
                    break;
                }
            }
        });
        assert!(error.contains("Space limit exceeded"), "{error}");
    }
    #[test]
    fn interleaved_notifications_share_one_call_deadline() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("sock");
        let listener = UnixListener::bind(&path).unwrap();
        let server = thread::spawn(move || {
            let (socket, _) = listener.accept().unwrap();
            let mut ws = tungstenite::accept(socket).unwrap();
            ws.read().unwrap();
            ws.send(Message::text("{\"id\":1,\"result\":{}}")).unwrap();
            ws.read().unwrap();
            ws.read().unwrap();
            for _ in 0..20 {
                thread::sleep(Duration::from_millis(15));
                if ws
                    .send(Message::text("{\"method\":\"notification\"}"))
                    .is_err()
                {
                    break;
                }
            }
        });
        let mut client = Client::connect(&path, Duration::from_millis(100)).unwrap();
        let before = Instant::now();
        assert!(client.call("test", json!({})).is_err());
        assert!(before.elapsed() < Duration::from_millis(240));
        drop(client);
        server.join().unwrap();
    }
    #[test]
    fn initialize_notifications_errors_and_identity() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("sock");
        let listener = UnixListener::bind(&path).unwrap();
        let server = thread::spawn(move || {
            let (socket, _) = listener.accept().unwrap();
            let mut ws = tungstenite::accept(socket).unwrap();
            let init: Value = serde_json::from_str(ws.read().unwrap().to_text().unwrap()).unwrap();
            assert_eq!(init["method"], "initialize");
            ws.send(Message::text(
                json!({"id":1,"result":{"ready":true}}).to_string(),
            ))
            .unwrap();
            assert!(
                ws.read()
                    .unwrap()
                    .to_text()
                    .unwrap()
                    .contains("initialized")
            );
            let request: Value =
                serde_json::from_str(ws.read().unwrap().to_text().unwrap()).unwrap();
            ws.send(Message::text(json!({"method":"notification"}).to_string()))
                .unwrap();
            ws.send(Message::text(
                json!({"id":request["id"],"error":{"code":-32601,"message":"unknown"}}).to_string(),
            ))
            .unwrap();
        });
        let mut client = Client::connect(&path, Duration::from_secs(2)).unwrap();
        assert_eq!(client.initialize_result["ready"], true);
        assert_eq!(client.generation.split(':').count(), 3);
        let error = client.call("missing", json!({})).unwrap_err();
        assert_eq!(error.downcast_ref::<RpcError>().unwrap().method, "missing");
        server.join().unwrap();
    }
    #[test]
    fn deadline_covers_all_reads() {
        let (socket, mut writer) = UnixStream::pair().unwrap();
        let mut stream = DeadlineStream {
            socket,
            deadline: Instant::now() + Duration::from_millis(80),
            header: None,
        };
        let sender = thread::spawn(move || {
            for _ in 0..4 {
                thread::sleep(Duration::from_millis(35));
                if writer.write_all(b"x").is_err() {
                    break;
                }
            }
        });
        let before = Instant::now();
        let mut bytes = [0; 4];
        assert!(stream.read_exact(&mut bytes).is_err());
        assert!(before.elapsed() < Duration::from_millis(200));
        drop(stream);
        sender.join().unwrap();
    }
}
