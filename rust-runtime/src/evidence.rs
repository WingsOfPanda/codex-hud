//! Bounded, positive provenance from the requested session's local rollout.
use anyhow::Result;
use serde_json::{Value, json};
use std::{
    fs::{self, OpenOptions},
    os::{
        fd::AsRawFd,
        unix::fs::{FileExt, MetadataExt, OpenOptionsExt},
    },
    path::Path,
};

const HEADER_BYTES: usize = 64 * 1024;
const TAIL_BYTES: u64 = 2 * 1024 * 1024;

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

fn record(bytes: &[u8]) -> Option<Value> {
    serde_json::from_slice::<Value>(bytes)
        .ok()
        .filter(Value::is_object)
}

fn timestamp_valid(text: &str) -> bool {
    let bytes = text.as_bytes();
    if !text.is_ascii()
        || bytes.len() < 20
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || bytes[10] != b'T'
        || bytes[13] != b':'
        || bytes[16] != b':'
    {
        return false;
    }
    let number = |a: usize, b: usize| -> Option<u32> {
        let s = text.get(a..b)?;
        if !s.bytes().all(|c| c.is_ascii_digit()) {
            return None;
        }
        s.parse().ok()
    };
    let (Some(year), Some(month), Some(day), Some(hour), Some(minute), Some(second)) = (
        number(0, 4),
        number(5, 7),
        number(8, 10),
        number(11, 13),
        number(14, 16),
        number(17, 19),
    ) else {
        return false;
    };
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let days = match month {
        2 => {
            if leap {
                29
            } else {
                28
            }
        }
        4 | 6 | 9 | 11 => 30,
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        _ => return false,
    };
    if year == 0 || day == 0 || day > days || hour > 23 || minute > 59 || second > 59 {
        return false;
    }
    let mut end = 19;
    if bytes.get(end) == Some(&b'.') {
        end += 1;
        let start = end;
        while bytes.get(end).is_some_and(u8::is_ascii_digit) {
            end += 1;
        }
        if end == start || end - start > 9 {
            return false;
        }
    }
    if &text[end..] == "Z" {
        return true;
    }
    // datetime.fromisoformat accepts offset components that normalize below 24 hours.
    bytes.len() == end + 6
        && matches!(bytes[end], b'+' | b'-')
        && bytes[end + 3] == b':'
        && number(end + 1, end + 3)
            .zip(number(end + 4, end + 6))
            .is_some_and(|(h, m)| h * 60 + m < 1440)
}

fn read_evidence(home: &Path, path: &Path, thread: &str, turn: &str) -> Result<Option<Value>> {
    if thread.is_empty() || turn.is_empty() {
        return Ok(None);
    }
    let root = home.canonicalize()?;
    let candidate = if path.is_absolute() {
        path.to_owned()
    } else {
        root.join(path)
    };
    if fs::symlink_metadata(&candidate)?.file_type().is_symlink() {
        return Ok(None);
    }
    let canonical = candidate.canonicalize()?;
    let relative = match canonical.strip_prefix(&root) {
        Ok(p) => p,
        Err(_) => return Ok(None),
    };
    let parts: Vec<_> = relative.components().collect();
    if parts.len() < 2
        || !matches!(
            parts[0].as_os_str().to_str(),
            Some("sessions" | "archived_sessions")
        )
    {
        return Ok(None);
    }
    let flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK;
    let mut directory = OpenOptions::new()
        .read(true)
        .custom_flags(flags | libc::O_DIRECTORY)
        .open(&root)?;
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
    if !before.is_file() {
        return Ok(None);
    }
    let mut header = vec![0; HEADER_BYTES + 1];
    let count = file.read_at(&mut header, 0)?;
    header.truncate(count);
    let Some(newline) = header.iter().position(|b| *b == b'\n') else {
        return Ok(None);
    };
    let Some(meta) = record(&header[..newline]) else {
        return Ok(None);
    };
    if meta["type"] != "session_meta" || meta["payload"]["id"] != thread {
        return Ok(None);
    }
    let start = before.len().saturating_sub(TAIL_BYTES);
    let offset = start.saturating_sub(1);
    let mut tail = vec![0; (before.len() - offset) as usize];
    let count = file.read_at(&mut tail, offset)?;
    if count != tail.len() {
        return Ok(None);
    }
    let tail = if start > 0 {
        match tail.iter().position(|b| *b == b'\n') {
            Some(i) => &tail[i + 1..],
            None => return Ok(None),
        }
    } else {
        &tail[..]
    };
    if extent(&file.metadata()?) != extent(&before)
        || extent(&fs::symlink_metadata(&candidate)?) != extent(&before)
    {
        return Ok(None);
    }
    let mut result: Option<Value> = None;
    let mut conflicted = false;
    let mut lines = tail.split(|b| *b == b'\n').peekable();
    while let Some(raw) = lines.next() {
        if lines.peek().is_none() {
            break;
        }
        let Some(record) = record(raw) else {
            continue;
        };
        let payload = &record["payload"];
        if record["type"] == "compacted"
            || (record["type"] == "event_msg" && payload["type"] == "thread_rolled_back")
        {
            result = None;
            conflicted = false;
            continue;
        }
        if record["type"] != "event_msg"
            || conflicted
            || !payload.is_object()
            || payload["turn_id"] != turn
        {
            continue;
        }
        let Some(event @ ("task_complete" | "turn_aborted")) = payload["type"].as_str() else {
            continue;
        };
        let error = &payload["error"];
        if !error.is_null() && (!error.is_object() || !error["message"].is_string()) {
            return Ok(None);
        }
        let status = if event == "turn_aborted" {
            if !matches!(
                payload["reason"].as_str(),
                Some("interrupted" | "replaced" | "review_ended" | "budget_limited")
            ) {
                return Ok(None);
            }
            "interrupted"
        } else if error.is_null() {
            "completed"
        } else {
            "failed"
        };
        if result
            .as_ref()
            .is_some_and(|r| r["status"] != status || r["event"] != event)
        {
            result = None;
            conflicted = true;
            continue;
        }
        let mut value = json!({"status": status, "event": event});
        if let Some(timestamp) = record["timestamp"].as_str().filter(|s| timestamp_valid(s)) {
            value["timestamp"] = timestamp.into();
        }
        result = Some(value);
    }
    Ok(result)
}

