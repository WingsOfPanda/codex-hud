//! Durable root-owned mode state. Mutation/admission callers hold GoalLock across
//! their RPC boundary; this module deliberately does not acquire it a second time.
use crate::ledger::Store;
use anyhow::{Result, anyhow, ensure};
use serde_json::{Value, json};
use std::{
    fs::{self, OpenOptions},
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
};
use uuid::Uuid;

pub const POLICY_VERSION: i64 = 1;
pub const BOOTSTRAP: &str = include_str!("../../docs/ultracode-bootstrap.txt");
pub const POLICY: &str = include_str!("../../docs/ultracode-policy.md");

fn qualified_instructions(row: &Value) -> Option<&str> {
    row["load_instructions"].as_str().filter(|text| {
        row["version"] == 1
            && row["load_policy"] == POLICY_VERSION
            && !row["load_generation"].is_null()
            && text.len() <= 1024 * 1024
            && text.contains(BOOTSTRAP)
            && !text.contains('\0')
    })
}

fn bounded(value: &str, maximum: usize) -> Result<()> {
    ensure!(
        !value.trim().is_empty() && value.len() <= maximum && !value.contains('\0'),
        "invalid Ultracode generation or evidence"
    );
    Ok(())
}

fn submission(mut row: Value) -> Result<Value> {
    row["snapshot"] = serde_json::from_str(
        row["snapshot"]
            .as_str()
            .ok_or_else(|| anyhow!("invalid Ultracode snapshot"))?,
    )?;
    Ok(row)
}

fn effective(row: &Value) -> Value {
    json!({"enabled":row["snapshot"]["enabled"],"revision":row["snapshot"]["revision"],
        "policy_version":row["snapshot"]["policy_version"],"root":row["root"],
        "submission":row["submission"],"turn":row["turn"],"generation":row["generation"]})
}

impl Store {
    fn ultracode_init(&mut self, root: &str) -> Result<()> {
        Uuid::parse_str(root)?;
        self.transaction(true, |s| {
            for sql in [
                "CREATE TABLE IF NOT EXISTS ultracode_roots (root TEXT PRIMARY KEY,version INTEGER NOT NULL DEFAULT 1,enabled INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 0,load_generation TEXT,load_kind TEXT,load_policy INTEGER,load_instructions TEXT)",
                "CREATE TABLE IF NOT EXISTS ultracode_submissions (root TEXT NOT NULL,submission TEXT NOT NULL,generation TEXT NOT NULL,snapshot TEXT NOT NULL,state TEXT NOT NULL,turn TEXT,evidence TEXT,PRIMARY KEY(root,submission))",
                "CREATE TABLE IF NOT EXISTS ultracode_turns (root TEXT NOT NULL,turn TEXT NOT NULL,generation TEXT NOT NULL,submission TEXT NOT NULL,snapshot TEXT NOT NULL,PRIMARY KEY(root,turn,generation))",
            ] {
                s.execute(sql, &[])?;
            }
            if !s.rows("PRAGMA table_info(ultracode_roots)", &[])?.iter().any(|r| r["name"] == "load_instructions") {
                s.execute("ALTER TABLE ultracode_roots ADD COLUMN load_instructions TEXT", &[])?;
            }
            s.execute("INSERT OR IGNORE INTO ultracode_roots(root) VALUES (?)", &[json!(root)])
        })
    }

    fn ultracode_root(&mut self, root: &str) -> Result<Value> {
        let row = self
            .first("SELECT root,version,enabled,revision,load_generation,load_kind,load_policy,load_instructions FROM ultracode_roots WHERE root=?", &[json!(root)])?
            .ok_or_else(|| anyhow!("missing Ultracode root"))?;
        ensure!(row["version"] == 1, "unsupported Ultracode state version");
        Ok(row)
    }

    fn ultracode_loaded(&mut self, root: &str, generation: &str) -> Result<Value> {
        bounded(generation, 512)?;
        let row = self.ultracode_root(root)?;
        ensure!(
            row["load_generation"] == generation && qualified_instructions(&row).is_some(),
            "Ultracode bootstrap unavailable for this daemon generation"
        );
        Ok(row)
    }

