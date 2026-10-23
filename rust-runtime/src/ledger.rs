//! Durable ownership and lifecycle policy for registered completion callbacks.
use anyhow::{Result, anyhow, bail};
use serde_json::{Value, json};
use sqlx::{Column, Connection, Row, SqliteConnection, TypeInfo, ValueRef};
use std::collections::{HashMap, HashSet};
use std::fs::{self, OpenOptions};
use std::io::Read;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::Duration;
use tokio::runtime::Runtime;
use uuid::Uuid;

pub const MAX_SECONDS: f64 = 24.0 * 60.0 * 60.0;
pub const TERMINAL: &[&str] = &["completed", "failed", "interrupted", "lost"];
pub const UPGRADE_WARNING: &str = "Coordinator upgrade unavailable; continuing installed observer.";
const CLAIMABLE: &str = "park IN (SELECT id FROM parks WHERE parent=? AND state IN ('armed','complete')) AND (((status='armed' OR callback IS NULL OR callback IN (SELECT id FROM callbacks WHERE state='ready' AND message IS NULL)) AND park IN (SELECT id FROM parks WHERE state='armed')) OR (recovery_allowed=1 AND callback IN (SELECT id FROM callbacks WHERE state IN ('goal_owned','goal_settled')) AND NOT EXISTS (SELECT 1 FROM goal_dispatch_jobs d JOIN goal_dispatches g ON g.id=d.dispatch WHERE d.job=jobs.id AND g.state NOT IN ('ready','invalidated'))))";
const JOB_TERMINAL: &str = "jobs.status IN ('completed','failed','interrupted','lost')";
const CALLBACK_OPEN: &str = "state IN ('ready','pending','starting','in_flight','uncertain','cancel_requested','cancel_uncertain')";

#[cfg(test)]
thread_local! {
    static BATCH_SELECTED: std::cell::RefCell<Option<Box<dyn FnOnce()>>> = Default::default();
}

#[derive(Debug)]
pub struct RegistrationError(pub String);
impl std::fmt::Display for RegistrationError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for RegistrationError {}

#[derive(Clone, Debug)]
pub struct ResolvedJob {
    pub id: String,
    pub kind: String,
    pub thread: String,
    pub turn: Option<String>,
    pub item: Option<String>,
    pub process: Option<String>,
    pub execution: Option<String>,
    pub status: String,
    pub exit_code: Option<i64>,
}

#[derive(Default)]
pub struct ReconcileEvidence {
    pub consumed: bool,
    pub queued: Option<Value>,
    pub deleted: Option<bool>,
    pub receipt_absent: bool,
    pub native_unavailable: bool,
}

pub struct Store {
    pub directory: PathBuf,
    pub supervisor_failures: u32,
    db: Option<SqliteConnection>,
    runtime: Runtime,
}

fn truth(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}
fn text(value: &Value) -> &str {
    value.as_str().unwrap_or("")
}
fn python_text(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(s) => s.clone(),
        _ => value.to_string(),
    }
}
fn label(value: &Value) -> String {
    python_text(value)
        .split(crate::python_text::whitespace)
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(96)
        .collect()
}
fn unsettled(final_states: &str) -> String {
    format!(
        "NOT EXISTS (SELECT 1 FROM goal_jobs WHERE goal_jobs.job=jobs.id AND {JOB_TERMINAL}) AND (jobs.status='armed' OR ({JOB_TERMINAL} AND (jobs.callback IS NULL OR callbacks.state NOT IN ({final_states}))))"
    )
}

pub(crate) fn execution_refs(directory: &Path, root: &str, execution: &str) -> Value {
    let directory = directory.join("executions/v1").join(root).join(execution);
    json!({"state":directory.join("state.json"),"result":directory.join("result.json"),"log":directory.join("stdout.log"),"cleanup_log":directory.join("cleanup.log")})
}

pub(crate) fn execution_cleanup_unresolved(record: &Value) -> bool {
    record["result"].is_object()
        && !matches!(
            record["result"]["cleanup"]["status"].as_str(),
            Some("not_required" | "completed")
        )
        && record["resolution"]["cleanup_verified"] != true
}

pub(crate) fn execution_recovery_required(record: &Value) -> bool {
    record["state"] == "unknown" || execution_cleanup_unresolved(record)
}

pub(crate) fn add_execution_refs(directory: &Path, root: &str, results: &mut [Value]) {
    for result in results {
        if let Some(id) = result["execution"].as_str() {
            let refs = execution_refs(directory, root, id);
            match crate::executions::read(directory.parent().unwrap(), root, id) {
                Ok(record) => {
                    result["recovery_required"] =
                        json!(result["status"] == "lost" || execution_recovery_required(&record));
                }
                Err(error) => {
                    result["recovery_required"] = json!(true);
                    result["evidence_status"] = json!("unavailable");
                    result["evidence_error"] = json!(format!("{error:#}"));
                }
            }
            result["result_refs"] = refs;
        }
    }
}

