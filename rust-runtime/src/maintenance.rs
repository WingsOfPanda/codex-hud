//! Durable admission hold; lock order is root GoalLock -> admission -> resources.
use anyhow::{Result, bail};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{File, OpenOptions};
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

#[derive(Debug)]
pub struct AdmissionGuard(File);

impl Drop for AdmissionGuard {
    fn drop(&mut self) {
        unsafe { libc::flock(self.0.as_raw_fd(), libc::LOCK_UN) };
    }
}

#[derive(Debug)]
pub struct Held;
impl std::fmt::Display for Held {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("maintenance hold blocks new Plus admission")
    }
}
impl std::error::Error for Held {}

fn lock(home: &Path) -> Result<AdmissionGuard> {
    let directory = crate::local::state_directory(home)?;
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(directory.join("admission.lock"))?;
    if !file.metadata()?.is_file() {
        bail!("admission lock must be a regular file");
    }
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } < 0 {
        bail!(
            "Plus admission is busy; retry maintenance or launch after the current admission finishes"
        );
    }
    Ok(AdmissionGuard(file))
}

fn path(home: &Path) -> PathBuf {
    home.join("codex-plus/maintenance.json")
}

fn read(home: &Path) -> Result<Value> {
    match crate::local::read_regular(&path(home), 4096) {
        Ok(bytes) => {
            let value: Value = serde_json::from_slice(&bytes)?;
            if value["schema"] != 1 || !value["held"].is_boolean() {
                bail!("invalid maintenance hold record");
            }
            if value["held"] == true
                && value["id"]
                    .as_str()
                    .is_none_or(|id| uuid::Uuid::parse_str(id).is_err())
            {
                bail!("maintenance hold identity is invalid");
            }
            Ok(value)
        }
        Err(error)
            if error
                .downcast_ref::<std::io::Error>()
                .is_some_and(|e| e.kind() == std::io::ErrorKind::NotFound) =>
        {
            Ok(json!({"schema":1,"held":false}))
        }
        Err(error) => Err(error),
    }
}

pub fn is_held(home: &Path) -> Result<bool> {
    Ok(read(home)?["held"] == true)
}

/// Hold this only around admission and its acknowledgement, never while acquiring GoalLock.
pub fn admission(home: &Path) -> Result<AdmissionGuard> {
    let guard = lock(home)?;
    if is_held(home)? {
        return Err(Held.into());
    }
    Ok(guard)
}

pub fn hold_enter(home: &Path) -> Result<Value> {
    {
        let _guard = lock(home)?;
        if !is_held(home)? {
            crate::local::atomic_json(
                &path(home),
                &json!({"schema":1,"held":true,"id":uuid::Uuid::new_v4().to_string(),"entered_at":crate::local::now()}),
            )?;
        }
    }
    // Reconciliation may take root locks, so it always follows release of the global gate.
    hold_status(home)
}

pub fn hold_leave(home: &Path) -> Result<Value> {
    let _guard = lock(home)?;
    let mut value = read(home)?;
    value["held"] = json!(false);
    value["left_at"] = json!(crate::local::now());
    crate::local::atomic_json(&path(home), &value)?;
    value["admission_quiet"] = json!(false);
    Ok(value)
}

pub fn hold_status(home: &Path) -> Result<Value> {
    let mut value = {
        let _guard = lock(home)?;
        read(home)?
    };
    value["admission_quiet"] = json!(false);
    value["execution_quiet"] = json!(false);
    value["scope"] = json!(
        "Plus admissions and known Plus roots; native and AP work require the fleet quiet check"
    );
    let execution_roots = crate::executions::roots(home)?;
    let mut execution_records = Vec::new();
    let mut counts = BTreeMap::<String, usize>::new();
    let mut execution_quiet = true;
    let mut execution_total = 0;
    let mut execution_nonquiet = 0;
    for root in &execution_roots {
        for record in crate::executions::list(home, root)? {
            let id = record["id"]
                .as_str()
                .ok_or_else(|| anyhow::anyhow!("execution identity missing"))?;
            let record = crate::executions::reconcile(home, root, id)?;
            let quiet = execution_is_quiet(&record);
            execution_quiet &= quiet;
            execution_nonquiet += usize::from(!quiet);
            execution_total += 1;
            *counts
                .entry(record["state"].as_str().unwrap_or("unknown").to_owned())
                .or_default() += 1;
            if !quiet && execution_records.len() < 64 {
                execution_records.push(json!({"root":root,"id":id,"state":record["state"],"package":record["package"],"cleanup_status":record["result"]["cleanup"]["status"],"resolution":!record["resolution"].is_null()}));
            }
        }
    }
    value["execution_quiet"] = json!(execution_quiet);
    value["execution_summary"] = json!({"total":execution_total,"nonquiet_total":execution_nonquiet,"states":counts,"nonquiet":execution_records});
    if value["held"] != true {
        return Ok(value);
    }
    let mut store = crate::ledger::Store::open(home)?;
    let mut roots = execution_roots.into_iter().collect::<BTreeSet<_>>();
    for row in store.rows("SELECT parent AS root FROM parks WHERE state='armed' UNION SELECT parent AS root FROM callbacks WHERE state IN ('pending','starting','in_flight','uncertain','cancel_requested','cancel_uncertain') UNION SELECT root FROM goal_turns WHERE finalized=0 UNION SELECT root FROM goal_dispatches WHERE state IN ('submitting','pending','starting','uncertain') ORDER BY root", &[])? {
        roots.insert(row["root"].as_str().ok_or_else(||anyhow::anyhow!("invalid maintenance root"))?.to_owned());
    }
    let mut observations = Vec::new();
    let mut quiet = true;
    for root in roots {
        match crate::background::maintenance_reconcile(&mut store, &root) {
            Ok(observation) => {
                quiet &= observation["quiet"] == true;
                observations.push(observation);
            }
            Err(error) => {
                quiet = false;
                observations.push(json!({"root":root,"quiet":false,"error":format!("{error:#}")}));
            }
        }
    }
    let _guard = lock(home)?;
    let current = read(home)?;
    value["admission_quiet"] =
        json!(quiet && current["held"] == true && current["id"] == value["id"]);
    value["roots"] = json!(observations);
    value["checked_at"] = json!(crate::local::now());
    Ok(value)
}