    /// Called only for an explicitly user-requested root mode change.
    pub fn ultracode_request(&mut self, root: &str, enabled: bool) -> Result<Value> {
        self.ultracode_init(root)?;
        if enabled {
            let directory = self.directory.join("ultracode").join(root).join("runs");
            for path in [
                directory.parent().unwrap().parent().unwrap(),
                directory.parent().unwrap(),
                &directory,
            ] {
                match fs::create_dir(path) {
                    Ok(()) => (),
                    Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => (),
                    Err(error) => return Err(error.into()),
                }
                let file = OpenOptions::new()
                    .read(true)
                    .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW)
                    .open(path)?;
                file.set_permissions(fs::Permissions::from_mode(0o700))?;
            }
        }
        self.transaction(true, |s| {
            let row = s.ultracode_root(root)?;
            let revision = row["revision"]
                .as_i64()
                .and_then(|n| n.checked_add(1))
                .ok_or_else(|| anyhow!("Ultracode revision overflow"))?;
            s.execute(
                "UPDATE ultracode_roots SET enabled=?,revision=? WHERE root=?",
                &[json!(i64::from(enabled)), json!(revision), json!(root)],
            )?;
            Ok(json!({"enabled":enabled,"revision":revision}))
        })
    }

    /// The caller must positively establish installation during a controlled load.
    pub fn ultracode_record_load(
        &mut self,
        root: &str,
        generation: &str,
        kind: &str,
        developer_instructions: Option<&str>,
    ) -> Result<()> {
        bounded(generation, 512)?;
        ensure!(
            matches!(kind, "fresh" | "cold-resume"),
            "unsupported Ultracode load provenance"
        );
        self.ultracode_init(root)?;
        self.transaction(true, |s| {
            let row = s.ultracode_root(root)?;
            let instructions = if kind == "fresh" {
                developer_instructions.ok_or_else(|| anyhow!("fresh Ultracode load requires full developer instructions"))?
            } else {
                let saved = qualified_instructions(&row).ok_or_else(|| anyhow!("root has no qualified complete Ultracode developer instructions"))?;
                ensure!(developer_instructions.is_none_or(|text| text == saved), "cold Ultracode load changed complete developer instructions");
                saved
            };
            ensure!(instructions.len() <= 1024 * 1024 && instructions.contains(BOOTSTRAP) && !instructions.contains('\0'),
                "Ultracode developer instructions omit the exact bootstrap or exceed the input bound");
            s.execute("UPDATE ultracode_roots SET load_generation=?,load_kind=?,load_policy=?,load_instructions=? WHERE root=?",
                &[json!(generation),json!(kind),json!(POLICY_VERSION),json!(instructions),json!(root)])
        })
    }

    /// Internal resume input only; never include this protected text in status.
    pub fn ultracode_load_instructions(&mut self, root: &str) -> Result<Option<String>> {
        Uuid::parse_str(root)?;
        if !self.exists(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ultracode_roots'",
            &[],
        )? {
            return Ok(None);
        }
        Ok(self
            .ultracode_read_root(root)?
            .as_ref()
            .and_then(qualified_instructions)
            .map(str::to_owned))
    }

    fn ultracode_read_root(&mut self, root: &str) -> Result<Option<Value>> {
        // Distinct projections avoid stale SQLx column metadata across migration.
        let full = self
            .rows("PRAGMA table_info(ultracode_roots)", &[])?
            .iter()
            .any(|r| r["name"] == "load_instructions");
        let query = if full {
            "SELECT root,version,enabled,revision,load_generation,load_kind,load_policy,load_instructions FROM ultracode_roots WHERE root=?"
        } else {
            "SELECT root,version,enabled,revision,load_generation,load_kind,load_policy FROM ultracode_roots WHERE root=?"
        };
        self.first(query, &[json!(root)])
    }

    /// Freeze before sending; a repeated submission returns its original outcome.
    pub fn ultracode_reserve(&mut self, root: &str, id: &str, generation: &str) -> Result<Value> {
        Uuid::parse_str(id)?;
        bounded(generation, 512)?;
        self.ultracode_init(root)?;
        self.transaction(true, |s| {
            let row = s.ultracode_loaded(root, generation)?;
            if let Some(existing) = s.first("SELECT * FROM ultracode_submissions WHERE root=? AND submission=?", &[json!(root),json!(id)])? {
                ensure!(existing["generation"] == generation, "Ultracode submission generation mismatch");
                return submission(existing);
            }
            let snapshot = json!({"enabled":row["enabled"] == 1,"revision":row["revision"],
                "policy_version":POLICY_VERSION,"policy":POLICY});
            s.execute("INSERT INTO ultracode_submissions(root,submission,generation,snapshot,state) VALUES (?,?,?,?,'pending')",
                &[json!(root),json!(id),json!(generation),json!(snapshot.to_string())])?;
            submission(s.first("SELECT * FROM ultracode_submissions WHERE root=? AND submission=?", &[json!(root),json!(id)])?.unwrap())
        })
    }

    /// Bind only with positive exact-turn evidence identifying its initial user
    /// submission. Steering an existing turn preserves the original effective policy.
    pub fn ultracode_bind(
        &mut self,
        root: &str,
        id: &str,
        generation: &str,
        turn: &str,
    ) -> Result<Value> {
        Uuid::parse_str(id)?;
        Uuid::parse_str(turn)?;
        self.ultracode_init(root)?;
        self.transaction(true, |s| {
            s.ultracode_loaded(root, generation)?;
            let reserved = s.first("SELECT * FROM ultracode_submissions WHERE root=? AND submission=?", &[json!(root),json!(id)])?
                .ok_or_else(|| anyhow!("no reserved Ultracode submission"))?;
            ensure!(reserved["generation"] == generation, "Ultracode submission generation mismatch");
            ensure!(reserved["state"] != "failed", "Ultracode submission definitively failed");
            ensure!(reserved["turn"].is_null() || reserved["turn"] == turn, "Ultracode submission already bound to another turn");
            s.execute("INSERT OR IGNORE INTO ultracode_turns(root,turn,generation,submission,snapshot) VALUES (?,?,?,?,?)",
                &[json!(root),json!(turn),json!(generation),json!(id),reserved["snapshot"].clone()])?;
            s.execute("UPDATE ultracode_submissions SET state='bound',turn=? WHERE root=? AND submission=?",
                &[json!(turn),json!(root),json!(id)])?;
            let binding = submission(s.first("SELECT * FROM ultracode_turns WHERE root=? AND turn=? AND generation=?",
                &[json!(root),json!(turn),json!(generation)])?.unwrap())?;
            Ok(effective(&binding))
        })
    }

    /// Persist failed/lost ACK evidence; this never schedules or retries delivery.
    pub fn ultracode_settle(
        &mut self,
        root: &str,
        id: &str,
        generation: &str,
        outcome: &str,
        evidence: &str,
    ) -> Result<Value> {
        Uuid::parse_str(root)?;
        Uuid::parse_str(id)?;
        bounded(generation, 512)?;
        bounded(evidence, 4096)?;
        ensure!(
            matches!(outcome, "failed" | "uncertain"),
            "invalid Ultracode submission outcome"
        );
        self.ultracode_init(root)?;
        self.transaction(true, |s| {
            let row = s
                .first(
                    "SELECT * FROM ultracode_submissions WHERE root=? AND submission=?",
                    &[json!(root), json!(id)],
                )?
                .ok_or_else(|| anyhow!("no reserved Ultracode submission"))?;
            ensure!(
                row["generation"] == generation,
                "Ultracode submission generation mismatch"
            );
            ensure!(
                row["state"] != "bound",
                "Ultracode submission already confirmed"
            );
            ensure!(
                row["state"] != "failed" || outcome == "failed",
                "Ultracode submission definitively failed"
            );
            s.execute(
                "UPDATE ultracode_submissions SET state=?,evidence=? WHERE root=? AND submission=?",
                &[json!(outcome), json!(evidence), json!(root), json!(id)],
            )?;
            submission(
                s.first(
                    "SELECT * FROM ultracode_submissions WHERE root=? AND submission=?",
                    &[json!(root), json!(id)],
                )?
                .unwrap(),
            )
        })
    }

    /// Read-only: never creates a root, installs a bootstrap or initiates RPC.
    pub fn ultracode_status(
        &mut self,
        root: &str,
        generation: Option<&str>,
        turn: Option<&str>,
    ) -> Result<Value> {
        Uuid::parse_str(root)?;
        if let Some(generation) = generation {
            bounded(generation, 512)?;
        }
        if let Some(turn) = turn {
            Uuid::parse_str(turn)?;
        }
        self.transaction(false, |s| {
            let exists = s.exists("SELECT name FROM sqlite_master WHERE type='table' AND name='ultracode_roots'", &[])?;
            let row = if exists { s.ultracode_read_root(root)? } else { None };
            if let Some(row) = &row { ensure!(row["version"] == 1, "unsupported Ultracode state version"); }
            let available = row.as_ref().is_some_and(|r| generation.is_some_and(|g| r["load_generation"] == g) && qualified_instructions(r).is_some());
            let binding = if available && turn.is_some() {
                s.first("SELECT * FROM ultracode_turns WHERE root=? AND turn=? AND generation=?",
                    &[json!(root),json!(turn),json!(generation)])?.map(submission).transpose()?
            } else { None };
            let pending = if exists { s.rows("SELECT submission,generation,state,evidence FROM ultracode_submissions WHERE root=? AND state IN ('pending','uncertain') ORDER BY rowid", &[json!(root)])? } else { vec![] };
            let instructions = binding.as_ref().filter(|b| b["snapshot"]["enabled"] == true)
                .map(|b| b["snapshot"]["policy"].clone()).unwrap_or(json!(""));
            Ok(json!({"version":1,"root":root,"journal_directory":s.directory.join("ultracode").join(root).join("runs"),
                "requested":{"enabled":row.as_ref().is_some_and(|r|r["enabled"] == 1),"revision":row.as_ref().map(|r|r["revision"].clone()).unwrap_or(json!(0)),"policy_version":POLICY_VERSION},
                "load":row.as_ref().filter(|r|!r["load_generation"].is_null()).map(|r|json!({"generation":r["load_generation"],"kind":r["load_kind"],"policy_version":r["load_policy"]})),
                "availability":if available {"available"} else {"unavailable"},
                "effective":binding.as_ref().map(effective),"pending":pending,"instructions":instructions}))
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn id() -> String {
        Uuid::new_v4().to_string()
    }
    fn setup() -> (tempfile::TempDir, Store, String) {
        let home = tempfile::tempdir().unwrap();
        let store = Store::open(home.path()).unwrap();
        (home, store, id())
    }

    #[test]
    fn default_is_off_and_status_does_not_create_state() {
        let (_home, mut s, root) = setup();
        let status = s.ultracode_status(&root, Some("generation"), None).unwrap();
        assert_eq!(
            status["requested"],
            json!({"enabled":false,"revision":0,"policy_version":POLICY_VERSION})
        );
        assert_eq!(status["availability"], "unavailable");
        assert!(status["effective"].is_null());
        assert!(!s.directory.join("ultracode").exists());
        assert!(
            !s.exists(
                "SELECT name FROM sqlite_master WHERE name='ultracode_roots'",
                &[]
            )
            .unwrap()
        );
    }

    #[test]
    fn on_off_on_preserves_queued_snapshots_and_steered_turn() {
        let (_home, mut s, root) = setup();
        s.ultracode_record_load(&root, "generation", "fresh", Some(BOOTSTRAP))
            .unwrap();
        assert_eq!(s.ultracode_request(&root, true).unwrap()["revision"], 1);
        let first = id();
        let snapshot = s.ultracode_reserve(&root, &first, "generation").unwrap();
        s.ultracode_request(&root, false).unwrap();
        let second = id();
        s.ultracode_reserve(&root, &second, "generation").unwrap();
        assert_eq!(s.ultracode_request(&root, true).unwrap()["revision"], 3);
        assert_eq!(
            s.ultracode_reserve(&root, &first, "generation").unwrap(),
            snapshot
        );
        let turn = id();
        let binding = s
            .ultracode_bind(&root, &first, "generation", &turn)
            .unwrap();
        assert_eq!(binding["revision"], 1);
        assert_eq!(
            s.ultracode_bind(&root, &second, "generation", &turn)
                .unwrap(),
            binding
        );
        assert_eq!(
            s.ultracode_reserve(&root, &second, "generation").unwrap()["snapshot"]["revision"],
            2
        );
        let status = s
            .ultracode_status(&root, Some("generation"), Some(&turn))
            .unwrap();
        assert_eq!(status["requested"]["revision"], 3);
        assert_eq!(status["effective"], binding);
        assert_eq!(status["instructions"], POLICY);
        assert!(status["pending"].as_array().unwrap().is_empty());
        let third = id();
        s.ultracode_reserve(&root, &third, "generation").unwrap();
        assert_eq!(
            s.ultracode_bind(&root, &third, "generation", &id())
                .unwrap()["revision"],
            3
        );
    }

    #[test]
    fn lost_ack_persists_without_replay_and_can_be_confirmed() {
        let (home, mut s, root) = setup();
        s.ultracode_record_load(&root, "generation", "fresh", Some(BOOTSTRAP))
            .unwrap();
        s.ultracode_request(&root, true).unwrap();
        let first = id();
        s.ultracode_reserve(&root, &first, "generation").unwrap();
        let uncertain = s
            .ultracode_settle(
                &root,
                &first,
                "generation",
                "uncertain",
                "RPC response lost",
            )
            .unwrap();
        s.ultracode_request(&root, false).unwrap();
        drop(s);
        let mut s = Store::open(home.path()).unwrap();
        assert_eq!(
            s.ultracode_reserve(&root, &first, "generation").unwrap(),
            uncertain
        );
        let status = s
            .ultracode_status(&root, Some("generation"), Some(&id()))
            .unwrap();
        assert!(status["effective"].is_null());
        assert_eq!(status["pending"][0]["state"], "uncertain");
        let turn = id();
        assert_eq!(
            s.ultracode_bind(&root, &first, "generation", &turn)
                .unwrap()["enabled"],
            true
        );
        assert!(
            s.ultracode_settle(&root, &first, "generation", "uncertain", "late timeout")
                .is_err()
        );
        drop(s);
        let mut s = Store::open(home.path()).unwrap();
        assert_eq!(
            s.ultracode_status(&root, Some("generation"), Some(&turn))
                .unwrap()["effective"]["revision"],
            1
        );
    }

    #[test]
    fn failed_ack_is_terminal_and_off_turn_has_no_policy() {
        let (_home, mut s, root) = setup();
        assert!(
            s.ultracode_record_load(&root, "generation", "cold-resume", None)
                .is_err()
        );
        s.ultracode_record_load(&root, "generation", "fresh", Some(BOOTSTRAP))
            .unwrap();
        s.ultracode_record_load(&root, "generation", "cold-resume", None)
            .unwrap();
        let first = id();
        s.ultracode_reserve(&root, &first, "generation").unwrap();
        let failed = s
            .ultracode_settle(&root, &first, "generation", "failed", "RPC rejected")
            .unwrap();
        assert_eq!(
            s.ultracode_reserve(&root, &first, "generation").unwrap(),
            failed
        );
        assert!(
            s.ultracode_bind(&root, &first, "generation", &id())
                .is_err()
        );
        let next = id();
        let turn = id();
        s.ultracode_reserve(&root, &next, "generation").unwrap();
        s.ultracode_bind(&root, &next, "generation", &turn).unwrap();
        assert_eq!(
            s.ultracode_status(&root, Some("generation"), Some(&turn))
                .unwrap()["instructions"],
            ""
        );
        assert!(s.ultracode_bind(&root, &next, "generation", &id()).is_err());
    }

    #[test]
    fn root_and_generation_isolation_and_input_validation() {
        let (_home, mut s, root) = setup();
        assert!(s.ultracode_reserve(&root, &id(), "generation").is_err());
        assert!(
            s.ultracode_record_load(&root, "generation", "loaded", None)
                .is_err()
        );
        s.ultracode_record_load(&root, "generation", "fresh", Some(BOOTSTRAP))
            .unwrap();
        let first = id();
        s.ultracode_reserve(&root, &first, "generation").unwrap();
        let turn = id();
        s.ultracode_bind(&root, &first, "generation", &turn)
            .unwrap();
        assert!(
            s.ultracode_bind(&id(), &first, "generation", &turn)
                .is_err()
        );
        assert!(
            s.ultracode_reserve(&root, &first, "new-generation")
                .is_err()
        );
        let status = s
            .ultracode_status(&root, Some("new-generation"), Some(&turn))
            .unwrap();
        assert_eq!(status["availability"], "unavailable");
        assert!(status["effective"].is_null());
        s.ultracode_record_load(&root, "new-generation", "cold-resume", None)
            .unwrap();
        assert!(
            s.ultracode_reserve(&root, &first, "new-generation")
                .is_err()
        );
        assert!(
            s.ultracode_bind(&root, &first, "new-generation", &turn)
                .is_err()
        );
        for malformed in ["", "../root", "not-a-uuid"] {
            assert!(s.ultracode_request(malformed, true).is_err());
            assert!(
                s.ultracode_reserve(&root, malformed, "new-generation")
                    .is_err()
            );
            assert!(
                s.ultracode_bind(&root, &first, "new-generation", malformed)
                    .is_err()
            );
        }
        assert!(
            s.ultracode_record_load(&root, "", "fresh", Some(BOOTSTRAP))
                .is_err()
        );
        assert!(
            s.ultracode_record_load(&root, &"x".repeat(513), "fresh", Some(BOOTSTRAP))
                .is_err()
        );
        assert!(
            s.ultracode_settle(&root, &first, "generation", "uncertain", "")
                .is_err()
        );
    }

    #[test]
    fn journal_is_private_and_rejects_symlink_without_enabling_mode() {
        use std::os::unix::fs::symlink;
        let (_home, mut s, root) = setup();
        s.ultracode_request(&root, false).unwrap();
        assert!(!s.directory.join("ultracode").exists());
        let outside = tempfile::tempdir().unwrap();
        symlink(outside.path(), s.directory.join("ultracode")).unwrap();
        assert!(s.ultracode_request(&root, true).is_err());
        assert!(!outside.path().join(&root).exists());
        assert_eq!(
            s.ultracode_status(&root, None, None).unwrap()["requested"]["enabled"],
            false
        );
        fs::remove_file(s.directory.join("ultracode")).unwrap();
        s.ultracode_request(&root, true).unwrap();
        let journal = s.directory.join("ultracode").join(&root).join("runs");
        assert_eq!(
            fs::metadata(journal).unwrap().permissions().mode() & 0o777,
            0o700
        );
    }

    #[test]
    fn complete_developer_instructions_survive_restart_without_status_disclosure() {
        let (home, mut s, root) = setup();
        assert_eq!(s.ultracode_load_instructions(&root).unwrap(), None);
        let instructions = format!(
            "Private original developer instructions\n{BOOTSTRAP}\nAdditional original instructions"
        );
        s.ultracode_record_load(&root, "old-generation", "fresh", Some(&instructions))
            .unwrap();
        drop(s);
        let mut s = Store::open(home.path()).unwrap();
        assert_eq!(
            s.ultracode_load_instructions(&root).unwrap().as_deref(),
            Some(instructions.as_str())
        );
        s.ultracode_record_load(&root, "new-generation", "cold-resume", None)
            .unwrap();
        s.ultracode_record_load(&root, "new-generation", "cold-resume", Some(&instructions))
            .unwrap();
        s.ultracode_request(&root, true).unwrap();
        let submission = id();
        let turn = id();
        s.ultracode_reserve(&root, &submission, "new-generation")
            .unwrap();
        s.ultracode_bind(&root, &submission, "new-generation", &turn)
            .unwrap();
        let status = s
            .ultracode_status(&root, Some("new-generation"), Some(&turn))
            .unwrap();
        assert_eq!(status["availability"], "available");
        assert_eq!(status["requested"]["policy_version"], POLICY_VERSION);
        assert_eq!(status["instructions"], POLICY);
        assert!(!status.to_string().contains("Private original"));
        assert!(!status.to_string().contains("Additional original"));
        assert!(!status.to_string().contains("load_instructions"));
        assert_eq!(s.ultracode_load_instructions(&id()).unwrap(), None);
    }

    #[test]
    fn missing_modified_and_changed_full_instructions_are_not_qualified() {
        let (_home, mut s, root) = setup();
        assert!(
            s.ultracode_record_load(&root, "generation", "fresh", None)
                .is_err()
        );
        let modified = BOOTSTRAP.replacen("Codex", "Other", 1);
        assert!(
            s.ultracode_record_load(&root, "generation", "fresh", Some(&modified))
                .is_err()
        );
        assert!(
            s.ultracode_record_load(
                &root,
                "generation",
                "fresh",
                Some(&format!("{BOOTSTRAP}{}", "x".repeat(1024 * 1024)))
            )
            .is_err()
        );
        let original = format!("Original instructions\n{BOOTSTRAP}");
        s.ultracode_record_load(&root, "generation", "fresh", Some(&original))
            .unwrap();
        assert!(
            s.ultracode_record_load(&root, "next-generation", "cold-resume", Some(BOOTSTRAP))
                .is_err()
        );
        assert_eq!(
            s.ultracode_load_instructions(&root).unwrap().as_deref(),
            Some(original.as_str())
        );
        assert_eq!(
            s.ultracode_status(&root, Some("next-generation"), None)
                .unwrap()["availability"],
            "unavailable"
        );
    }

    #[test]
    fn old_candidate_load_without_complete_instructions_is_refused() {
        let (_home, mut s, root) = setup();
        s.ultracode_init(&root).unwrap();
        s.execute("DROP TABLE ultracode_roots", &[]).unwrap();
        s.execute("CREATE TABLE ultracode_roots (root TEXT PRIMARY KEY,version INTEGER NOT NULL DEFAULT 1,enabled INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 0,load_generation TEXT,load_kind TEXT,load_policy INTEGER)", &[]).unwrap();
        s.execute("INSERT INTO ultracode_roots(root,load_generation,load_kind,load_policy) VALUES (?,'generation','fresh',1)", &[json!(root)]).unwrap();
        assert_eq!(s.ultracode_load_instructions(&root).unwrap(), None);
        assert_eq!(
            s.ultracode_status(&root, Some("generation"), None).unwrap()["availability"],
            "unavailable"
        );
        assert!(
            s.ultracode_record_load(&root, "new-generation", "cold-resume", None)
                .is_err()
        );
        assert!(s.ultracode_reserve(&root, &id(), "generation").is_err());
        s.ultracode_record_load(&root, "new-generation", "fresh", Some(BOOTSTRAP))
            .unwrap();
        assert_eq!(
            s.ultracode_load_instructions(&root).unwrap().as_deref(),
            Some(BOOTSTRAP)
        );
        s.execute(
            "UPDATE ultracode_roots SET load_policy=0 WHERE root=?",
            &[json!(root)],
        )
        .unwrap();
        assert_eq!(s.ultracode_load_instructions(&root).unwrap(), None);
    }
}