impl Store {
    pub fn open(home: &Path) -> Result<Self> {
        let directory = crate::local::state_directory(home)?;
        let path = directory.join("background.sqlite");
        if fs::symlink_metadata(&path).is_ok_and(|m| m.file_type().is_symlink()) {
            bail!("background state must not be a symlink");
        }
        drop(
            OpenOptions::new()
                .create(true)
                .truncate(false)
                .read(true)
                .write(true)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW)
                .open(&path)?,
        );
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600))?;
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        // Leave the journal mode untouched, matching sqlite3's existing-file behavior.
        let options = sqlx::sqlite::SqliteConnectOptions::new()
            .filename(path)
            .busy_timeout(Duration::from_secs(10))
            .synchronous(sqlx::sqlite::SqliteSynchronous::Full);
        let db = runtime.block_on(SqliteConnection::connect_with(&options))?;
        let mut store = Self {
            directory,
            supervisor_failures: 0,
            db: Some(db),
            runtime,
        };
        for sql in [
            "CREATE TABLE IF NOT EXISTS parks (id TEXT PRIMARY KEY, parent TEXT NOT NULL, origin TEXT NOT NULL, reason TEXT NOT NULL, state TEXT NOT NULL, deadline REAL NOT NULL, error TEXT)",
            "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, park TEXT NOT NULL, kind TEXT NOT NULL, thread TEXT NOT NULL, turn TEXT, item TEXT, process TEXT, status TEXT NOT NULL, exit_code INTEGER, callback TEXT)",
            "CREATE TABLE IF NOT EXISTS callbacks (id TEXT PRIMARY KEY, parent TEXT NOT NULL, client TEXT UNIQUE NOT NULL, state TEXT NOT NULL, queue TEXT, error TEXT)",
        ] {
            store.execute(sql, &[])?;
        }
        store.transaction(true, |s| {
            for (table, columns) in [
                (
                    "parks",
                    vec![
                        ("error", "TEXT"),
                        ("generation", "TEXT"),
                        ("recovery_generation", "TEXT"),
                        ("origin_pending", "INTEGER NOT NULL DEFAULT 0"),
                        ("cancelled_at", "REAL"),
                        ("cancel_kind", "TEXT"),
                        ("cancel_evidence", "TEXT"),
                    ],
                ),
                (
                    "callbacks",
                    vec![
                        ("reconcile_attempts", "INTEGER NOT NULL DEFAULT 0"),
                        ("idempotent", "INTEGER NOT NULL DEFAULT 0"),
                        ("message", "TEXT"),
                    ],
                ),
                (
                    "jobs",
                    vec![
                        ("execution", "TEXT"),
                        ("observer_run", "TEXT"),
                        ("recovery_pending", "INTEGER NOT NULL DEFAULT 1"),
                        ("report", "INTEGER NOT NULL DEFAULT 0"),
                        ("recovery_allowed", "INTEGER NOT NULL DEFAULT 0"),
                        ("label", "TEXT"),
                        ("interruption_pending", "INTEGER NOT NULL DEFAULT 0"),
                    ],
                ),
            ] {
                let existing = s.rows(&format!("PRAGMA table_info({table})"), &[])?;
                for (name, definition) in columns {
                    if !existing.iter().any(|r| r["name"] == name) {
                        s.execute(
                            &format!("ALTER TABLE {table} ADD COLUMN {name} {definition}"),
                            &[],
                        )?;
                    }
                }
            }
            Ok(())
        })?;
        store.init_goals()?;
        Ok(store)
    }

    fn query<'a>(
        sql: &'a str,
        args: &'a [Value],
    ) -> sqlx::query::Query<'a, sqlx::Sqlite, sqlx::sqlite::SqliteArguments> {
        // SQL structure comes only from module constants; all external values are bound.
        let mut q = sqlx::query(sqlx::AssertSqlSafe(sql));
        for value in args {
            q = match value {
                Value::Null => q.bind(None::<String>),
                Value::String(s) => q.bind(s),
                Value::Bool(b) => q.bind(i64::from(*b)),
                Value::Number(n) if n.is_i64() => q.bind(n.as_i64().unwrap()),
                Value::Number(n) => q.bind(n.as_f64().unwrap()),
                _ => q.bind(value.to_string()),
            };
        }
        q
    }
    pub(crate) fn execute(&mut self, sql: &str, args: &[Value]) -> Result<()> {
        self.runtime
            .block_on(Self::query(sql, args).execute(self.db.as_mut().unwrap()))?;
        Ok(())
    }
    pub(crate) fn rows(&mut self, sql: &str, args: &[Value]) -> Result<Vec<Value>> {
        let rows = self
            .runtime
            .block_on(Self::query(sql, args).fetch_all(self.db.as_mut().unwrap()))?;
        rows.into_iter()
            .map(|row| {
                let mut object = serde_json::Map::new();
                for (index, column) in row.columns().iter().enumerate() {
                    let raw = row.try_get_raw(index)?;
                    let value = if raw.is_null() {
                        Value::Null
                    } else {
                        match raw.type_info().name() {
                            "INTEGER" => json!(row.try_get::<i64, _>(index)?),
                            "REAL" => json!(row.try_get::<f64, _>(index)?),
                            _ => json!(row.try_get::<String, _>(index)?),
                        }
                    };
                    object.insert(column.name().into(), value);
                }
                Ok(Value::Object(object))
            })
            .collect()
    }
    pub(crate) fn first(&mut self, sql: &str, args: &[Value]) -> Result<Option<Value>> {
        Ok(self.rows(sql, args)?.into_iter().next())
    }
    pub(crate) fn exists(&mut self, sql: &str, args: &[Value]) -> Result<bool> {
        Ok(self.first(sql, args)?.is_some())
    }
    pub(crate) fn transaction<T>(
        &mut self,
        immediate: bool,
        work: impl FnOnce(&mut Self) -> Result<T>,
    ) -> Result<T> {
        self.execute(
            if immediate {
                "BEGIN IMMEDIATE"
            } else {
                "BEGIN"
            },
            &[],
        )?;
        match work(self) {
            Ok(result) => match self.execute("COMMIT", &[]) {
                Ok(()) => Ok(result),
                Err(e) => {
                    let _ = self.execute("ROLLBACK", &[]);
                    Err(e)
                }
            },
            Err(e) => {
                let _ = self.execute("ROLLBACK", &[]);
                Err(e)
            }
        }
    }

    pub fn status(&mut self, parent: &str) -> Result<Value> {
        let mut parks = self.rows("SELECT * FROM parks WHERE parent=?", &[json!(parent)])?;
        let mut jobs = self.rows(
            "SELECT jobs.* FROM jobs JOIN parks ON parks.id=jobs.park WHERE parks.parent=?",
            &[json!(parent)],
        )?;
        add_execution_refs(&self.directory, parent, &mut jobs);
        let callbacks = self.rows("SELECT * FROM callbacks WHERE parent=?", &[json!(parent)])?;
        for park in &mut parks {
            let reason = if truth(&park["origin_pending"]) {
                Some(
                    "Interrupted origin lacks positive terminal evidence; delivery and recovery resume are paused.",
                )
            } else if jobs
                .iter()
                .any(|j| j["park"] == park["id"] && truth(&j["interruption_pending"]))
            {
                Some(
                    "Interrupted worker lacks positive terminal evidence; its outcome remains pending.",
                )
            } else {
                None
            };
            park["pending_reason"] = if park["state"] == "armed" {
                json!(reason)
            } else {
                Value::Null
            };
            if truth(&park["pending_reason"]) {
                park["monitoring_error"] = park["error"].clone();
                park["error"] = json!(reason);
            }
        }
        Ok(
            json!({"parent":parent,"parks":parks,"jobs":jobs,"callbacks":callbacks,"health":self.health(parent,None)?}),
        )
    }

    pub fn health(&mut self, parent: &str, updated_at: Option<f64>) -> Result<Value> {
        let jobs = self.rows(&format!("SELECT jobs.report,jobs.status FROM jobs JOIN parks ON parks.id=jobs.park LEFT JOIN callbacks ON callbacks.id=jobs.callback WHERE parks.parent=? AND parks.state='armed' AND {}", unsettled("'delivered','cancelled','expired'")), &[json!(parent)])?;
        let uncertain = self.rows("SELECT COUNT(*) AS count FROM callbacks WHERE parent=? AND state IN ('uncertain','cancel_uncertain')", &[json!(parent)])?[0]["count"].as_i64().unwrap();
        let error = self.exists("SELECT 1 FROM parks WHERE parent=? AND state<>'cancelled' AND (error IS NOT NULL OR origin_pending=1 OR EXISTS (SELECT 1 FROM jobs WHERE jobs.park=parks.id AND interruption_pending=1)) UNION ALL SELECT 1 FROM callbacks WHERE parent=? AND state NOT IN ('delivered','cancelled','expired','goal_owned','goal_settled') AND error IS NOT NULL LIMIT 1", &[json!(parent),json!(parent)])?;
        let reports: i64 = jobs.iter().map(|j| j["report"].as_i64().unwrap_or(0)).sum();
        let running = jobs.iter().filter(|j| j["status"] == "armed").count();
        let state = if uncertain != 0 {
            "uncertain"
        } else if error {
            "error"
        } else if running != 0 {
            "waiting"
        } else if reports != 0 {
            "report"
        } else if !jobs.is_empty() {
            "waiting"
        } else {
            "idle"
        };
        let updated_at = updated_at.map(|n| json!(n)).unwrap_or_else(|| {
            let read = || -> Result<Value> {
                let file = OpenOptions::new()
                    .read(true)
                    .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
                    .open(self.directory.join(format!("{parent}.status.json")))?;
                if !file.metadata()?.is_file() {
                    bail!("not a regular status file");
                }
                let mut bytes = Vec::new();
                file.take(1024).read_to_end(&mut bytes)?;
                let view: Value = serde_json::from_slice(&bytes)?;
                if view["parent"] == parent
                    && (view["updated_at"].is_number() || view["updated_at"].is_boolean())
                {
                    Ok(view["updated_at"].clone())
                } else {
                    Ok(json!(0))
                }
            };
            read().unwrap_or(json!(0))
        });
        Ok(
            json!({"version":1,"parent":parent,"updated_at":updated_at,"state":state,"jobs":running,"uncertain":uncertain,"reports":reports}),
        )
    }
    pub fn publish(&mut self, parent: &str) -> Result<Value> {
        let parent = Uuid::parse_str(parent)?.to_string();
        let view = self.health(&parent, Some(crate::local::now()))?;
        crate::local::atomic_json(&self.directory.join(format!("{parent}.status.json")), &view)?;
        self.goal_publish(&parent)?;
        Ok(view)
    }

    pub fn register_resolved(
        &mut self,
        parent: &str,
        origin: &str,
        reason: &str,
        resolved: &[ResolvedJob],
        labels: &HashMap<String, Option<String>>,
        generation: Option<&str>,
        pending: &HashSet<String>,
    ) -> Result<Value> {
        // Python dict replacement keeps the first insertion position and last value.
        let mut unique: Vec<&ResolvedJob> = Vec::new();
        for job in resolved {
            if let Some(index) = unique.iter().position(|j| j.id == job.id) {
                unique[index] = job;
            } else {
                unique.push(job);
            }
        }
        let result = self.transaction(true, |s| {
            let (mut armed, mut existing, mut finished) = (Vec::new(), Vec::new(), Vec::new());
            for job in unique {
                let prior = s.first("SELECT jobs.status,parks.state,callbacks.state AS delivery FROM jobs JOIN parks ON parks.id=jobs.park LEFT JOIN callbacks ON callbacks.id=jobs.callback WHERE jobs.id=?", &[json!(job.id)])?;
                let mut report = json!({"id":job.id,"kind":job.kind,"turn":job.turn,"status":job.status});
                if let Some(execution) = &job.execution {
                    report["execution"] = json!(execution);
                    report["exit_code"] = json!(job.exit_code);
                    report["result_refs"] = execution_refs(&s.directory, parent, execution);
                }
                if let Some(prior) = prior {
                    if ["cancelled","expired"].contains(&text(&prior["state"])) || ["cancelled","expired"].contains(&text(&prior["status"])) {
                        return Err(RegistrationError("Selected job was cancelled or expired; it cannot be rearmed. Select a new worker turn or command item.".into()).into());
                    }
                    report["status"] = prior["status"].clone(); report["delivery"] = prior["delivery"].clone();
                    if prior["delivery"] == "delivered" || prior["state"] == "complete" { finished.push(report); } else { existing.push(report); }
                } else if TERMINAL.contains(&job.status.as_str()) { finished.push(report); } else { armed.push(job); }
            }
            let park = if armed.is_empty() { None } else { Some(Uuid::new_v4().to_string()) };
            if let Some(park) = &park {
                s.execute("INSERT INTO parks (id,parent,origin,reason,state,deadline,generation) VALUES (?,?,?,?,?,?,?)", &[json!(park),json!(parent),json!(origin),json!(reason),json!("armed"),json!(crate::local::now()+MAX_SECONDS),json!(generation)])?;
                for job in &armed {
                    let source = if job.kind == "worker" { json!(labels.get(&job.thread).and_then(|x| x.as_ref()).filter(|x| !x.is_empty()).unwrap_or(&job.thread)) } else if job.kind == "execution" { json!(job.execution) } else { json!(job.process) };
                    s.execute("INSERT INTO jobs (id,park,kind,thread,turn,item,process,execution,status,exit_code,callback,recovery_pending,label) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,0,?)", &[json!(job.id),json!(park),json!(job.kind),json!(job.thread),json!(job.turn),json!(job.item),json!(job.process),json!(job.execution),json!(job.status),json!(job.exit_code),json!(label(&source))])?;
                    if pending.contains(&job.id) { s.execute("UPDATE jobs SET interruption_pending=1 WHERE id=?", &[json!(job.id)])?; }
                }
                s.goal_bind_jobs(parent,origin,&armed)?;
            }
            Ok(json!({"parent":parent,"park":park,"state":if !armed.is_empty(){"armed"}else if !existing.is_empty(){"already_registered"}else{"already_finished"},"armed":armed.iter().map(|j|j.id.clone()).collect::<Vec<_>>(),"already_registered":existing,"already_finished":finished}))
        })?;
        self.publish(parent)?;
        Ok(result)
    }

    pub fn cancel(
        &mut self,
        parent: &str,
        origin: Option<&str>,
        evidence: Option<&Value>,
    ) -> Result<()> {
        let confirmed_origin = evidence.is_some_and(truth);
        let origin = origin.filter(|s| !s.is_empty());
        let scope = if origin.is_some() {
            "parent=? AND origin=?"
        } else {
            "parent=?"
        };
        let mut args = vec![json!(parent)];
        if let Some(origin) = origin {
            args.push(json!(origin));
        }
        self.transaction(true, |s| {
            for park in s.rows(&format!("SELECT * FROM parks WHERE {scope} AND state<>'cancelled'"), &args)? {
                let default = json!({"source":"explicit"});
                let evidence = evidence.filter(|v| truth(v)).unwrap_or(&default);
                let mut provenance = serde_json::Map::new();
                if let Some(evidence) = evidence.as_object() {
                    for (key,value) in evidence {
                        if ["status","completedAt","source","event","timestamp","observer","generation"].contains(&key.as_str()) {
                            provenance.insert(key.clone(), value.as_str().map(|v|json!(v.chars().take(160).collect::<String>())).unwrap_or_else(||value.clone()));
                        }
                    }
                }
                let owner = s.first("SELECT observer_run FROM jobs WHERE park=? AND observer_run IS NOT NULL LIMIT 1", &[park["id"].clone()])?;
                provenance.entry("observer").or_insert_with(||owner.map(|o|o["observer_run"].clone()).unwrap_or(Value::Null));
                provenance.entry("generation").or_insert(park["generation"].clone());
                provenance.insert("previous_error".into(),park["error"].clone());
                // Python's compact json.dumps keeps ensure_ascii=True for stored provenance.
                let serialized = crate::local::ascii_json(&Value::Object(provenance))?;
                s.execute("UPDATE parks SET state='cancelled',error=NULL,origin_pending=0,cancelled_at=?,cancel_kind=?,cancel_evidence=? WHERE id=?", &[json!(crate::local::now()),json!(if confirmed_origin { "confirmed_origin" } else { "explicit" }),json!(serialized),park["id"].clone()])?;
            }
            s.execute(&format!("UPDATE jobs SET status='cancelled',interruption_pending=0 WHERE status='armed' AND park IN (SELECT id FROM parks WHERE {scope})"), &args)?;
            s.execute(&format!("UPDATE callbacks SET state=CASE WHEN state='ready' THEN 'cancelled' ELSE 'cancel_requested' END WHERE id IN (SELECT callback FROM jobs WHERE park IN (SELECT id FROM parks WHERE {scope})) AND state IN ('ready','in_flight','uncertain','pending','starting')"), &args)?;
            Ok(())
        })?;
        let mut args = vec![json!(parent)];
        if let Some(origin) = origin {
            args.push(json!(origin));
        }
        let cancelled = self.rows(&format!("SELECT jobs.execution,jobs.park FROM jobs JOIN parks ON parks.id=jobs.park WHERE parks.state='cancelled' AND jobs.execution IS NOT NULL AND {}", if origin.is_some() {"parks.parent=? AND parks.origin=?"} else {"parks.parent=?"}), &args)?;
        for job in cancelled {
            crate::executions::mark_observation(
                self.directory
                    .parent()
                    .ok_or_else(|| anyhow!("state directory has no home"))?,
                parent,
                text(&job["execution"]),
                "cancelled",
                &json!({"source":"watch_cancelled","park":job["park"]}),
            )?;
        }
        self.publish(parent)?;
        Ok(())
    }
    pub fn origin_pending(&mut self, parent: &str, origin: &str, pending: bool) -> Result<()> {
        self.execute(
            "UPDATE parks SET origin_pending=? WHERE parent=? AND origin=? AND state='armed'",
            &[json!(i64::from(pending)), json!(parent), json!(origin)],
        )
    }
    pub fn worker_pending(&mut self, job: &str, pending: bool) -> Result<()> {
        self.execute(
            "UPDATE jobs SET interruption_pending=? WHERE id=? AND status='armed'",
            &[json!(i64::from(pending)), json!(job)],
        )
    }
    pub fn claim_observation(
        &mut self,
        parent: &str,
        run_id: &str,
        recovering: bool,
    ) -> Result<()> {
        self.transaction(false, |s| {
            s.execute(&format!("UPDATE jobs SET recovery_pending=1,recovery_allowed=0 WHERE {CLAIMABLE} AND observer_run IS NOT NULL AND observer_run<>?"), &[json!(parent),json!(run_id)])?;
            s.execute(&format!("UPDATE jobs SET report=1 WHERE {CLAIMABLE} AND recovery_pending=1 AND status IN ('completed','failed','interrupted','lost')"), &[json!(parent)])?;
            s.execute(&format!("UPDATE jobs SET observer_run=? WHERE {CLAIMABLE}"), &[json!(run_id),json!(parent)])?;
            if recovering { s.execute("UPDATE callbacks SET state='uncertain' WHERE parent=? AND state IN ('in_flight','starting')", &[json!(parent)])?; }
            Ok(())
        })
    }
    pub fn observe_generation(&mut self, parent: &str, generation: Option<&str>) -> Result<bool> {
        let Some(generation) = generation else {
            return Ok(false);
        };
        self.transaction(false, |s| {
            s.execute("UPDATE parks SET generation=? WHERE parent=? AND state='armed' AND generation IS NULL", &[json!(generation),json!(parent)])?;
            s.execute(&format!("UPDATE jobs SET recovery_pending=1,recovery_allowed=0,report=CASE WHEN status IN ('completed','failed','interrupted','lost') THEN 1 ELSE report END WHERE {CLAIMABLE} AND park IN (SELECT id FROM parks WHERE generation<>? AND (recovery_generation IS NULL OR recovery_generation<>?))"), &[json!(parent),json!(generation),json!(generation)])?;
            s.execute("UPDATE parks SET recovery_generation=? WHERE parent=? AND state='armed' AND generation<>?", &[json!(generation),json!(parent),json!(generation)])
        })?;
        self.exists("SELECT 1 FROM parks WHERE parent=? AND state='armed' AND recovery_generation IS NOT NULL LIMIT 1", &[json!(parent)])
    }
    pub fn active_parks(&mut self, parent: &str) -> Result<Vec<Value>> {
        self.rows(
            "SELECT parks.*,EXISTS(SELECT 1 FROM jobs JOIN goal_jobs ON goal_jobs.job=jobs.id WHERE jobs.park=parks.id) AS goal_bound FROM parks WHERE parent=? AND state='armed'",
            &[json!(parent)],
        )
    }
    pub fn can_resume(&mut self, parent: &str, queue: &[Value], native: bool) -> Result<bool> {
        if self.active_parks(parent)?.is_empty()
            || self.exists(
                "SELECT 1 FROM parks WHERE parent=? AND state='armed' AND origin_pending=1 LIMIT 1",
                &[json!(parent)],
            )?
        {
            return Ok(false);
        }
        let callbacks = self.rows(
            "SELECT client,state,idempotent FROM callbacks WHERE parent=?",
            &[json!(parent)],
        )?;
        Ok(queue.iter().all(|q| {
            callbacks.iter().any(|c| {
                q.get("clientUserMessageId") == c.get("client")
                    && ["pending", "uncertain", "starting", "in_flight"]
                        .contains(&text(&c["state"]))
                    && (!truth(&c["idempotent"]) || native)
            })
        }))
    }
    pub fn expire_registration(&mut self, park: &str) -> Result<()> {
        self.transaction(false, |s| {
            s.execute("UPDATE parks SET state='expired',error='Registration duration expired; completion remains unknown.' WHERE id=? AND state='armed' AND NOT EXISTS(SELECT 1 FROM jobs WHERE jobs.park=parks.id AND kind='execution')", &[json!(park)])?;
            s.execute("UPDATE jobs SET status='expired',interruption_pending=0 WHERE park=? AND status='armed' AND kind<>'execution'", &[json!(park)])?;
            s.execute("UPDATE callbacks SET state=CASE WHEN state='ready' THEN 'expired' ELSE 'cancel_requested' END WHERE id IN (SELECT callback FROM jobs WHERE park=?) AND state IN ('ready','pending','starting','in_flight') AND NOT EXISTS(SELECT 1 FROM jobs WHERE jobs.callback=callbacks.id AND kind='execution')", &[json!(park)])
        })
    }
    pub fn jobs_to_observe(&mut self, parent: &str) -> Result<Vec<Value>> {
        self.rows("SELECT jobs.*,parks.recovery_generation IS NOT NULL AS verify_running FROM jobs JOIN parks ON parks.id=jobs.park WHERE parks.parent=? AND parks.state='armed' AND jobs.status='armed'", &[json!(parent)])
    }
    pub fn has_execution_watches(&mut self, parent: &str) -> Result<bool> {
        self.exists("SELECT 1 FROM jobs JOIN parks ON parks.id=jobs.park WHERE parks.parent=? AND parks.state='armed' AND jobs.kind='execution' LIMIT 1", &[json!(parent)])
    }
    pub fn observe_job(
        &mut self,
        job: &Value,
        status: Option<&str>,
        exit_code: Option<i64>,
        turn: Option<&str>,
        running: bool,
    ) -> Result<()> {
        if running {
            return self.execute(
                "UPDATE jobs SET recovery_pending=0 WHERE id=? AND status='armed'",
                &[job["id"].clone()],
            );
        }
        let Some(current) = self.first("SELECT jobs.status,parks.recovery_generation FROM jobs JOIN parks ON parks.id=jobs.park WHERE jobs.id=?", &[job["id"].clone()])? else { return Ok(()); };
        if current["status"] != "armed" {
            return Ok(());
        }
        let (status, exit_code) = if !status.is_some_and(|v| TERMINAL.contains(&v))
            && truth(&current["recovery_generation"])
        {
            (Some("lost"), None)
        } else {
            (status, exit_code)
        };
        if status.is_some_and(|v| TERMINAL.contains(&v)) {
            // Legacy helpers see report-only; continuous observation permits bounded recovery in this helper.
            self.execute("UPDATE jobs SET status=?,exit_code=?,turn=COALESCE(turn,?),recovery_allowed=CASE WHEN ? AND report=0 AND recovery_pending=0 AND observer_run=? THEN 1 ELSE 0 END,report=CASE WHEN ? OR (?='lost' AND kind='execution') THEN 1 ELSE MAX(report,recovery_pending) END,recovery_pending=0 WHERE id=? AND status='armed'", &[json!(status),json!(exit_code),json!(turn),json!(job["live_recovery"] == true && !truth(&current["recovery_generation"])),job["observer_run"].clone(),json!(job["execution_recovery"] == true),json!(status),job["id"].clone()])?;
        }
        Ok(())
    }
    pub fn finish_recovery(&mut self, parent: &str) -> Result<()> {
        self.execute("UPDATE parks SET generation=recovery_generation,recovery_generation=NULL WHERE parent=? AND state='armed' AND recovery_generation IS NOT NULL AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.park=parks.id AND status='armed' AND interruption_pending=1)", &[json!(parent)])
    }
    pub fn batch(&mut self, parent: &str) -> Result<()> {
        self.transaction(true, |s| {
            // Retire callbacks created by older batching races, before they can block fresh work.
            s.execute("UPDATE callbacks SET state=CASE WHEN EXISTS (SELECT 1 FROM jobs JOIN parks ON parks.id=jobs.park WHERE jobs.callback=callbacks.id AND parks.state='cancelled') THEN 'cancelled' ELSE 'expired' END WHERE parent=? AND state='ready' AND message IS NULL AND queue IS NULL AND EXISTS (SELECT 1 FROM jobs JOIN parks ON parks.id=jobs.park WHERE jobs.callback=callbacks.id AND parks.state IN ('cancelled','expired'))", &[json!(parent)])?;
            let rows = s.rows(&format!("SELECT jobs.id,jobs.report,jobs.recovery_allowed,parks.origin FROM jobs JOIN parks ON parks.id=jobs.park WHERE parks.parent=? AND parks.state='armed' AND {JOB_TERMINAL} AND jobs.callback IS NULL AND NOT EXISTS (SELECT 1 FROM goal_jobs WHERE goal_jobs.job=jobs.id)"), &[json!(parent)])?;
            #[cfg(test)]
            BATCH_SELECTED.with(|hook| {
                if let Some(hook) = hook.borrow_mut().take() { hook(); }
            });
            let Some(first) = rows.first() else { return Ok(()); };
            let id = Uuid::new_v4().to_string();
            s.execute("INSERT INTO callbacks (id,parent,client,state,queue,error) VALUES (?,?,?,'ready',NULL,NULL)", &[json!(id),json!(parent),json!(Uuid::new_v4().to_string())])?;
            for row in rows.iter().filter(|r|r["origin"]==first["origin"] && r["report"]==first["report"] && r["recovery_allowed"]==first["recovery_allowed"]) { s.execute("UPDATE jobs SET callback=? WHERE id=? AND callback IS NULL", &[json!(id),row["id"].clone()])?; }
            Ok(())
        })
    }
    fn message(&mut self, parent: &str, callback: &Value) -> Result<String> {
        let rows = self.rows(
            "SELECT * FROM jobs WHERE callback=?",
            &[callback["id"].clone()],
        )?;
        let remaining = self.rows("SELECT COUNT(*) AS count FROM jobs JOIN parks ON parks.id=jobs.park WHERE parks.parent=? AND jobs.status='armed' AND parks.state='armed'", &[json!(parent)])?[0]["count"].clone();
        let mut details = Vec::new();
        let mut recovery = rows.iter().any(|row| row["recovery_allowed"] == 1);
        for row in rows.iter().take(3) {
            let source = if truth(&row["label"]) {
                &row["label"]
            } else if row["kind"] == "worker" {
                &row["thread"]
            } else {
                &row["process"]
            };
            let mut detail = format!(
                "{} {} status={}",
                text(&row["kind"]),
                label(source),
                text(&row["status"])
            );
            if row["status"] == "lost" || row["kind"] == "execution" {
                let known_receipt = if let Some(execution) = row["execution"].as_str() {
                    let home = self
                        .directory
                        .parent()
                        .ok_or_else(|| anyhow!("state directory has no home"))?;
                    match crate::executions::read(home, parent, execution) {
                        Ok(record) => {
                            if record["result"].is_object()
                                && matches!(
                                    record["state"].as_str(),
                                    Some("completed" | "failed" | "cancelled")
                                )
                            {
                                Some(execution_cleanup_unresolved(&record))
                            } else {
                                None
                            }
                        }
                        Err(error) => {
                            // Missing evidence cannot erase terminal facts retained by the ledger.
                            recovery = true;
                            detail.push_str(" evidence=UNAVAILABLE");
                            detail.push_str(&format!(
                                " evidence_error={}",
                                json!(format!("{error:#}"))
                            ));
                            (row["status"] != "lost").then_some(true)
                        }
                    }
                } else {
                    None
                };
                detail.push_str(match known_receipt {
                    Some(true) => " cleanup=UNRESOLVED command_receipt=KNOWN",
                    Some(false) if row["status"] == "lost" => {
                        " cleanup=RESOLVED command_receipt=KNOWN"
                    }
                    Some(false) => "",
                    None => " outcome=UNKNOWN",
                });
            }
            if !row["exit_code"].is_null() {
                detail.push_str(&format!(" exit={}", row["exit_code"]));
            }
            if let Some(execution) = row["execution"].as_str() {
                detail.push_str(&format!(
                    " result_refs={}",
                    execution_refs(&self.directory, parent, execution)
                ));
            }
            details.push(detail);
        }
        let mut details = details.join("; ");
        if rows.len() > 3 {
            details.push_str(&format!("; +{} more", rows.len() - 3));
        }
        let report = rows
            .iter()
            .any(|r| truth(&r["report"]) && r["recovery_allowed"] != 1);
        let header = if report {
            "REPORT ONLY"
        } else if recovery {
            "RECOVERY REQUIRED"
        } else {
            "LIVE COMPLETION"
        };
        let warning = if report {
            "Do not resume earlier actions from this report. Do not rerun lost jobs. "
        } else if recovery {
            "Investigate only within current authorization; preserve original outcomes and resource claims. Do not blindly replay unknown work. Continue independent authorized work. "
        } else {
            ""
        };
        Ok(format!(
            "{header}: {details}. Remaining registered jobs: {remaining}. {warning}Results: codex-plus park --status --parent {parent}; callback={}.",
            text(&callback["id"])
        ))
    }
    pub fn callbacks_to_reconcile(&mut self, parent: &str) -> Result<Vec<Value>> {
        self.rows("SELECT * FROM callbacks WHERE parent=? AND state IN ('uncertain','pending','starting','cancel_requested','cancel_uncertain')", &[json!(parent)])
    }
    pub fn callback_cancelling(&mut self, callback: &Value) -> Result<bool> {
        Ok(["cancel_requested","cancel_uncertain"].contains(&text(&callback["state"])) || self.exists("SELECT 1 FROM jobs JOIN parks ON parks.id=jobs.park WHERE callback=? AND parks.state IN ('expired','cancelled') LIMIT 1", &[callback["id"].clone()])?)
    }
    pub fn record_receipt(
        &mut self,
        callback: &Value,
        receipt_state: &str,
        queue: Option<&str>,
    ) -> Result<()> {
        let state = match receipt_state {
            "pending" => "pending",
            "consumed" => "delivered",
            "cancelled" => "cancelled",
            _ => return Err(anyhow!("invalid receipt state: {receipt_state}")),
        };
        self.execute("UPDATE callbacks SET state=CASE WHEN ?='pending' AND state IN ('cancel_requested','cancel_uncertain') THEN state ELSE ? END,queue=COALESCE(?,queue),error=NULL,reconcile_attempts=0 WHERE id=?", &[json!(state),json!(state),json!(queue),callback["id"].clone()])
    }
    pub fn reconcile_evidence(
        &mut self,
        callback: &Value,
        evidence: &ReconcileEvidence,
    ) -> Result<()> {
        let cancelling = self.callback_cancelling(callback)?;
        if evidence.receipt_absent {
            self.execute("UPDATE callbacks SET state='ready',error=NULL,reconcile_attempts=0 WHERE id=? AND idempotent=1 AND state IN ('uncertain','pending','starting')", &[callback["id"].clone()])
        } else if evidence.consumed {
            self.execute("UPDATE callbacks SET state='delivered',error=NULL,reconcile_attempts=CASE WHEN ? THEN reconcile_attempts ELSE 0 END WHERE id=?", &[json!(i64::from(evidence.native_unavailable)),callback["id"].clone()])
        } else if evidence.native_unavailable {
            self.execute("UPDATE callbacks SET state=CASE WHEN state='cancel_requested' THEN 'cancel_uncertain' ELSE state END,error='Keyed callback waits for native queue idempotency support.' WHERE id=?", &[callback["id"].clone()])
        } else if let Some(queued) = evidence.queued.as_ref().filter(|v| truth(v)) {
            if cancelling && evidence.deleted != Some(true) {
                self.execute(
                    "UPDATE callbacks SET state='cancel_uncertain' WHERE id=?",
                    &[callback["id"].clone()],
                )
            } else {
                self.execute("UPDATE callbacks SET state=?,queue=?,error=NULL,reconcile_attempts=CASE WHEN ? THEN reconcile_attempts ELSE 0 END WHERE id=?", &[json!(if cancelling {"cancelled"}else{"pending"}),queued["id"].clone(),json!(i64::from(cancelling)),callback["id"].clone()])
            }
        } else {
            self.execute("UPDATE callbacks SET state=?,error=?,reconcile_attempts=reconcile_attempts+1 WHERE id=?", &[json!(if cancelling {"cancel_uncertain"}else{"uncertain"}),json!("No positive queue or consumed-message evidence; enqueue will not be retried."),callback["id"].clone()])
        }
    }
    pub fn reconciliation_limited(&mut self, callback: &Value, reason: &str) -> Result<()> {
        let cancelling = self.callback_cancelling(callback)?;
        self.execute("UPDATE callbacks SET state=?,error=?,reconcile_attempts=reconcile_attempts+1 WHERE id=?", &[json!(if cancelling {"cancel_uncertain"} else {"uncertain"}),json!(reason),callback["id"].clone()])
    }
    pub fn queue_owned(&mut self, parent: &str, queue: &[Value]) -> Result<bool> {
        let callbacks = self.rows(
            "SELECT client FROM callbacks WHERE parent=?",
            &[json!(parent)],
        )?;
        Ok(queue.iter().all(|q| {
            callbacks
                .iter()
                .any(|c| q.get("clientUserMessageId") == c.get("client"))
        }))
    }
    pub fn next_callback(
        &mut self,
        parent: &str,
        starting: bool,
        native: bool,
    ) -> Result<(Option<Value>, bool)> {
        let callback = self.first("SELECT * FROM callbacks WHERE parent=? AND state=? AND NOT EXISTS (SELECT 1 FROM jobs JOIN parks ON parks.id=jobs.park WHERE jobs.callback=callbacks.id AND parks.origin_pending=1) LIMIT 1", &[json!(parent),json!(if starting {"pending"}else{"ready"})])?;
        if let Some(c) = callback
            .as_ref()
            .filter(|c| truth(&c["idempotent"]) && !native)
        {
            if !starting {
                self.execute("UPDATE callbacks SET error='Keyed callback waits for native queue idempotency support.' WHERE id=?", &[c["id"].clone()])?;
            }
            return Ok((None, !starting));
        }
        Ok((callback, false))
    }
    pub fn claim_callback(
        &mut self,
        callback: &Value,
        starting: bool,
        native: bool,
    ) -> Result<Option<Value>> {
        self.transaction(true, |s| {
            let Some(current) = s.first("SELECT * FROM callbacks WHERE id=?", &[callback["id"].clone()])? else { return Ok(None); };
            let expected = if starting {"pending"}else{"ready"};
            if current["state"] != expected || s.callback_cancelling(&current)? || s.exists("SELECT 1 FROM jobs JOIN parks ON parks.id=jobs.park WHERE jobs.callback=? AND parks.origin_pending=1 LIMIT 1", &[current["id"].clone()])? || (truth(&current["idempotent"]) && !native) { return Ok(None); }
            if starting { s.execute("UPDATE callbacks SET state='starting' WHERE id=?", &[current["id"].clone()])?; }
            else {
                let message = if current["message"].is_null() { s.message(text(&current["parent"]),&current)? } else { text(&current["message"]).into() };
                s.execute("UPDATE callbacks SET state='in_flight',idempotent=MAX(idempotent,?),message=COALESCE(message,?) WHERE id=?", &[json!(i64::from(native)),json!(message),current["id"].clone()])?;
            }
            s.first("SELECT * FROM callbacks WHERE id=?", &[current["id"].clone()])
        })
    }
    pub fn dispatch_result(
        &mut self,
        callback: &Value,
        starting: bool,
        queued: Option<&str>,
        acknowledged: bool,
    ) -> Result<()> {
        let expected = if starting { "starting" } else { "in_flight" };
        let state = if starting && acknowledged {
            "delivered"
        } else if queued.is_some_and(|q| !q.is_empty()) {
            "pending"
        } else {
            "uncertain"
        };
        if starting && acknowledged {
            self.execute(
                "UPDATE callbacks SET state=?,error=NULL WHERE id=? AND state=? AND idempotent=0",
                &[json!(state), callback["id"].clone(), json!(expected)],
            )
        } else {
            self.execute(
                "UPDATE callbacks SET state=?,queue=COALESCE(?,queue) WHERE id=? AND state=?",
                &[
                    json!(state),
                    json!(queued),
                    callback["id"].clone(),
                    json!(expected),
                ],
            )
        }
    }
    pub fn has_work(&mut self, parent: &str) -> Result<bool> {
        Ok(!self.active_parks(parent)?.is_empty()
            || self.exists(
                &format!("SELECT 1 FROM callbacks WHERE parent=? AND {CALLBACK_OPEN} LIMIT 1"),
                &[json!(parent)],
            )?)
    }
    pub fn finish_cycle(&mut self, parent: &str) -> Result<bool> {
        self.execute("UPDATE callbacks SET state='goal_settled' WHERE parent=? AND state='goal_owned' AND EXISTS(SELECT 1 FROM jobs WHERE jobs.callback=callbacks.id) AND NOT EXISTS(SELECT 1 FROM jobs WHERE jobs.callback=callbacks.id AND jobs.status='armed')", &[json!(parent)])?;
        self.execute(&format!("UPDATE parks SET state='complete' WHERE parent=? AND state='armed' AND NOT EXISTS (SELECT 1 FROM jobs LEFT JOIN callbacks ON callbacks.id=jobs.callback WHERE jobs.park=parks.id AND {})",unsettled("'delivered','cancelled'")), &[json!(parent)])?;
        let active = self.exists(&format!("SELECT 1 FROM jobs JOIN parks ON parks.id=jobs.park WHERE parks.parent=? AND parks.state='armed' AND (jobs.status='armed' OR ({JOB_TERMINAL} AND jobs.callback IS NULL AND NOT EXISTS (SELECT 1 FROM goal_jobs WHERE goal_jobs.job=jobs.id))) LIMIT 1"), &[json!(parent)])?;
        let deliverable = self.exists("SELECT 1 FROM callbacks WHERE parent=? AND (state IN ('ready','pending','starting','in_flight','cancel_requested') OR (idempotent=1 AND state IN ('uncertain','cancel_uncertain')) OR (state='uncertain' AND queue IS NOT NULL AND reconcile_attempts<8)) LIMIT 1", &[json!(parent)])?;
        Ok(active || deliverable)
    }
    pub fn monitoring_succeeded(&mut self, parent: &str) -> Result<()> {
        self.transaction(false, |s| {
            s.execute("UPDATE parks SET error=NULL WHERE parent=? AND state NOT IN ('expired','cancelled') AND (state<>'armed' OR error IS NOT ?) AND origin_pending=0 AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.park=parks.id AND interruption_pending=1)", &[json!(parent),json!(UPGRADE_WARNING)])?;
            s.execute("UPDATE callbacks SET error=NULL WHERE parent=? AND state='ready' AND error LIKE 'RPC unavailable (%); no blind enqueue retry.'", &[json!(parent)])
        })
    }
    pub fn upgrade_warning(&mut self, parent: &str, invalid: bool) -> Result<()> {
        self.execute("UPDATE parks SET error=? WHERE parent=? AND state='armed' AND (error IS NULL OR error=?)", &[json!(if invalid {Some(UPGRADE_WARNING)}else{None}),json!(parent),json!(UPGRADE_WARNING)])
    }
    pub fn monitoring_failed(&mut self, parent: &str, error_type: &str) -> Result<()> {
        self.transaction(false, |s| {
            s.execute("UPDATE parks SET error=? WHERE parent=? AND state='armed'", &[json!(format!("Monitoring unavailable ({error_type}); completion remains unknown.")),json!(parent)])?;
            s.execute("UPDATE callbacks SET error=? WHERE parent=? AND state NOT IN ('delivered','cancelled','goal_owned','goal_settled')", &[json!(format!("RPC unavailable ({error_type}); no blind enqueue retry.")),json!(parent)])
        })
    }
    pub fn supervisor_failed(&mut self, parent: &str) -> Result<()> {
        self.transaction(false, |s| {
            s.execute("UPDATE parks SET error='Supervisor restart limit reached; observer recovery needs attention.' WHERE parent=? AND state='armed'", &[json!(parent)])?;
            s.execute("UPDATE callbacks SET error='Supervisor restart limit reached; observer recovery needs attention.' WHERE parent=? AND state NOT IN ('delivered','cancelled','expired','goal_owned','goal_settled')", &[json!(parent)])
        })?;
        self.publish(parent)?;
        Ok(())
    }
    pub fn launch_failed(&mut self, parent: &str) -> Result<()> {
        self.execute("UPDATE parks SET error='Observer launch failed; monitoring unavailable.' WHERE parent=? AND state='armed'", &[json!(parent)])?;
        self.publish(parent)?;
        Ok(())
    }
    pub fn expire_observation(&mut self, parent: &str) -> Result<()> {
        self.transaction(false, |s| {
            s.execute("UPDATE parks SET state='expired',error='Observer duration expired; completion remains unknown.' WHERE parent=? AND state='armed' AND NOT EXISTS(SELECT 1 FROM jobs WHERE jobs.park=parks.id AND jobs.kind='execution')", &[json!(parent)])?;
            s.execute("UPDATE jobs SET status='expired',interruption_pending=0 WHERE status='armed' AND kind<>'execution' AND park IN (SELECT id FROM parks WHERE parent=? AND state IN ('armed','expired'))", &[json!(parent)])?;
            s.execute("UPDATE callbacks SET state=CASE WHEN state='ready' THEN 'expired' ELSE 'uncertain' END,error='Observer duration expired; delivery needs positive reconciliation.' WHERE parent=? AND state IN ('ready','pending','starting','in_flight') AND NOT EXISTS(SELECT 1 FROM jobs WHERE jobs.callback=callbacks.id AND jobs.kind='execution')", &[json!(parent)])
        })?;
        self.publish(parent)?;
        Ok(())
    }
}