pub fn terminal_event(home: &Path, path: &Path, thread: &str, turn: &str) -> Result<Option<Value>> {
    // An unreadable or changing rollout supplies no positive terminal evidence.
    Ok(read_evidence(home, path, thread, turn).unwrap_or(None))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs::File, io::Write};
    fn rollout(extra: &[Value]) -> (tempfile::TempDir, std::path::PathBuf) {
        let temp = tempfile::tempdir().unwrap();
        fs::create_dir(temp.path().join("sessions")).unwrap();
        let path = temp.path().join("sessions/rollout.jsonl");
        let mut f = File::create(&path).unwrap();
        writeln!(
            f,
            "{}",
            json!({"type":"session_meta","payload":{"id":"thread"}})
        )
        .unwrap();
        for value in extra {
            writeln!(f, "{value}").unwrap();
        }
        (temp, path)
    }
    fn event(kind: &str) -> Value {
        json!({"type":"event_msg","timestamp":"2024-02-29T00:00:00.123Z","payload":{"type":kind,"turn_id":"turn","reason":"interrupted"}})
    }
    #[test]
    fn exact_session_and_contradictions() {
        let (home, path) = rollout(&[event("task_complete")]);
        assert_eq!(
            terminal_event(home.path(), &path, "thread", "turn")
                .unwrap()
                .unwrap()["status"],
            "completed"
        );
        assert!(
            terminal_event(home.path(), &path, "other", "turn")
                .unwrap()
                .is_none()
        );
        let (home, path) = rollout(&[event("task_complete"), event("turn_aborted")]);
        assert!(
            terminal_event(home.path(), &path, "thread", "turn")
                .unwrap()
                .is_none()
        );
    }
    #[test]
    fn reset_and_symlink_rules() {
        let (home, path) = rollout(&[
            event("task_complete"),
            event("turn_aborted"),
            json!({"type":"compacted"}),
            event("turn_aborted"),
        ]);
        assert_eq!(
            terminal_event(home.path(), &path, "thread", "turn")
                .unwrap()
                .unwrap()["status"],
            "interrupted"
        );
        let link = home.path().join("sessions/link");
        std::os::unix::fs::symlink(path, &link).unwrap();
        assert!(
            terminal_event(home.path(), &link, "thread", "turn")
                .unwrap()
                .is_none()
        );
    }
    #[test]
    fn timestamp_calendar_validation() {
        assert!(timestamp_valid("2024-02-29T00:00:00Z"));
        assert!(!timestamp_valid("2023-02-29T00:00:00Z"));
        assert!(!timestamp_valid("2024-01-01T24:00:00Z"));
    }
    #[test]
    fn evidence_outside_tail_and_unterminated_records_stay_unknown() {
        let (home, path) = rollout(&[event("task_complete")]);
        let mut file = OpenOptions::new().append(true).open(&path).unwrap();
        file.write_all(&vec![b' '; TAIL_BYTES as usize + 1])
            .unwrap();
        file.write_all(b"\n").unwrap();
        assert!(
            terminal_event(home.path(), &path, "thread", "turn")
                .unwrap()
                .is_none()
        );
        let (home, path) = rollout(&[]);
        let mut file = OpenOptions::new().append(true).open(&path).unwrap();
        write!(file, "{}", event("task_complete")).unwrap();
        assert!(
            terminal_event(home.path(), &path, "thread", "turn")
                .unwrap()
                .is_none()
        );
    }
    #[test]
    fn malformed_terminal_fields_and_header_are_not_positive_evidence() {
        let mut failed = event("task_complete");
        failed["payload"]["error"] = json!({"message":false});
        let (home, path) = rollout(&[failed]);
        assert!(
            terminal_event(home.path(), &path, "thread", "turn")
                .unwrap()
                .is_none()
        );
        let (home, path) = rollout(&[]);
        fs::write(&path, vec![b'x'; HEADER_BYTES + 2]).unwrap();
        assert!(
            terminal_event(home.path(), &path, "thread", "turn")
                .unwrap()
                .is_none()
        );
    }
}