pub(crate) fn execution_is_quiet(record: &Value) -> bool {
    match record["state"].as_str() {
        Some("prepared") => true,
        // Resolution proves cleanup, not the missing command outcome.
        Some("unknown") => record["resolution"]["cleanup_verified"] == true,
        Some("completed" | "failed" | "cancelled") => {
            record["result"].is_object()
                && (matches!(
                    record["result"]["cleanup"]["status"].as_str(),
                    Some("not_required" | "completed")
                ) || record["resolution"]["cleanup_verified"] == true)
        }
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn verified_cleanup_allows_quiet_without_inventing_an_unknown_exit() {
        let home = tempfile::tempdir().unwrap();
        let root = uuid::Uuid::new_v4().to_string();
        let mut record = crate::executions::prepare(
            home.path(),
            &root,
            &json!({"root":root,"turn":"origin","goal":null}),
            &json!({"schema":1,"topic":"resolved gap","argv":["/usr/bin/true"],"cwd":home.path()}),
        )
        .unwrap();
        let id = record["id"].as_str().unwrap().to_owned();
        record["state"] = json!("launching");
        let state = home
            .path()
            .join("codex-plus/executions/v1")
            .join(&root)
            .join(&id)
            .join("state.json");
        crate::local::atomic_json(&state, &record).unwrap();
        assert_eq!(
            crate::executions::reconcile(home.path(), &root, &id).unwrap()["state"],
            "unknown"
        );
        assert_eq!(hold_status(home.path()).unwrap()["execution_quiet"], false);
        let resolved = crate::executions::resolve(home.path(), &root, &id,
            &json!({"root":root,"id":id,"cleanup_verified":true,"evidence":"fixture verified absent executor and no workload effects"})).unwrap();
        assert_eq!(resolved["state"], "unknown");
        assert!(resolved["result"].is_null());
        assert_eq!(hold_status(home.path()).unwrap()["execution_quiet"], true);
    }

    #[test]
    fn unwatched_execution_roots_cannot_claim_quiet_and_corrupt_records_fail_closed() {
        let home = tempfile::tempdir().unwrap();
        let root = uuid::Uuid::new_v4().to_string();
        let mut record = crate::executions::prepare(
            home.path(),
            &root,
            &json!({"root":root,"turn":"origin","goal":null}),
            &json!({"schema":1,"topic":"unwatched","argv":["/usr/bin/true"],"cwd":home.path()}),
        )
        .unwrap();
        record["state"] = json!("launching");
        let state = home
            .path()
            .join("codex-plus/executions/v1")
            .join(&root)
            .join(record["id"].as_str().unwrap())
            .join("state.json");
        crate::local::atomic_json(&state, &record).unwrap();
        let status = hold_enter(home.path()).unwrap();
        assert_eq!(status["held"], true);
        assert_eq!(status["admission_quiet"], false);
        assert_eq!(status["execution_quiet"], false);
        assert_eq!(status["execution_summary"]["nonquiet_total"], 1);
        assert_eq!(status["roots"][0]["root"], root);
        std::fs::write(&state, b"{partial").unwrap();
        assert!(hold_status(home.path()).is_err());
        assert!(is_held(home.path()).unwrap());
    }

    #[test]
    fn execution_quiet_requires_terminal_receipt_and_resolved_cleanup() {
        for state in [
            "launching",
            "running",
            "cancel_requested",
            "unknown",
            "unexpected",
        ] {
            assert!(!execution_is_quiet(&json!({"state":state})));
        }
        assert!(execution_is_quiet(&json!({"state":"prepared"})));
        assert!(!execution_is_quiet(&json!({"state":"completed"})));
        assert!(!execution_is_quiet(
            &json!({"state":"failed","result":{"cleanup":{"status":"unknown"}}})
        ));
        assert!(execution_is_quiet(
            &json!({"state":"failed","result":{"cleanup":{"status":"unknown"}},"resolution":{"cleanup_verified":true}})
        ));
        assert!(execution_is_quiet(
            &json!({"state":"completed","result":{"cleanup":{"status":"not_required"}}})
        ));
    }
    #[test]
    fn hold_survives_reopen_blocks_admission_and_leaves_intent_untouched() {
        let temp = tempfile::tempdir().unwrap();
        let home = temp.path();
        let guard = admission(home).unwrap();
        assert!(hold_enter(home).is_err());
        drop(guard);
        assert_eq!(hold_enter(home).unwrap()["admission_quiet"], true);
        assert!(
            admission(home)
                .unwrap_err()
                .downcast_ref::<Held>()
                .is_some()
        );
        assert_eq!(hold_status(home).unwrap()["held"], true);
        hold_leave(home).unwrap();
        assert!(admission(home).is_ok());
    }
    #[test]
    fn corrupt_and_symlink_holds_fail_closed() {
        let temp = tempfile::tempdir().unwrap();
        crate::local::state_directory(temp.path()).unwrap();
        std::fs::write(path(temp.path()), b"bad").unwrap();
        assert!(admission(temp.path()).is_err());
        std::fs::remove_file(path(temp.path())).unwrap();
        std::os::unix::fs::symlink("missing", path(temp.path())).unwrap();
        assert!(admission(temp.path()).is_err());
    }
}