impl Drop for Store {
    fn drop(&mut self) {
        if let Some(db) = self.db.take() {
            let _ = self.runtime.block_on(db.close());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const PARENT: &str = "a0b06b89-1581-48f7-b0ca-0fcc33aca71c";
    fn job(id: &str) -> ResolvedJob {
        ResolvedJob {
            id: id.into(),
            kind: "worker".into(),
            thread: format!("thread-{id}"),
            turn: Some(format!("turn-{id}")),
            item: None,
            process: None,
            execution: None,
            status: "armed".into(),
            exit_code: None,
        }
    }
    fn register(store: &mut Store, jobs: &[ResolvedJob]) -> Value {
        store
            .register_resolved(
                PARENT,
                "origin",
                "reason",
                jobs,
                &HashMap::new(),
                Some("gen-1"),
                &HashSet::new(),
            )
            .unwrap()
    }
    fn complete(store: &mut Store, id: &str) {
        store
            .observe_job(&json!({"id":id}), Some("completed"), Some(0), None, false)
            .unwrap();
        store.batch(PARENT).unwrap();
    }
    fn callback(store: &mut Store) -> Value {
        store.status(PARENT).unwrap()["callbacks"][0].clone()
    }

    #[test]
    fn an_explicit_recovery_report_survives_positive_running_and_terminal_observation() {
        let home = tempfile::tempdir().unwrap();
        let mut store = Store::open(home.path()).unwrap();
        register(&mut store, &[job("report")]);
        store
            .execute("UPDATE jobs SET report=1 WHERE id='report'", &[])
            .unwrap();
        store
            .observe_job(&json!({"id":"report"}), None, None, None, true)
            .unwrap();
        complete(&mut store, "report");
        let ready = callback(&mut store);
        assert!(
            store
                .message(PARENT, &ready)
                .unwrap()
                .starts_with("REPORT ONLY")
        );
    }

    #[test]
    fn recovery_batches_isolate_reports_and_cancelled_jobs_and_freeze_submissions() {
        let home = tempfile::tempdir().unwrap();
        let mut store = Store::open(home.path()).unwrap();
        register(&mut store, &[job("cancelled")]);
        store.cancel(PARENT, None, None).unwrap();
        let record = crate::executions::prepare(home.path(), PARENT, &json!({"root":PARENT,"turn":"origin","goal":null}), &json!({"schema":1,"topic":"mixed recovery","argv":["/usr/bin/true"],"cwd":home.path()})).unwrap();
        let id = record["id"].as_str().unwrap();
        let mut record = crate::executions::cancel(home.path(), PARENT, id).unwrap();
        record["result"]["cleanup"]["status"] = json!("unknown");
        let path = home
            .path()
            .join("codex-plus/executions/v1")
            .join(PARENT)
            .join(id)
            .join("state.json");
        crate::local::atomic_json(&path, &record).unwrap();
        let mut recovery = job("recovery");
        recovery.kind = "execution".into();
        recovery.thread = PARENT.into();
        recovery.execution = Some(id.into());
        register(&mut store, &[recovery, job("report"), job("live")]);
        store.claim_observation(PARENT, "observer", false).unwrap();
        store.observe_job(&json!({"id":"recovery","observer_run":"observer","execution_recovery":true,"live_recovery":true}), Some("interrupted"), None, None, false).unwrap();
        store
            .execute("UPDATE jobs SET report=1 WHERE id='report'", &[])
            .unwrap();
        complete(&mut store, "report");
        complete(&mut store, "live");
        store.batch(PARENT).unwrap();
        let callbacks = store.status(PARENT).unwrap()["callbacks"]
            .as_array()
            .unwrap()
            .clone();
        assert_eq!(callbacks.len(), 3);
        let mut headers = Vec::new();
        let mut recovery_callback = None;
        for callback in callbacks {
            let claimed = store
                .claim_callback(&callback, false, false)
                .unwrap()
                .unwrap();
            let message = claimed["message"].as_str().unwrap();
            headers.push(message.split(':').next().unwrap().to_owned());
            if message.starts_with("RECOVERY REQUIRED") {
                recovery_callback = Some(claimed);
            }
        }
        headers.sort();
        assert_eq!(
            headers,
            ["LIVE COMPLETION", "RECOVERY REQUIRED", "REPORT ONLY"]
        );
        let frozen = recovery_callback.unwrap();
        store.claim_observation(PARENT, "restart", true).unwrap();
        store
            .observe_generation(PARENT, Some("new-generation"))
            .unwrap();
        let status = store.status(PARENT).unwrap();
        let callback = status["callbacks"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["id"] == frozen["id"])
            .unwrap();
        assert_eq!(callback["message"], frozen["message"]);
        assert_eq!(callback["client"], frozen["client"]);
        assert_eq!(
            status["jobs"]
                .as_array()
                .unwrap()
                .iter()
                .find(|row| row["id"] == "recovery")
                .unwrap()["recovery_allowed"],
            1
        );
        store.cancel(PARENT, None, None).unwrap();
        store.batch(PARENT).unwrap();
        assert!(
            store
                .next_callback(PARENT, false, false)
                .unwrap()
                .0
                .is_none()
        );
    }

    #[test]
    fn detached_watch_survives_expiry_without_extending_native_watch_or_repurposing_ids() {
        let home = tempfile::tempdir().unwrap();
        let mut store = Store::open(home.path()).unwrap();
        let record = crate::executions::prepare(
            home.path(),
            PARENT,
            &json!({"root":PARENT,"turn":"origin","goal":null}),
            &json!({"schema":1,"topic":"expiry","argv":["/usr/bin/true"],"cwd":home.path()}),
        )
        .unwrap();
        let mut execution = job("detached");
        execution.kind = "execution".into();
        execution.thread = PARENT.into();
        execution.turn = None;
        execution.execution = Some(record["id"].as_str().unwrap().into());
        let registration = register(&mut store, &[execution, job("native")]);
        let park = registration["park"].as_str().unwrap();
        store.expire_registration(park).unwrap();
        store.expire_observation(PARENT).unwrap();
        let status = store.status(PARENT).unwrap();
        assert_eq!(status["parks"][0]["state"], "armed");
        assert_eq!(status["jobs"][0]["status"], "armed");
        assert!(status["jobs"][0]["process"].is_null());
        assert!(status["jobs"][0]["item"].is_null());
        assert!(status["jobs"][0]["execution"].is_string());
        assert_eq!(status["jobs"][1]["status"], "expired");
        assert!(store.has_execution_watches(PARENT).unwrap());
        store
            .observe_job(&json!({"id":"detached"}), Some("lost"), None, None, false)
            .unwrap();
        store.batch(PARENT).unwrap();
        let ready = callback(&mut store);
        let message = store.message(PARENT, &ready).unwrap();
        assert!(message.starts_with("REPORT ONLY"));
        assert!(message.contains("outcome=UNKNOWN"));
        assert!(message.contains("stdout.log"));
        store.cancel(PARENT, None, None).unwrap();
        assert!(!store.has_execution_watches(PARENT).unwrap());
    }

    #[test]
    fn registration_deduplicates_and_never_rearms_cancelled_work() {
        let home = tempfile::tempdir().unwrap();
        let mut store = Store::open(home.path()).unwrap();
        let mut last = job("one");
        last.turn = Some("last turn".into());
        let first = register(&mut store, &[job("one"), last]);
        assert_eq!(first["armed"], json!(["one"]));
        let status = store.status(PARENT).unwrap();
        assert_eq!(status["jobs"][0]["turn"], "last turn");
        assert_eq!(status["jobs"][0]["item"], Value::Null);
        assert_eq!(status["jobs"][0]["recovery_pending"], json!(0));
        assert!(status["parks"][0]["deadline"].as_f64().unwrap() > crate::local::now());
        assert_eq!(
            register(&mut store, &[job("one")])["state"],
            "already_registered"
        );
        store.cancel(PARENT, None, None).unwrap();
        let err = store
            .register_resolved(
                PARENT,
                "origin",
                "reason",
                &[job("two"), job("one")],
                &HashMap::new(),
                None,
                &HashSet::new(),
            )
            .unwrap_err();
        assert!(err.downcast_ref::<RegistrationError>().is_some());
        assert_eq!(
            store.status(PARENT).unwrap()["jobs"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        let park = &store.status(PARENT).unwrap()["parks"][0];
        assert_eq!(park["cancel_kind"], "explicit");
        assert!(park["cancelled_at"].is_f64());
    }

    #[test]
    fn cancellation_preserves_provenance_and_claim_cannot_beat_cancellation() {
        let home = tempfile::tempdir().unwrap();
        let mut store = Store::open(home.path()).unwrap();
        register(&mut store, &[job("one")]);
        store.claim_observation(PARENT, "observer", false).unwrap();
        store.monitoring_failed(PARENT, "TimeoutError").unwrap();
        complete(&mut store, "one");
        let stale = callback(&mut store);
        let evidence = json!({"source":"rollout", "event":"turn_aborted", "status":"interrupted", "timestamp":"é".repeat(170), "secret":"excluded"});
        store
            .cancel(PARENT, Some("origin"), Some(&evidence))
            .unwrap();
        assert!(
            store
                .claim_callback(&stale, false, false)
                .unwrap()
                .is_none()
        );
        let status = store.status(PARENT).unwrap();
        let park = &status["parks"][0];
        assert_eq!(park["cancel_kind"], "confirmed_origin");
        let provenance: Value =
            serde_json::from_str(park["cancel_evidence"].as_str().unwrap()).unwrap();
        assert_eq!(provenance["observer"], "observer");
        assert_eq!(provenance["generation"], "gen-1");
        assert_eq!(
            provenance["timestamp"].as_str().unwrap().chars().count(),
            160
        );
        assert!(provenance.get("secret").is_none());
        assert_eq!(
            provenance["previous_error"],
            "Monitoring unavailable (TimeoutError); completion remains unknown."
        );
        assert_eq!(status["callbacks"][0]["state"], "cancelled");
    }

    #[test]
    fn batching_and_cancellation_serialize_both_orders_on_real_connections() {
        for cancel_first in [true, false] {
            let home = tempfile::tempdir().unwrap();
            let mut batcher = Store::open(home.path()).unwrap();
            let mut canceller = Store::open(home.path()).unwrap();
            register(&mut batcher, &[job("one")]);
            batcher
                .observe_job(
                    &json!({"id":"one"}),
                    Some("completed"),
                    Some(0),
                    None,
                    false,
                )
                .unwrap();
            let mut cancellation = None;
            let mut release = None;
            if cancel_first {
                canceller.cancel(PARENT, None, None).unwrap();
            } else {
                canceller.execute("PRAGMA busy_timeout=0", &[]).unwrap();
                let (selected_tx, selected_rx) = std::sync::mpsc::channel();
                let (attempted_tx, attempted_rx) = std::sync::mpsc::channel();
                let (release_tx, release_rx) = std::sync::mpsc::channel();
                cancellation = Some(std::thread::spawn(move || {
                    selected_rx.recv_timeout(Duration::from_secs(5)).unwrap();
                    let error = canceller.cancel(PARENT, None, None).unwrap_err();
                    assert!(error.to_string().contains("database is locked"), "{error}");
                    attempted_tx.send(()).unwrap();
                    release_rx.recv_timeout(Duration::from_secs(5)).unwrap();
                    canceller.cancel(PARENT, None, None).unwrap();
                }));
                release = Some(release_tx);
                BATCH_SELECTED.with(|hook| {
                    *hook.borrow_mut() = Some(Box::new(move || {
                        selected_tx.send(()).unwrap();
                        attempted_rx.recv_timeout(Duration::from_secs(5)).unwrap();
                    }))
                });
            }
            batcher.batch(PARENT).unwrap();
            let stale = batcher.next_callback(PARENT, false, false).unwrap().0;
            if let Some(release) = release {
                release.send(()).unwrap();
                cancellation.unwrap().join().unwrap();
            }
            if let Some(stale) = stale {
                assert!(
                    batcher
                        .claim_callback(&stale, false, false)
                        .unwrap()
                        .is_none()
                );
            }
            assert!(
                batcher
                    .next_callback(PARENT, false, false)
                    .unwrap()
                    .0
                    .is_none()
            );
            assert_eq!(
                batcher.status(PARENT).unwrap()["parks"][0]["cancel_kind"],
                "explicit"
            );
        }
    }

    #[test]
    fn legacy_invalidated_unsubmitted_callbacks_retire_without_changing_provenance() {
        for cancelled in [true, false] {
            let home = tempfile::tempdir().unwrap();
            let mut store = Store::open(home.path()).unwrap();
            let old = register(&mut store, &[job("old")]);
            complete(&mut store, "old");
            let callback = callback(&mut store);
            if cancelled {
                store.cancel(PARENT, None, None).unwrap();
            } else {
                store
                    .expire_registration(old["park"].as_str().unwrap())
                    .unwrap();
            }
            let provenance = store.status(PARENT).unwrap()["parks"][0].clone();
            // Old batching could assign a ready callback after cancellation had committed.
            store
                .execute(
                    "UPDATE callbacks SET state='ready' WHERE id=?",
                    &[callback["id"].clone()],
                )
                .unwrap();
            register(&mut store, &[job("fresh")]);
            complete(&mut store, "fresh");
            let next = store
                .next_callback(PARENT, false, false)
                .unwrap()
                .0
                .unwrap();
            assert_ne!(next["id"], callback["id"]);
            assert!(store.claim_callback(&next, false, false).unwrap().is_some());
            let state = store.status(PARENT).unwrap();
            let retired = state["callbacks"]
                .as_array()
                .unwrap()
                .iter()
                .find(|c| c["id"] == callback["id"])
                .unwrap();
            assert_eq!(
                retired["state"],
                if cancelled { "cancelled" } else { "expired" }
            );
            assert_eq!(state["parks"][0], provenance);
        }
    }

    #[test]
    fn claim_race_freezes_identity_payload_and_recovery_class() {
        let home = tempfile::tempdir().unwrap();
        let mut first = Store::open(home.path()).unwrap();
        let mut labels = HashMap::new();
        labels.insert("thread-one".into(), Some("  α\n beta\u{1c}gamma  ".into()));
        first
            .register_resolved(
                PARENT,
                "origin",
                "reason",
                &[job("one"), job("two")],
                &labels,
                Some("gen-1"),
                &HashSet::new(),
            )
            .unwrap();
        first.claim_observation(PARENT, "initial", false).unwrap();
        complete(&mut first, "one");
        let stale = callback(&mut first);
        let mut second = Store::open(home.path()).unwrap();
        let frozen = first.claim_callback(&stale, false, false).unwrap().unwrap();
        assert!(
            second
                .claim_callback(&stale, false, false)
                .unwrap()
                .is_none()
        );
        let expected = format!(
            "LIVE COMPLETION: worker α beta gamma status=completed exit=0. Remaining registered jobs: 1. Results: codex-plus park --status --parent {PARENT}; callback={}.",
            text(&frozen["id"])
        );
        assert_eq!(frozen["message"], expected);
        first.claim_observation(PARENT, "restarted", true).unwrap();
        first.observe_generation(PARENT, Some("gen-2")).unwrap();
        let recovered = callback(&mut second);
        assert_eq!(recovered["id"], frozen["id"]);
        assert_eq!(recovered["client"], frozen["client"]);
        assert_eq!(recovered["message"], frozen["message"]);
        assert_eq!(recovered["state"], "uncertain");
        assert_eq!(second.status(PARENT).unwrap()["jobs"][0]["report"], 0);
    }

    #[test]
    fn observer_and_daemon_recovery_only_classify_unfrozen_work() {
        let home = tempfile::tempdir().unwrap();
        let mut store = Store::open(home.path()).unwrap();
        register(
            &mut store,
            &[job("running"), job("finished"), job("missing")],
        );
        store
            .claim_observation(PARENT, "old-observer", false)
            .unwrap();
        store
            .observe_job(
                &json!({"id":"finished"}),
                Some("completed"),
                None,
                None,
                false,
            )
            .unwrap();
        store
            .claim_observation(PARENT, "new-observer", true)
            .unwrap();
        store
            .observe_job(&json!({"id":"running"}), None, None, None, true)
            .unwrap();
        store
            .observe_job(
                &json!({"id":"running"}),
                Some("completed"),
                None,
                None,
                false,
            )
            .unwrap();
        assert!(store.observe_generation(PARENT, Some("gen-2")).unwrap());
        store.worker_pending("missing", true).unwrap();
        store.finish_recovery(PARENT).unwrap();
        assert_eq!(
            store.active_parks(PARENT).unwrap()[0]["recovery_generation"],
            "gen-2"
        );
        store
            .observe_job(&json!({"id":"missing"}), None, Some(1), None, false)
            .unwrap();
        let status = store.status(PARENT).unwrap();
        assert_eq!(status["jobs"][0]["report"], 1); // Unfrozen terminal results become reports on daemon replacement.
        assert_eq!(status["jobs"][1]["report"], 1);
        assert_eq!(status["jobs"][2]["status"], "lost");
        assert_eq!(status["jobs"][2]["exit_code"], Value::Null);
        store.finish_recovery(PARENT).unwrap();
        assert_eq!(
            store.active_parks(PARENT).unwrap()[0]["generation"],
            "gen-2"
        );
    }

    #[test]
    fn ambiguous_enqueue_never_retries_without_positive_receipt_evidence() {
        let home = tempfile::tempdir().unwrap();
        let mut store = Store::open(home.path()).unwrap();
        register(&mut store, &[job("one")]);
        complete(&mut store, "one");
        let ready = callback(&mut store);
        let frozen = store.claim_callback(&ready, false, false).unwrap().unwrap();
        store.dispatch_result(&frozen, false, None, false).unwrap();
        let uncertain = callback(&mut store);
        store
            .reconcile_evidence(&uncertain, &ReconcileEvidence::default())
            .unwrap();
        assert!(
            store
                .next_callback(PARENT, false, false)
                .unwrap()
                .0
                .is_none()
        );
        assert!(!store.finish_cycle(PARENT).unwrap());
        assert_eq!(store.health(PARENT, None).unwrap()["state"], "uncertain");
        store
            .reconcile_evidence(
                &uncertain,
                &ReconcileEvidence {
                    receipt_absent: true,
                    ..Default::default()
                },
            )
            .unwrap();
        assert_eq!(callback(&mut store)["state"], "uncertain"); // Stock absence is never resend permission.
        store
            .reconcile_evidence(
                &uncertain,
                &ReconcileEvidence {
                    consumed: true,
                    ..Default::default()
                },
            )
            .unwrap();
        assert_eq!(callback(&mut store)["state"], "delivered");
        assert!(!store.finish_cycle(PARENT).unwrap());
        assert_eq!(store.health(PARENT, None).unwrap()["state"], "idle");
    }

    #[test]
    fn keyed_reconciliation_preserves_text_and_waits_on_stock_daemon() {
        let home = tempfile::tempdir().unwrap();
        let mut store = Store::open(home.path()).unwrap();
        register(&mut store, &[job("one")]);
        complete(&mut store, "one");
        let ready = callback(&mut store);
        let frozen = store.claim_callback(&ready, false, true).unwrap().unwrap();
        store.dispatch_result(&frozen, false, None, false).unwrap();
        store
            .reconcile_evidence(
                &frozen,
                &ReconcileEvidence {
                    native_unavailable: true,
                    ..Default::default()
                },
            )
            .unwrap();
        assert_eq!(callback(&mut store)["state"], "uncertain");
        store
            .reconcile_evidence(
                &frozen,
                &ReconcileEvidence {
                    receipt_absent: true,
                    ..Default::default()
                },
            )
            .unwrap();
        assert_eq!(
            store.next_callback(PARENT, false, false).unwrap(),
            (None, true)
        );
        let reclaimed = store.claim_callback(&frozen, false, true).unwrap().unwrap();
        assert_eq!(reclaimed["message"], frozen["message"]);
        store
            .dispatch_result(&reclaimed, false, Some("queue-1"), false)
            .unwrap();
        let pending = callback(&mut store);
        store.cancel(PARENT, None, None).unwrap();
        store
            .record_receipt(&pending, "pending", Some("queue-1"))
            .unwrap();
        assert_eq!(callback(&mut store)["state"], "cancel_requested");
        let cancelling = callback(&mut store);
        store
            .reconcile_evidence(
                &cancelling,
                &ReconcileEvidence {
                    queued: Some(json!({"id":"queue-1"})),
                    deleted: Some(false),
                    ..Default::default()
                },
            )
            .unwrap();
        assert_eq!(callback(&mut store)["state"], "cancel_uncertain");
        store
            .reconcile_evidence(
                &cancelling,
                &ReconcileEvidence {
                    queued: Some(json!({"id":"queue-1"})),
                    deleted: Some(true),
                    ..Default::default()
                },
            )
            .unwrap();
        assert_eq!(callback(&mut store)["state"], "cancelled");
    }

    #[test]
    fn keyed_start_ack_requires_receipt_and_stale_start_claim_rechecks_cancel() {
        let home = tempfile::tempdir().unwrap();
        let mut store = Store::open(home.path()).unwrap();
        register(&mut store, &[job("one")]);
        complete(&mut store, "one");
        let ready = callback(&mut store);
        let frozen = store.claim_callback(&ready, false, true).unwrap().unwrap();
        store
            .dispatch_result(&frozen, false, Some("queue-1"), false)
            .unwrap();
        let pending = callback(&mut store);
        assert!(
            store
                .claim_callback(&pending, true, false)
                .unwrap()
                .is_none()
        );
        let starting = store.claim_callback(&pending, true, true).unwrap().unwrap();
        store.dispatch_result(&starting, true, None, true).unwrap();
        assert_eq!(callback(&mut store)["state"], "starting");
        store.record_receipt(&starting, "pending", None).unwrap();
        let stale = callback(&mut store);
        store.cancel(PARENT, None, None).unwrap();
        assert!(store.claim_callback(&stale, true, true).unwrap().is_none());
        store.record_receipt(&stale, "consumed", None).unwrap();
        assert_eq!(callback(&mut store)["state"], "delivered");
    }

    #[test]
    fn report_message_preserves_unicode_bounds_unknown_outcomes_and_batch_limit() {
        let home = tempfile::tempdir().unwrap();
        let mut store = Store::open(home.path()).unwrap();
        let jobs = (0..4).map(|i| job(&i.to_string())).collect::<Vec<_>>();
        let mut labels = HashMap::new();
        labels.insert(
            "thread-0".into(),
            Some(format!(" \u{85}{}\n ", "界".repeat(110))),
        );
        store
            .register_resolved(
                PARENT,
                "origin",
                "reason",
                &jobs,
                &labels,
                Some("gen-1"),
                &HashSet::new(),
            )
            .unwrap();
        store
            .claim_observation(PARENT, "first-observer", false)
            .unwrap();
        store
            .claim_observation(PARENT, "second-observer", true)
            .unwrap();
        store.observe_generation(PARENT, Some("gen-2")).unwrap();
        for job in jobs {
            store
                .observe_job(&json!({"id":job.id}), None, None, None, false)
                .unwrap();
        }
        store.batch(PARENT).unwrap();
        let ready = callback(&mut store);
        let frozen = store.claim_callback(&ready, false, false).unwrap().unwrap();
        let expected = format!(
            "REPORT ONLY: worker {} status=lost outcome=UNKNOWN; worker thread-1 status=lost outcome=UNKNOWN; worker thread-2 status=lost outcome=UNKNOWN; +1 more. Remaining registered jobs: 0. Do not resume earlier actions from this report. Do not rerun lost jobs. Results: codex-plus park --status --parent {PARENT}; callback={}.",
            "界".repeat(96),
            text(&frozen["id"])
        );
        assert_eq!(frozen["message"], expected);
        assert_eq!(store.health(PARENT, None).unwrap()["reports"], 4);
    }

    #[test]
    fn concurrent_connections_only_one_callback_claim_wins() {
        let home = tempfile::tempdir().unwrap();
        let mut store = Store::open(home.path()).unwrap();
        register(&mut store, &[job("one")]);
        complete(&mut store, "one");
        let stale = callback(&mut store);
        let barrier = std::sync::Arc::new(std::sync::Barrier::new(2));
        let claims = (0..2)
            .map(|_| {
                let home = home.path().to_owned();
                let callback = stale.clone();
                let barrier = barrier.clone();
                std::thread::spawn(move || {
                    let mut store = Store::open(&home).unwrap();
                    barrier.wait();
                    store
                        .claim_callback(&callback, false, false)
                        .unwrap()
                        .is_some()
                })
            })
            .collect::<Vec<_>>();
        assert_eq!(
            claims
                .into_iter()
                .map(|t| t.join().unwrap())
                .filter(|won| *won)
                .count(),
            1
        );
        assert!(callback(&mut store)["message"].is_string());
    }

    #[test]
    fn origin_uncertainty_gates_claim_resume_and_preserves_monitoring_warning() {
        let home = tempfile::tempdir().unwrap();
        let mut store = Store::open(home.path()).unwrap();
        register(&mut store, &[job("one")]);
        complete(&mut store, "one");
        let ready = callback(&mut store);
        store.origin_pending(PARENT, "origin", true).unwrap();
        assert!(
            store
                .next_callback(PARENT, false, false)
                .unwrap()
                .0
                .is_none()
        );
        assert!(
            store
                .claim_callback(&ready, false, false)
                .unwrap()
                .is_none()
        );
        assert!(!store.can_resume(PARENT, &[], false).unwrap());
        store.monitoring_failed(PARENT, "RpcError").unwrap();
        store.monitoring_succeeded(PARENT).unwrap();
        let status = store.status(PARENT).unwrap();
        assert_eq!(status["health"]["state"], "error");
        assert!(
            status["parks"][0]["monitoring_error"]
                .as_str()
                .unwrap()
                .contains("RpcError")
        );
        store.origin_pending(PARENT, "origin", false).unwrap();
        assert!(store.can_resume(PARENT, &[], false).unwrap());
        assert!(
            !store
                .can_resume(PARENT, &[json!({"clientUserMessageId":"foreign"})], true)
                .unwrap()
        );
        store.monitoring_succeeded(PARENT).unwrap();
        store.upgrade_warning(PARENT, true).unwrap();
        store.monitoring_succeeded(PARENT).unwrap();
        assert_eq!(
            store.status(PARENT).unwrap()["parks"][0]["error"],
            UPGRADE_WARNING
        );
    }

    #[test]
    fn additive_legacy_migration_preserves_rows_and_sqlite_storage_classes() {
        let home = tempfile::tempdir().unwrap();
        let directory = crate::local::state_directory(home.path()).unwrap();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let options = sqlx::sqlite::SqliteConnectOptions::new()
            .filename(directory.join("background.sqlite"))
            .create_if_missing(true);
        let mut db = runtime
            .block_on(SqliteConnection::connect_with(&options))
            .unwrap();
        for sql in [
            "CREATE TABLE parks (id TEXT PRIMARY KEY,parent TEXT NOT NULL,origin TEXT NOT NULL,reason TEXT NOT NULL,state TEXT NOT NULL,deadline REAL NOT NULL)",
            "CREATE TABLE jobs (id TEXT PRIMARY KEY,park TEXT NOT NULL,kind TEXT NOT NULL,thread TEXT NOT NULL,turn TEXT,item TEXT,process TEXT,status TEXT NOT NULL,exit_code INTEGER,callback TEXT)",
            "CREATE TABLE callbacks (id TEXT PRIMARY KEY,parent TEXT NOT NULL,client TEXT UNIQUE NOT NULL,state TEXT NOT NULL,queue TEXT,error TEXT)",
            "INSERT INTO jobs VALUES ('old-job','old-park','process','thread',NULL,'item','123','completed',0,'old-callback')",
        ] {
            runtime.block_on(sqlx::query(sql).execute(&mut db)).unwrap();
        }
        runtime.block_on(sqlx::query("INSERT INTO parks VALUES ('old-park',?,'old-origin','old reason','armed',12345.25)").bind(PARENT).execute(&mut db)).unwrap();
        runtime.block_on(sqlx::query("INSERT INTO callbacks VALUES ('old-callback',?,'old-client','uncertain',NULL,'old uncertainty')").bind(PARENT).execute(&mut db)).unwrap();
        runtime.block_on(db.close()).unwrap();
        drop(runtime);
        let mut store = Store::open(home.path()).unwrap();
        let status = store.status(PARENT).unwrap();
        assert_eq!(status["parks"][0]["deadline"], 12345.25);
        assert_eq!(status["jobs"][0]["exit_code"], json!(0));
        assert_eq!(status["jobs"][0]["turn"], Value::Null);
        assert_eq!(status["jobs"][0]["recovery_pending"], json!(1));
        assert_eq!(status["jobs"][0]["recovery_allowed"], json!(0));
        assert_eq!(status["callbacks"][0]["idempotent"], json!(0));
        assert_eq!(status["callbacks"][0]["message"], Value::Null);
        assert_eq!(
            store.rows("PRAGMA synchronous", &[]).unwrap()[0]["synchronous"],
            2
        );
        assert_eq!(
            store.rows("PRAGMA busy_timeout", &[]).unwrap()[0]["timeout"],
            10000
        );
        assert_eq!(
            store.rows("PRAGMA journal_mode", &[]).unwrap()[0]["journal_mode"],
            "delete"
        );
        drop(store);
        let mut reopened = Store::open(home.path()).unwrap();
        assert_eq!(reopened.status(PARENT).unwrap(), status);
        assert_eq!(
            fs::metadata(directory.join("background.sqlite"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }
}
