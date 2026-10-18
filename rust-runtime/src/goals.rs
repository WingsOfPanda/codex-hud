//! Durable Plus goal policy; network submissions are reserved before queue/add.
use crate::ledger::Store;
use anyhow::{Result, bail, ensure};
use serde_json::{Value, json};
use std::collections::HashSet;
use uuid::Uuid;

const OPEN: &str = "state IN ('ready','submitting','pending','starting','accepted','uncertain')";

fn required<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    let text = value[key].as_str().unwrap_or("");
    ensure!(!text.trim().is_empty(), "{key} must be nonempty text");
    Ok(text)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::ResolvedJob;
    use std::collections::{HashMap, HashSet};

    fn fixture() -> (tempfile::TempDir, Store, String) {
        let home = tempfile::tempdir().unwrap();
        let store = Store::open(home.path()).unwrap();
        (home, store, Uuid::new_v4().to_string())
    }
    #[test]
    fn recovered_execution_keeps_its_original_goal_and_ordinary_execution_stays_unbound() {
        let (home, mut store, root) = fixture();
        let snapshot = store
            .goal_start(&root, "original", None, false, 1.0)
            .unwrap();
        let id = snapshot["goal"]["id"].as_str().unwrap();
        let ordinary = crate::executions::prepare(
            home.path(),
            &root,
            &json!({"root":root,"turn":"origin","goal":null}),
            &json!({"schema":1,"topic":"ordinary","argv":["/usr/bin/true"],"cwd":home.path()}),
        )
        .unwrap();
        store.goal_admit(&root, id, 1, "origin", 1, 2.0).unwrap();
        let bound = crate::executions::prepare(
            home.path(),
            &root,
            &json!({"root":root,"turn":"origin","input_epoch":1,"goal":{"id":id,"revision":1}}),
            &json!({"schema":1,"topic":"bound","argv":["/usr/bin/true"],"cwd":home.path()}),
        )
        .unwrap();
        store
            .goal_replace(&root, id, 1, "replacement", None, false, 3.0)
            .unwrap();
        let jobs: Vec<_> = [ordinary, bound]
            .iter()
            .enumerate()
            .map(|(index, record)| ResolvedJob {
                id: format!("job-{index}"),
                kind: "execution".into(),
                thread: root.clone(),
                turn: None,
                item: None,
                process: None,
                execution: Some(record["id"].as_str().unwrap().into()),
                status: "armed".into(),
                exit_code: None,
            })
            .collect();
        store
            .register_resolved(
                &root,
                "origin",
                "recover without rebinding",
                &jobs,
                &HashMap::new(),
                None,
                &HashSet::new(),
            )
            .unwrap();
        let rows = store.rows("SELECT * FROM goal_jobs", &[]).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["job"], "job-1");
        assert_eq!(rows[0]["goal"], id);
        assert_ne!(store.goal_status(&root).unwrap()["goal"]["id"], id);
    }
    #[test]
    fn settlement_time_tracks_acknowledgement_and_start_not_intent_creation() {
        let (home, mut store, root) = fixture();
        let snapshot = store
            .goal_start(&root, "settle exactly once", None, false, 1.0)
            .unwrap();
        let dispatch = store
            .goal_intent(&root, &snapshot, "continue", "frozen payload", 2.0)
            .unwrap()
            .unwrap();
        let id = dispatch["id"].as_str().unwrap();
        store.goal_claim_dispatch(id).unwrap().unwrap();
        assert_eq!(
            store
                .goal_dispatch_result(id, "pending", Some("queue"), None, None, 10.0)
                .unwrap()["settlement_started"],
            10.0
        );
        assert_eq!(
            store
                .goal_dispatch_result(id, "pending", Some("queue"), None, None, 20.0)
                .unwrap()["settlement_started"],
            10.0
        );
        assert_eq!(
            store
                .goal_dispatch_result(id, "starting", Some("queue"), None, None, 30.0)
                .unwrap()["settlement_started"],
            30.0
        );
        drop(store);
        let mut store = Store::open(home.path()).unwrap();
        assert_eq!(
            store.goal_dispatches(&root).unwrap()[0]["settlement_started"],
            30.0
        );
    }
    fn start(s: &mut Store, root: &str) -> Value {
        s.goal_start(root, "Verify the requested result", None, false, 0.0)
            .unwrap()
    }
    fn admit(s: &mut Store, root: &str, turn: &str) -> Value {
        let v = s.goal_snapshot(root).unwrap();
        s.goal_admit(
            root,
            v["goal"]["id"].as_str().unwrap(),
            v["goal"]["revision"].as_i64().unwrap(),
            turn,
            v["input_epoch"].as_i64().unwrap(),
            1.0,
        )
        .unwrap()
    }
    fn stage(s: &mut Store, root: &str, turn: &str, d: Value) {
        let v = s.goal_snapshot(root).unwrap();
        s.goal_checkpoint(
            root,
            v["goal"]["id"].as_str().unwrap(),
            v["goal"]["revision"].as_i64().unwrap(),
            turn,
            v["input_epoch"].as_i64().unwrap(),
            &d,
            2.0,
        )
        .unwrap();
    }
    fn mutate(s: &mut Store, root: &str, action: &str) -> Value {
        let v = s.goal_snapshot(root).unwrap();
        s.goal_mutate(
            root,
            v["goal"]["id"].as_str().unwrap(),
            v["goal"]["revision"].as_i64().unwrap(),
            action,
            &json!({}),
            3.0,
        )
        .unwrap()
    }
    fn input(s: &mut Store, root: &str, renewed: bool, now: f64) -> Value {
        let v = s.goal_input(root, renewed, now).unwrap();
        s.goal_input_settle(
            root,
            v["submission_id"].as_str().unwrap(),
            Some("ordinary-user-turn"),
            "accepted",
            now + 0.01,
        )
        .unwrap()
    }
    fn job(s: &mut Store, root: &str, turn: &str, name: &str) {
        s.register_resolved(
            root,
            turn,
            "wait",
            &[ResolvedJob {
                id: name.into(),
                kind: "command".into(),
                thread: root.into(),
                turn: Some(turn.into()),
                item: Some(name.into()),
                process: Some(name.into()),
                execution: None,
                status: "armed".into(),
                exit_code: None,
            }],
            &HashMap::new(),
            Some("generation"),
            &HashSet::new(),
        )
        .unwrap();
    }

    #[test]
    fn staged_checkpoint_requires_successful_exact_terminal_turn() {
        let (_home, mut s, root) = fixture();
        start(&mut s, &root);
        admit(&mut s, &root, "turn");
        stage(
            &mut s,
            &root,
            "turn",
            json!({"disposition":"continue","next_action":"run check","evidence":["result:1"]}),
        );
        assert_eq!(
            s.goal_snapshot(&root).unwrap()["goal"]["checkpoint"],
            Value::Null
        );
        let unrelated = s.goal_finalize(&root, "unrelated", true, 3.0).unwrap();
        assert_eq!(unrelated["goal"]["revision"], 1);
        let failed = s.goal_finalize(&root, "turn", false, 4.0).unwrap();
        assert_eq!(failed["goal"]["state"], "needs_input");
        assert!(
            s.test_goal_intent(&root, "continue", "continue", 5.0)
                .unwrap()
                .is_none()
        );
        assert_eq!(
            s.goal_finalize(&root, "turn", true, 6.0).unwrap()["goal"]["revision"],
            2
        );
    }

    #[test]
    fn missing_checkpoint_and_stale_input_cannot_continue() {
        let (_home, mut s, root) = fixture();
        start(&mut s, &root);
        let v = admit(&mut s, &root, "turn");
        input(&mut s, &root, false, 2.0);
        assert!(
            s.goal_checkpoint(
                &root,
                v["goal"]["id"].as_str().unwrap(),
                1,
                "turn",
                1,
                &json!({"disposition":"continue","next_action":"work"}),
                3.0
            )
            .is_err()
        );
        s.goal_finalize(&root, "turn", true, 4.0).unwrap();
        assert_eq!(s.goal_snapshot(&root).unwrap()["goal"]["no_progress"], 0);
        admit(&mut s, &root, "next");
        assert_eq!(
            s.goal_finalize(&root, "next", true, 5.0).unwrap()["goal"]["state"],
            "needs_input"
        );
    }

    #[test]
    fn repeated_evidence_counts_once_per_eligible_turn() {
        let (_home, mut s, root) = fixture();
        start(&mut s, &root);
        for n in 0..3 {
            let turn = format!("turn-{n}");
            admit(&mut s, &root, &turn);
            stage(
                &mut s,
                &root,
                &turn,
                json!({"disposition":"continue","next_action":format!("different prose {n}"),"evidence":["result:same"]}),
            );
            let v = s.goal_finalize(&root, &turn, true, 3.0 + n as f64).unwrap();
            assert_eq!(v["goal"]["no_progress"], n);
            assert_eq!(
                v["goal"]["state"],
                if n == 2 { "needs_input" } else { "active" }
            );
            assert_eq!(
                s.goal_finalize(&root, &turn, true, 9.0).unwrap()["goal"]["no_progress"],
                n
            );
        }
    }

    #[test]
    fn healthy_wait_cycles_do_not_count_as_no_progress_or_dispatch_legacy_callbacks() {
        let (_home, mut s, root) = fixture();
        start(&mut s, &root);
        for n in 0..3 {
            let turn = format!("turn-{n}");
            let id = format!("job-{n}");
            admit(&mut s, &root, &turn);
            job(&mut s, &root, &turn, &id);
            stage(
                &mut s,
                &root,
                &turn,
                json!({"disposition":"wait","jobs":[id]}),
            );
            let v = s.goal_finalize(&root, &turn, true, 3.0).unwrap();
            assert_eq!(v["goal"]["state"], "waiting");
            assert_eq!(v["goal"]["no_progress"], 0);
            assert!(
                s.test_goal_intent(&root, "review", "review", 10000.0)
                    .unwrap()
                    .is_none()
            );
            s.execute(
                "UPDATE jobs SET status='completed' WHERE id=?",
                &[json!(id)],
            )
            .unwrap();
            s.batch(&root).unwrap();
            assert!(s.next_callback(&root, false, false).unwrap().0.is_none());
            assert!(s.goal_wait_ready(&root, 4.0).unwrap());
            assert!(!s.finish_cycle(&root).unwrap());
            assert!(
                s.rows("SELECT state FROM callbacks WHERE parent=?", &[json!(root)])
                    .unwrap()
                    .iter()
                    .all(|c| c["state"] == "goal_settled")
            );
        }
        assert_eq!(
            s.goal_snapshot(&root).unwrap()["jobs"]
                .as_array()
                .unwrap()
                .len(),
            3
        );
    }

    #[test]
    fn completion_between_checkpoint_and_terminal_does_not_stall_wait() {
        let (_home, mut s, root) = fixture();
        start(&mut s, &root);
        admit(&mut s, &root, "turn");
        job(&mut s, &root, "turn", "job");
        stage(
            &mut s,
            &root,
            "turn",
            json!({"disposition":"wait","jobs":["job"]}),
        );
        s.execute("UPDATE jobs SET status='completed' WHERE id='job'", &[])
            .unwrap();
        assert_eq!(
            s.goal_finalize(&root, "turn", true, 3.0).unwrap()["goal"]["state"],
            "active"
        );
    }

    #[test]
    fn pause_and_edit_survive_late_dispatch_evidence() {
        let (_home, mut s, root) = fixture();
        start(&mut s, &root);
        let d = s
            .test_goal_intent(&root, "continue", "frozen", 1.0)
            .unwrap()
            .unwrap();
        let id = d["id"].as_str().unwrap();
        s.goal_claim_dispatch(id).unwrap().unwrap();
        mutate(&mut s, &root, "pause");
        s.goal_dispatch_result(
            id,
            "uncertain",
            None,
            None,
            Some("lost acknowledgement"),
            4.0,
        )
        .unwrap();
        assert_eq!(s.goal_snapshot(&root).unwrap()["goal"]["state"], "paused");
        s.goal_dispatch_result(id, "accepted", Some("queue"), Some("turn"), None, 5.0)
            .unwrap();
        assert_eq!(s.goal_snapshot(&root).unwrap()["goal"]["state"], "paused");
        assert!(
            s.goal_dispatch_result(id, "accepted", Some("changed"), Some("turn"), None, 6.0)
                .is_err()
        );
    }

    #[test]
    fn uncertain_old_identity_blocks_replacement_and_review_across_restart() {
        let (home, mut s, root) = fixture();
        let old = start(&mut s, &root);
        let d = s
            .test_goal_intent(&root, "continue", "frozen payload", 1.0)
            .unwrap()
            .unwrap();
        s.goal_claim_dispatch(d["id"].as_str().unwrap()).unwrap();
        s.goal_dispatch_result(
            d["id"].as_str().unwrap(),
            "uncertain",
            None,
            None,
            Some("unknown"),
            2.0,
        )
        .unwrap();
        s.goal_replace(
            &root,
            old["goal"]["id"].as_str().unwrap(),
            1,
            "replacement",
            Some(3600),
            false,
            3.0,
        )
        .unwrap();
        drop(s);
        let mut s = Store::open(home.path()).unwrap();
        assert!(
            s.test_goal_intent(&root, "review", "new", 90000.0)
                .unwrap()
                .is_none()
        );
        let stored = s.goal_dispatches(&root).unwrap();
        assert_eq!(stored[0]["message"], "frozen payload");
        assert_eq!(stored[0]["client"], d["client"]);
        s.goal_dispatch_result(
            d["id"].as_str().unwrap(),
            "accepted",
            Some("queue"),
            Some("oldturn"),
            None,
            5.0,
        )
        .unwrap();
        s.goal_finalize(&root, "oldturn", true, 6.0).unwrap();
        assert!(
            s.test_goal_intent(&root, "review", "new", 90000.0)
                .unwrap()
                .is_some()
        );
    }

    #[test]
    fn unsubmitted_intent_is_invalidated_and_cas_rejects_stale_mutation() {
        let (_home, mut s, root) = fixture();
        let v = start(&mut s, &root);
        let d = s
            .test_goal_intent(&root, "continue", "future", 1.0)
            .unwrap()
            .unwrap();
        mutate(&mut s, &root, "pause");
        assert!(
            s.goal_claim_dispatch(d["id"].as_str().unwrap())
                .unwrap()
                .is_none()
        );
        assert!(
            s.goal_mutate(
                &root,
                v["goal"]["id"].as_str().unwrap(),
                1,
                "resume",
                &json!({}),
                4.0
            )
            .is_err()
        );
        assert!(
            s.goal_start(&root, "unsafe replacement", None, false, 4.0)
                .is_err()
        );
    }

    #[test]
    fn report_barrier_survives_restarts_report_delivery_and_model_continue() {
        let (home, mut s, root) = fixture();
        start(&mut s, &root);
        admit(&mut s, &root, "oldturn");
        stage(
            &mut s,
            &root,
            "oldturn",
            json!({"disposition":"continue","next_action":"old work","evidence":["result:1"]}),
        );
        s.goal_recovery(&root, "report-1", "unwatched gap", 3.0)
            .unwrap();
        s.goal_report_delivered(&root, "report-1").unwrap();
        drop(s);
        let mut s = Store::open(home.path()).unwrap();
        let v = s
            .goal_recovery(&root, "report-2", "another restart", 4.0)
            .unwrap();
        assert_eq!(v["report_id"], "report-1");
        assert_eq!(v["report_delivered"], 1);
        s.goal_finalize(&root, "oldturn", true, 5.0).unwrap();
        input(&mut s, &root, false, 6.0);
        assert_eq!(s.goal_snapshot(&root).unwrap()["report_barrier"], 1);
        assert!(
            s.test_goal_intent(&root, "continue", "work", 7.0)
                .unwrap()
                .is_none()
        );
        assert_eq!(input(&mut s, &root, true, 8.0)["report_barrier"], 0);
        assert!(
            s.test_goal_intent(&root, "continue", "work", 9.0)
                .unwrap()
                .is_some()
        );
    }

    #[test]
    fn review_coalesces_missed_intervals_and_obeys_clock_and_state() {
        let (_home, mut s, root) = fixture();
        s.goal_start(&root, "review", Some(3600), false, 100.0)
            .unwrap();
        assert!(
            s.test_goal_intent(&root, "review", "review", 3699.0)
                .unwrap()
                .is_none()
        );
        let d = s
            .test_goal_intent(&root, "review", "review", 100000.0)
            .unwrap()
            .unwrap();
        assert!(
            s.test_goal_intent(&root, "review", "review", 100000.0)
                .unwrap()
                .is_none()
        );
        s.goal_claim_dispatch(d["id"].as_str().unwrap()).unwrap();
        s.goal_dispatch_result(
            d["id"].as_str().unwrap(),
            "done",
            None,
            None,
            None,
            100001.0,
        )
        .unwrap();
        assert!(
            s.test_goal_intent(&root, "review", "review", 100001.0)
                .unwrap()
                .is_none()
        );
        assert!(
            s.test_goal_intent(&root, "review", "review", 200.0)
                .unwrap()
                .is_none()
        );
        mutate(&mut s, &root, "pause");
        assert!(
            s.test_goal_intent(&root, "review", "review", 200000.0)
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn completed_indicator_dismissal_preserves_history_and_projection_is_private() {
        use std::os::unix::fs::PermissionsExt;
        let (home, mut s, root) = fixture();
        let v = start(&mut s, &root);
        s.goal_mutate(
            &root,
            v["goal"]["id"].as_str().unwrap(),
            1,
            "complete",
            &json!({"evidence":["check:passed"]}),
            1.0,
        )
        .unwrap();
        let v = input(&mut s, &root, false, 2.0);
        assert!(v["goal"].is_null());
        assert_eq!(
            s.goal_snapshot(&root).unwrap()["history"][0]["state"],
            "complete"
        );
        let path = home
            .path()
            .join("codex-plus/goals")
            .join(format!("{root}.json"));
        let saved: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(saved, v);
        assert_eq!(
            std::fs::metadata(path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }

    #[test]
    fn report_dispatch_is_once_only_and_never_clears_recovery_barrier() {
        let (_home, mut s, root) = fixture();
        start(&mut s, &root);
        s.goal_recovery(&root, "report", "unwatched gap", 1.0)
            .unwrap();
        let d = s
            .test_goal_report_intent(&root, "REPORT ONLY: preserve outcomes", 2.0)
            .unwrap()
            .unwrap();
        s.goal_claim_dispatch(d["id"].as_str().unwrap())
            .unwrap()
            .unwrap();
        s.goal_dispatch_result(
            d["id"].as_str().unwrap(),
            "accepted",
            Some("q"),
            Some("report-turn"),
            None,
            3.0,
        )
        .unwrap();
        assert!(
            s.goal_admit(
                &root,
                d["goal"].as_str().unwrap(),
                d["revision"].as_i64().unwrap(),
                "report-turn",
                d["input_epoch"].as_i64().unwrap(),
                4.0
            )
            .is_err()
        );
        let v = s.goal_finalize(&root, "report-turn", true, 5.0).unwrap();
        assert_eq!(v["report_barrier"], 1);
        assert_eq!(v["report_delivered"], 1);
        assert!(
            s.test_goal_report_intent(&root, "repeat", 6.0)
                .unwrap()
                .is_none()
        );
        assert!(
            s.test_goal_intent(&root, "continue", "continue", 6.0)
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn publication_is_bounded_and_does_not_include_history_or_staged_payload() {
        let (_home, mut s, root) = fixture();
        assert!(
            s.goal_start(&root, &"x".repeat(8193), None, false, 0.0)
                .is_err()
        );
        let v = s
            .goal_start(&root, &"x".repeat(8190), None, false, 0.0)
            .unwrap();
        admit(&mut s, &root, "turn");
        let d = json!({"disposition":"continue","next_action":"x".repeat(16000)});
        stage(&mut s, &root, "turn", d.clone());
        let projection = s.goal_publish(&root).unwrap();
        assert!(projection.get("history").is_none());
        assert!(projection["binding"].get("disposition").is_none());
        assert!(serde_json::to_vec(&projection).unwrap().len() < 65536);
        assert!(
            s.goal_checkpoint(
                &root,
                v["goal"]["id"].as_str().unwrap(),
                1,
                "turn",
                1,
                &json!({"disposition":"continue","next_action":"x".repeat(17000)}),
                3.0
            )
            .is_err()
        );
        s.goal_finalize(&root, "turn", true, 4.0).unwrap();
        assert!(
            serde_json::to_vec(&s.goal_publish(&root).unwrap())
                .unwrap()
                .len()
                < 65536
        );
    }

    #[test]
    fn resumed_goal_can_wait_on_its_original_jobs_but_stale_jobs_need_input() {
        let (_home, mut s, root) = fixture();
        start(&mut s, &root);
        admit(&mut s, &root, "original");
        job(&mut s, &root, "original", "job");
        stage(
            &mut s,
            &root,
            "original",
            json!({"disposition":"wait","jobs":["job"]}),
        );
        s.goal_finalize(&root, "original", true, 3.0).unwrap();
        mutate(&mut s, &root, "pause");
        mutate(&mut s, &root, "resume");
        admit(&mut s, &root, "newturn");
        stage(
            &mut s,
            &root,
            "newturn",
            json!({"disposition":"wait","jobs":["job"]}),
        );
        s.execute(
            "UPDATE parks SET state='expired' WHERE parent=?",
            &[json!(root)],
        )
        .unwrap();
        assert_eq!(
            s.goal_finalize(&root, "newturn", true, 5.0).unwrap()["goal"]["state"],
            "needs_input"
        );
    }

    #[test]
    fn lost_wait_results_block_autonomous_scheduling_without_rerunning_work() {
        let (_home, mut s, root) = fixture();
        start(&mut s, &root);
        admit(&mut s, &root, "turn");
        job(&mut s, &root, "turn", "job");
        stage(
            &mut s,
            &root,
            "turn",
            json!({"disposition":"wait","jobs":["job"]}),
        );
        s.goal_finalize(&root, "turn", true, 3.0).unwrap();
        s.execute("UPDATE jobs SET status='lost' WHERE id='job'", &[])
            .unwrap();
        assert!(!s.goal_wait_ready(&root, 4.0).unwrap());
        assert_eq!(
            s.goal_snapshot(&root).unwrap()["goal"]["state"],
            "needs_input"
        );
        assert!(
            s.test_goal_intent(&root, "completion", "inspect", 5.0)
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn completion_remains_visible_until_the_following_user_message() {
        let (_home, mut s, root) = fixture();
        start(&mut s, &root);
        input(&mut s, &root, false, 1.0);
        admit(&mut s, &root, "completion");
        stage(
            &mut s,
            &root,
            "completion",
            json!({"disposition":"complete","evidence":["check:passed"]}),
        );
        assert_eq!(
            s.goal_finalize(&root, "completion", true, 3.0).unwrap()["goal"]["state"],
            "complete"
        );
        assert!(input(&mut s, &root, false, 4.0)["goal"].is_null());
    }

    #[test]
    fn escaped_unicode_limits_are_checked_before_persisting_the_objective() {
        let (_home, mut s, root) = fixture();
        assert!(
            s.goal_start(&root, &"🚀".repeat(1000), None, false, 0.0)
                .is_err()
        );
        assert!(
            s.goal_snapshot(&root).unwrap()["history"]
                .as_array()
                .unwrap()
                .is_empty()
        );
        assert!(
            s.goal_start(&root, &"🚀".repeat(600), None, false, 0.0)
                .is_ok()
        );
        assert!(
            crate::local::ascii_json(&s.goal_status(&root).unwrap())
                .unwrap()
                .len()
                < 65536
        );
    }

    #[test]
    fn hidden_terminal_goals_allow_fresh_start_and_retain_their_terminal_history() {
        let (_home, mut s, root) = fixture();
        let v = start(&mut s, &root);
        mutate(&mut s, &root, "clear");
        let next = s.goal_start(&root, "new", None, false, 4.0).unwrap();
        assert_ne!(v["goal"]["id"], next["goal"]["id"]);
        assert_eq!(
            s.goal_snapshot(&root).unwrap()["history"][0]["state"],
            "cleared"
        );
        s.goal_mutate(
            &root,
            next["goal"]["id"].as_str().unwrap(),
            1,
            "complete",
            &json!({"evidence":["check:passed"]}),
            5.0,
        )
        .unwrap();
        assert!(
            s.goal_start(&root, "replace visible", None, false, 6.0)
                .is_err()
        );
        input(&mut s, &root, false, 7.0);
        s.goal_start(&root, "after completed", None, false, 8.0)
            .unwrap();
        assert_eq!(
            s.goal_snapshot(&root).unwrap()["history"][1]["state"],
            "complete"
        );
    }

    #[test]
    fn recovered_job_reports_are_coalesced_once_across_renewal_and_restart() {
        let (home, mut s, root) = fixture();
        start(&mut s, &root);
        admit(&mut s, &root, "turn");
        job(&mut s, &root, "turn", "job");
        stage(
            &mut s,
            &root,
            "turn",
            json!({"disposition":"wait","jobs":["job"]}),
        );
        s.goal_finalize(&root, "turn", true, 3.0).unwrap();
        s.execute(
            "UPDATE jobs SET status='completed',report=1 WHERE id='job'",
            &[],
        )
        .unwrap();
        let v = s.goal_reconcile_reports(&root, 4.0).unwrap();
        assert_eq!(v["report_barrier"], 1);
        assert_eq!(v["goal"]["state"], "needs_input");
        s.goal_report_delivered(&root, v["report_id"].as_str().unwrap())
            .unwrap();
        input(&mut s, &root, true, 5.0);
        drop(s);
        let mut s = Store::open(home.path()).unwrap();
        let again = s.goal_reconcile_reports(&root, 6.0).unwrap();
        assert_eq!(again["report_barrier"], 0);
        assert_eq!(again["report_id"], v["report_id"]);
        assert!(s.goal_report_jobs(&root).unwrap().is_empty());
    }

    #[test]
    fn pending_user_submission_blocks_dispatch_until_its_exact_acceptance() {
        let (home, mut s, root) = fixture();
        start(&mut s, &root);
        let first = Uuid::new_v4().to_string();
        let second = Uuid::new_v4().to_string();
        let v = s.goal_input_submission(&root, &first, false, 1.0).unwrap();
        assert_eq!(v["pending_input_state"], "submitting");
        assert!(
            s.test_goal_intent(&root, "continue", "race", 2.0)
                .unwrap()
                .is_none()
        );
        s.goal_input_submission(&root, &second, false, 3.0).unwrap();
        drop(s);
        let mut s = Store::open(home.path()).unwrap();
        let v = s
            .goal_input_settle(&root, &first, Some("first-turn"), "accepted", 4.0)
            .unwrap();
        assert_eq!(v["submission_id"], second);
        assert!(
            s.test_goal_intent(&root, "continue", "still blocked", 5.0)
                .unwrap()
                .is_none()
        );
        s.goal_input_settle(&root, &second, Some("second-turn"), "accepted", 6.0)
            .unwrap();
        assert!(
            s.test_goal_intent(&root, "continue", "after user admission", 7.0)
                .unwrap()
                .is_some()
        );
        assert!(s.goal_turns(&root).unwrap().is_empty());
    }

    #[test]
    fn uncertain_input_and_unrelated_failed_receipts_do_not_clear_the_latch() {
        let (_home, mut s, root) = fixture();
        start(&mut s, &root);
        let id = Uuid::new_v4().to_string();
        let v = s.goal_input_submission(&root, &id, false, 1.0).unwrap();
        let epoch = v["input_epoch"].clone();
        assert_eq!(
            s.goal_input_submission(&root, &id, false, 2.0).unwrap()["input_epoch"],
            epoch
        );
        assert!(
            s.goal_input_settle(&root, &Uuid::new_v4().to_string(), None, "failed", 3.0)
                .is_err()
        );
        let v = s
            .goal_input_settle(&root, &id, None, "uncertain", 4.0)
            .unwrap();
        assert_eq!(v["pending_input_state"], "uncertain");
        assert_eq!(v["attention"]["state"], "needs_input");
        assert!(
            s.test_goal_intent(&root, "continue", "blocked", 5.0)
                .unwrap()
                .is_none()
        );
        let v = s
            .goal_input_settle(&root, &id, None, "failed", 6.0)
            .unwrap();
        assert!(v["pending_input"].is_null());
        assert_eq!(v["goal"]["state"], "needs_input");
    }

    #[test]
    fn wait_cannot_adopt_an_independent_ready_callback() {
        let (_home, mut s, root) = fixture();
        start(&mut s, &root);
        job(&mut s, &root, "turn", "independent");
        s.execute(
            "UPDATE jobs SET status='completed' WHERE id='independent'",
            &[],
        )
        .unwrap();
        s.batch(&root).unwrap();
        let v = admit(&mut s, &root, "turn");
        assert!(
            s.goal_checkpoint(
                &root,
                v["goal"]["id"].as_str().unwrap(),
                1,
                "turn",
                1,
                &json!({"disposition":"wait","jobs":["independent"]}),
                2.0
            )
            .is_err()
        );
        assert!(
            s.goal_snapshot(&root).unwrap()["jobs"]
                .as_array()
                .unwrap()
                .is_empty()
        );
        mutate(&mut s, &root, "pause");
        assert!(s.next_callback(&root, false, false).unwrap().0.is_some());
    }

    #[test]
    fn goal_result_markers_do_not_leave_a_delivery_error_after_monitoring_recovers() {
        let (_home, mut s, root) = fixture();
        start(&mut s, &root);
        admit(&mut s, &root, "turn");
        job(&mut s, &root, "turn", "job");
        s.monitoring_failed(&root, "temporary").unwrap();
        s.monitoring_succeeded(&root).unwrap();
        assert_eq!(s.health(&root, Some(1.0)).unwrap()["state"], "waiting");
        mutate(&mut s, &root, "pause");
        s.goal_recovery(&root, "report", "gap", 2.0).unwrap();
        assert_eq!(s.goal_status(&root).unwrap()["goal"]["state"], "paused");
    }

    #[test]
    fn recovered_results_of_paused_or_terminal_goals_do_not_create_report_wakes() {
        for action in ["pause", "complete", "clear"] {
            let (_home, mut s, root) = fixture();
            let v = start(&mut s, &root);
            admit(&mut s, &root, "turn");
            job(&mut s, &root, "turn", "job");
            s.goal_mutate(
                &root,
                v["goal"]["id"].as_str().unwrap(),
                1,
                action,
                &json!({"evidence":["verified:complete"]}),
                2.0,
            )
            .unwrap();
            s.execute(
                "UPDATE jobs SET status='completed',report=1 WHERE id='job'",
                &[],
            )
            .unwrap();
            let v = s.goal_reconcile_reports(&root, 3.0).unwrap();
            assert_eq!(v["report_barrier"], 0, "{action}");
            assert!(
                s.test_goal_report_intent(&root, "REPORT ONLY", 4.0)
                    .unwrap()
                    .is_none(),
                "{action}"
            );
            let full = s.goal_snapshot(&root).unwrap();
            assert_eq!(full["jobs"][0]["status"], "completed");
            assert_eq!(
                full["history"][0]["state"],
                match action {
                    "pause" => "paused",
                    "complete" => "complete",
                    _ => "cleared",
                }
            );
            assert!(s.goal_report_jobs(&root).unwrap().is_empty());
            assert_eq!(
                s.goal_reconcile_reports(&root, 5.0).unwrap()["report_barrier"],
                0
            );
        }
    }

    #[test]
    fn recovered_replaced_goal_results_do_not_block_the_new_goal() {
        let (_home, mut s, root) = fixture();
        let old = start(&mut s, &root);
        admit(&mut s, &root, "oldturn");
        job(&mut s, &root, "oldturn", "oldjob");
        let new = s
            .goal_replace(
                &root,
                old["goal"]["id"].as_str().unwrap(),
                1,
                "new objective",
                None,
                false,
                2.0,
            )
            .unwrap();
        s.execute(
            "UPDATE jobs SET status='completed',report=1 WHERE id='oldjob'",
            &[],
        )
        .unwrap();
        let v = s.goal_reconcile_reports(&root, 3.0).unwrap();
        assert_eq!(v["report_barrier"], 0);
        assert_eq!(v["goal"]["id"], new["goal"]["id"]);
        assert_eq!(v["goal"]["revision"], 1);
        assert_eq!(v["goal"]["state"], "active");
        assert!(
            s.test_goal_report_intent(&root, "old report", 4.0)
                .unwrap()
                .is_none()
        );
        s.goal_finalize(&root, "oldturn", true, 5.0).unwrap();
        assert!(
            s.test_goal_intent(&root, "continue", "new objective", 6.0)
                .unwrap()
                .is_some()
        );
        assert_eq!(
            s.goal_snapshot(&root).unwrap()["jobs"][0]["goal"],
            old["goal"]["id"]
        );
    }

    #[test]
    fn report_claim_requires_the_frozen_goal_revision_state_and_input_epoch() {
        for changed in [
            "revision",
            "input_epoch",
            "paused",
            "complete",
            "cleared",
            "identity",
        ] {
            let (_home, mut s, root) = fixture();
            start(&mut s, &root);
            s.goal_recovery(&root, "gap", "unwatched gap", 1.0).unwrap();
            let d = s
                .test_goal_report_intent(&root, "REPORT ONLY", 2.0)
                .unwrap()
                .unwrap();
            match changed {
                "revision" => s
                    .execute(
                        "UPDATE goals SET revision=revision+1 WHERE root=?",
                        &[json!(root)],
                    )
                    .unwrap(),
                "input_epoch" => s
                    .execute(
                        "UPDATE goal_roots SET input_epoch=input_epoch+1 WHERE root=?",
                        &[json!(root)],
                    )
                    .unwrap(),
                "identity" => s
                    .execute(
                        "UPDATE goals SET id=? WHERE root=?",
                        &[json!(Uuid::new_v4().to_string()), json!(root)],
                    )
                    .unwrap(),
                state => s
                    .execute(
                        "UPDATE goals SET state=? WHERE root=?",
                        &[json!(state), json!(root)],
                    )
                    .unwrap(),
            }
            assert!(
                s.goal_claim_dispatch(d["id"].as_str().unwrap())
                    .unwrap()
                    .is_none(),
                "{changed}"
            );
        }
    }

    #[test]
    fn an_existing_recovery_barrier_does_not_authorize_reports_after_pause_or_completion() {
        for action in ["pause", "complete", "clear"] {
            let (_home, mut s, root) = fixture();
            start(&mut s, &root);
            let v = s.goal_recovery(&root, "gap", "unwatched gap", 1.0).unwrap();
            s.goal_mutate(
                &root,
                v["goal"]["id"].as_str().unwrap(),
                v["goal"]["revision"].as_i64().unwrap(),
                action,
                &json!({"evidence":["verified:complete"]}),
                2.0,
            )
            .unwrap();
            assert!(
                s.test_goal_report_intent(&root, "REPORT ONLY", 3.0)
                    .unwrap()
                    .is_none(),
                "{action}"
            );
        }
    }

    #[test]
    fn report_result_view_is_bounded_and_scoped_to_its_root_and_report_identity() {
        let (_home, mut s, root) = fixture();
        start(&mut s, &root);
        admit(&mut s, &root, "turn");
        let ids = (0..23).map(|n| format!("job-{n:02}")).collect::<Vec<_>>();
        for id in &ids {
            job(&mut s, &root, "turn", id);
        }
        stage(
            &mut s,
            &root,
            "turn",
            json!({"disposition":"wait","jobs":ids}),
        );
        s.goal_finalize(&root, "turn", true, 2.0).unwrap();
        s.execute("UPDATE jobs SET status='completed',report=1", &[])
            .unwrap();
        let v = s.goal_reconcile_reports(&root, 3.0).unwrap();
        let id = v["report_id"].as_str().unwrap();
        let results = s.goal_report_results(&root, id).unwrap();
        assert_eq!(results["total"], 23);
        assert_eq!(results["results"].as_array().unwrap().len(), 20);
        assert!(
            results["results"]
                .as_array()
                .unwrap()
                .iter()
                .all(|r| r.get("label").is_none() && r.get("objective").is_none())
        );
        assert_eq!(
            s.goal_report_results(&Uuid::new_v4().to_string(), id)
                .unwrap()["total"],
            0
        );
        assert_eq!(
            s.goal_report_results(&root, "different-report").unwrap()["total"],
            0
        );
    }
    #[test]
    fn prepared_messages_cannot_acquire_a_new_goal_revision_or_input_epoch() {
        for change in ["edit", "replace", "input_epoch"] {
            let (_home, mut s, root) = fixture();
            let expected = start(&mut s, &root);
            match change {
                "edit" => {
                    s.goal_mutate(
                        &root,
                        expected["goal"]["id"].as_str().unwrap(),
                        1,
                        "edit",
                        &json!({"objective":"new objective"}),
                        1.0,
                    )
                    .unwrap();
                }
                "replace" => {
                    s.goal_replace(
                        &root,
                        expected["goal"]["id"].as_str().unwrap(),
                        1,
                        "replacement",
                        None,
                        false,
                        1.0,
                    )
                    .unwrap();
                }
                _ => {
                    s.execute(
                        "UPDATE goal_roots SET input_epoch=input_epoch+1 WHERE root=?",
                        &[json!(root)],
                    )
                    .unwrap();
                }
            }
            assert!(
                s.goal_intent(&root, &expected, "continue", "old objective payload", 2.0)
                    .unwrap()
                    .is_none(),
                "{change}"
            );
            assert!(s.goal_dispatches(&root).unwrap().is_empty());
        }
    }

    #[test]
    fn prepared_report_messages_require_the_same_report_identity() {
        let (_home, mut s, root) = fixture();
        start(&mut s, &root);
        let expected = s.goal_recovery(&root, "report-one", "gap", 1.0).unwrap();
        s.execute(
            "UPDATE goal_roots SET report_id='report-two' WHERE root=?",
            &[json!(root)],
        )
        .unwrap();
        assert!(
            s.goal_report_intent(&root, &expected, "old report payload", 2.0)
                .unwrap()
                .is_none()
        );
        assert!(s.goal_dispatches(&root).unwrap().is_empty());
    }

    #[test]
    fn acceptance_persists_exact_turn_admission_atomically() {
        let (home, mut s, root) = fixture();
        start(&mut s, &root);
        let d = s
            .test_goal_intent(&root, "continue", "work", 1.0)
            .unwrap()
            .unwrap();
        s.goal_claim_dispatch(d["id"].as_str().unwrap()).unwrap();
        s.goal_accept_dispatch(
            d["id"].as_str().unwrap(),
            Some("queue"),
            "accepted-turn",
            2.0,
        )
        .unwrap();
        drop(s);
        let mut s = Store::open(home.path()).unwrap();
        let turn = s.goal_turns(&root).unwrap().remove(0);
        assert_eq!(turn["goal"], d["goal"]);
        assert_eq!(turn["revision"], d["revision"]);
        assert_eq!(turn["input_epoch"], d["input_epoch"]);
        assert_eq!(turn["turn"], "accepted-turn");
        assert_eq!(
            s.goal_finalize(&root, "accepted-turn", true, 3.0).unwrap()["goal"]["state"],
            "needs_input"
        );
        assert!(
            s.test_goal_intent(&root, "continue", "another turn", 4.0)
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn legacy_acceptance_crash_gaps_recover_admission_before_terminal_finalization() {
        for state in ["accepted", "done"] {
            let (home, mut s, root) = fixture();
            start(&mut s, &root);
            let d = s
                .test_goal_intent(&root, "continue", "work", 1.0)
                .unwrap()
                .unwrap();
            s.goal_claim_dispatch(d["id"].as_str().unwrap()).unwrap();
            s.execute("UPDATE goal_dispatches SET state=?,accepted_turn='finished-turn',queue='queue' WHERE id=?",&[json!(state),d["id"].clone()]).unwrap();
            assert!(s.goal_turns(&root).unwrap().is_empty());
            drop(s);
            let mut s = Store::open(home.path()).unwrap();
            if state == "done" {
                let newer = s
                    .test_goal_intent(&root, "continue", "reserved before legacy repair", 2.0)
                    .unwrap()
                    .unwrap();
                s.goal_reconcile_accepted(&root, 2.0).unwrap();
                assert_eq!(s.goal_turns(&root).unwrap().len(), 1);
                assert!(
                    s.goal_claim_dispatch(newer["id"].as_str().unwrap())
                        .unwrap()
                        .is_none()
                );
            }
            let v = s.goal_finalize(&root, "finished-turn", true, 3.0).unwrap();
            assert_eq!(v["goal"]["state"], "needs_input", "{state}");
            assert!(
                v["goal"]["error"]
                    .as_str()
                    .unwrap()
                    .contains("without a checkpoint")
            );
            assert!(
                s.test_goal_intent(&root, "continue", "must not wake", 4.0)
                    .unwrap()
                    .is_none()
            );
        }
    }

    #[test]
    fn late_acceptance_does_not_admit_or_reactivate_paused_or_replaced_goals() {
        for action in ["pause", "replace"] {
            let (_home, mut s, root) = fixture();
            let old = start(&mut s, &root);
            let d = s
                .test_goal_intent(&root, "continue", "old work", 1.0)
                .unwrap()
                .unwrap();
            s.goal_claim_dispatch(d["id"].as_str().unwrap()).unwrap();
            let changed = if action == "replace" {
                s.goal_replace(
                    &root,
                    old["goal"]["id"].as_str().unwrap(),
                    1,
                    "new",
                    None,
                    false,
                    2.0,
                )
                .unwrap()
            } else {
                mutate(&mut s, &root, "pause")
            };
            s.goal_accept_dispatch(d["id"].as_str().unwrap(), Some("queue"), "old-turn", 3.0)
                .unwrap();
            assert!(s.goal_turns(&root).unwrap().is_empty());
            s.goal_reconcile_accepted(&root, 4.0).unwrap();
            let final_state = s.goal_finalize(&root, "old-turn", true, 5.0).unwrap();
            assert_eq!(final_state["goal"]["id"], changed["goal"]["id"]);
            assert_eq!(final_state["goal"]["revision"], changed["goal"]["revision"]);
            assert_eq!(final_state["goal"]["state"], changed["goal"]["state"]);
        }
    }

    #[test]
    fn interrupted_old_origins_and_admissions_do_not_stop_a_replacement() {
        let (_home, mut s, root) = fixture();
        let old = start(&mut s, &root);
        admit(&mut s, &root, "old-turn");
        job(&mut s, &root, "old-turn", "job");
        let park = s.status(&root).unwrap()["parks"][0]["id"]
            .as_str()
            .unwrap()
            .to_owned();
        let new = s
            .goal_replace(
                &root,
                old["goal"]["id"].as_str().unwrap(),
                1,
                "new goal",
                None,
                false,
                2.0,
            )
            .unwrap();
        s.goal_origin_interrupted(&root, &park, 3.0).unwrap();
        s.goal_turn_uncertain(&root, "old-turn", "old projected interruption", 4.0)
            .unwrap();
        let current = s.goal_status(&root).unwrap();
        assert_eq!(current["goal"]["id"], new["goal"]["id"]);
        assert_eq!(current["goal"]["state"], "active");
        assert_eq!(current["goal"]["revision"], 1);
        assert_eq!(
            s.goal_snapshot(&root).unwrap()["jobs"][0]["status"],
            "armed"
        );
    }

    #[test]
    fn an_interruption_is_handled_once_and_cannot_defeat_an_explicit_resume() {
        let (_home, mut s, root) = fixture();
        start(&mut s, &root);
        let dispatched = s
            .test_goal_intent(&root, "continue", "work", 1.0)
            .unwrap()
            .unwrap();
        s.goal_claim_dispatch(dispatched["id"].as_str().unwrap())
            .unwrap();
        s.goal_accept_dispatch(
            dispatched["id"].as_str().unwrap(),
            Some("queue"),
            "turn",
            1.5,
        )
        .unwrap();
        job(&mut s, &root, "turn", "job");
        let park = s.status(&root).unwrap()["parks"][0]["id"]
            .as_str()
            .unwrap()
            .to_owned();
        assert_eq!(
            s.goal_origin_interrupted(&root, &park, 2.0).unwrap()["goal"]["state"],
            "needs_input"
        );
        let resumed = mutate(&mut s, &root, "resume");
        s.goal_origin_interrupted(&root, &park, 4.0).unwrap();
        s.goal_turn_uncertain(&root, "turn", "same old interruption", 5.0)
            .unwrap();
        let current = s.goal_status(&root).unwrap();
        assert_eq!(current["goal"]["state"], "active");
        assert_eq!(current["goal"]["revision"], resumed["goal"]["revision"]);
        assert_eq!(s.goal_dispatches(&root).unwrap()[0]["state"], "accepted");
        assert!(
            s.goal_snapshot(&root).unwrap()["events"]
                .as_array()
                .unwrap()
                .iter()
                .any(|e| e["event"] == "turn_uncertain"
                    && e["payload"]
                        .as_str()
                        .unwrap()
                        .contains("Goal origin interrupted"))
        );
        assert!(
            s.test_goal_intent(&root, "continue", "still fenced", 6.0)
                .unwrap()
                .is_none()
        );
        s.goal_dispatch_result(
            dispatched["id"].as_str().unwrap(),
            "done",
            None,
            None,
            None,
            7.0,
        )
        .unwrap();
        assert_eq!(
            s.first(
                "SELECT outcome FROM goal_turns WHERE root=? AND turn='turn'",
                &[json!(root)]
            )
            .unwrap()
            .unwrap()["outcome"],
            "uncertain_superseded"
        );
        assert!(
            s.test_goal_intent(&root, "continue", "renewed work", 8.0)
                .unwrap()
                .is_some()
        );
    }
    #[test]
    fn a_zero_job_old_admission_cannot_change_its_replacement() {
        let (_home, mut s, root) = fixture();
        let old = start(&mut s, &root);
        let dispatched = s
            .test_goal_intent(&root, "continue", "old work", 1.0)
            .unwrap()
            .unwrap();
        s.goal_claim_dispatch(dispatched["id"].as_str().unwrap())
            .unwrap();
        s.goal_accept_dispatch(
            dispatched["id"].as_str().unwrap(),
            Some("queue"),
            "old-zero-job-turn",
            1.5,
        )
        .unwrap();
        let new = s
            .goal_replace(
                &root,
                old["goal"]["id"].as_str().unwrap(),
                1,
                "new goal",
                None,
                false,
                2.0,
            )
            .unwrap();
        s.goal_dispatch_result(
            dispatched["id"].as_str().unwrap(),
            "uncertain",
            None,
            None,
            Some("old delivery outcome remains uncertain"),
            2.5,
        )
        .unwrap();
        let v = s
            .goal_turn_uncertain(
                &root,
                "old-zero-job-turn",
                "interruption with no positive terminal evidence",
                3.0,
            )
            .unwrap();
        assert_eq!(v["goal"]["id"], new["goal"]["id"]);
        assert_eq!(v["goal"]["revision"], 1);
        assert_eq!(v["goal"]["state"], "active");
        assert_eq!(s.goal_dispatches(&root).unwrap()[0]["state"], "uncertain");
        assert!(
            s.test_goal_intent(&root, "continue", "still fenced", 4.0)
                .unwrap()
                .is_none()
        );
        s.goal_dispatch_result(
            dispatched["id"].as_str().unwrap(),
            "done",
            None,
            None,
            None,
            5.0,
        )
        .unwrap();
        assert_eq!(
            s.first(
                "SELECT outcome FROM goal_turns WHERE root=? AND turn='old-zero-job-turn'",
                &[json!(root)]
            )
            .unwrap()
            .unwrap()["outcome"],
            "uncertain_superseded"
        );
        assert!(
            s.test_goal_intent(&root, "continue", "new work", 6.0)
                .unwrap()
                .is_some()
        );
    }

    #[test]
    fn uncertain_retirement_does_not_clear_a_pending_user_submission() {
        let (_home, mut s, root) = fixture();
        start(&mut s, &root);
        admit(&mut s, &root, "old-turn");
        s.goal_turn_uncertain(&root, "old-turn", "unknown interruption", 1.0)
            .unwrap();
        let id = Uuid::new_v4().to_string();
        s.goal_input_submission(&root, &id, false, 2.0).unwrap();
        mutate(&mut s, &root, "resume");
        assert_eq!(s.goal_status(&root).unwrap()["submission_id"], id);
        assert!(
            s.test_goal_intent(&root, "continue", "must wait for user admission", 3.0)
                .unwrap()
                .is_none()
        );
        s.goal_input_settle(&root, &id, Some("ordinary-turn"), "accepted", 4.0)
            .unwrap();
        assert!(
            s.test_goal_intent(&root, "continue", "after actual user admission", 5.0)
                .unwrap()
                .is_some()
        );
    }

    #[test]
    fn a_recovery_barrier_blocks_model_mutations_before_a_report_is_prepared() {
        let (_home, mut store, root) = fixture();
        start(&mut store, &root);
        let old_input = Uuid::new_v4().to_string();
        store
            .goal_input_submission(&root, &old_input, false, 1.0)
            .unwrap();
        store
            .goal_input_settle(
                &root,
                &old_input,
                Some("preexisting-stock-turn"),
                "accepted",
                2.0,
            )
            .unwrap();
        store.goal_recovery(&root, "report", "gap", 3.0).unwrap();
        assert!(store.goal_dispatches(&root).unwrap().is_empty());
        assert!(store.goal_model_mutation_blocked(&root).unwrap());
        let failed_input = Uuid::new_v4().to_string();
        store
            .goal_input_submission(&root, &failed_input, false, 4.0)
            .unwrap();
        assert!(store.goal_model_mutation_blocked(&root).unwrap());
        store
            .goal_input_settle(&root, &failed_input, None, "failed", 5.0)
            .unwrap();
        assert!(store.goal_model_mutation_blocked(&root).unwrap());
        let new_input = Uuid::new_v4().to_string();
        store
            .goal_input_submission(&root, &new_input, false, 6.0)
            .unwrap();
        store
            .goal_input_settle(&root, &new_input, None, "uncertain", 7.0)
            .unwrap();
        assert!(store.goal_model_mutation_blocked(&root).unwrap());
        store
            .goal_input_settle(
                &root,
                &new_input,
                Some("ordinary-user-turn"),
                "accepted",
                8.0,
            )
            .unwrap();
        assert!(!store.goal_model_mutation_blocked(&root).unwrap());
        let newer_input = Uuid::new_v4().to_string();
        store
            .goal_input_submission(&root, &newer_input, false, 9.0)
            .unwrap();
        assert!(store.goal_model_mutation_blocked(&root).unwrap());
        assert_eq!(store.goal_status(&root).unwrap()["report_barrier"], 1);
    }

    #[test]
    fn an_unattributed_submitted_report_cannot_authorize_model_mutations() {
        let (_home, mut store, root) = fixture();
        start(&mut store, &root);
        store.goal_recovery(&root, "report", "gap", 1.0).unwrap();
        let report = store
            .test_goal_report_intent(&root, "REPORT ONLY", 2.0)
            .unwrap()
            .unwrap();
        let id = report["id"].as_str().unwrap();
        store.goal_claim_dispatch(id).unwrap().unwrap();
        store
            .goal_dispatch_result(id, "pending", Some("queue"), None, None, 3.0)
            .unwrap();
        assert!(store.goal_model_mutation_blocked(&root).unwrap());
        let submission = Uuid::new_v4().to_string();
        store
            .goal_input_submission(&root, &submission, false, 4.0)
            .unwrap();
        assert!(store.goal_model_mutation_blocked(&root).unwrap());
        store
            .goal_input_settle(
                &root,
                &submission,
                Some("actual-user-turn"),
                "accepted",
                5.0,
            )
            .unwrap();
        assert!(!store.goal_model_mutation_blocked(&root).unwrap());
        assert_eq!(store.goal_status(&root).unwrap()["report_barrier"], 1);
    }

    #[test]
    fn report_turn_renewal_requires_an_accepted_later_ordinary_user_receipt() {
        let (_home, mut s, root) = fixture();
        start(&mut s, &root);
        s.goal_recovery(&root, "report", "gap", 1.0).unwrap();
        let report = s
            .test_goal_report_intent(&root, "REPORT ONLY", 2.0)
            .unwrap()
            .unwrap();
        s.goal_claim_dispatch(report["id"].as_str().unwrap())
            .unwrap();
        s.goal_accept_dispatch(
            report["id"].as_str().unwrap(),
            Some("queue"),
            "report-turn",
            3.0,
        )
        .unwrap();
        s.goal_finalize(&root, "report-turn", true, 4.0).unwrap();
        assert!(s.goal_model_mutation_blocked(&root).unwrap());
        s.execute(
            "UPDATE goal_roots SET input_epoch=input_epoch+1 WHERE root=?",
            &[json!(root)],
        )
        .unwrap();
        assert!(s.goal_model_mutation_blocked(&root).unwrap());
        let input = Uuid::new_v4().to_string();
        s.goal_input_submission(&root, &input, false, 5.0).unwrap();
        assert!(s.goal_model_mutation_blocked(&root).unwrap());
        s.goal_input_settle(&root, &input, None, "uncertain", 6.0)
            .unwrap();
        assert!(s.goal_model_mutation_blocked(&root).unwrap());
        s.goal_input_settle(&root, &input, Some("report-turn"), "accepted", 7.0)
            .unwrap();
        assert!(!s.goal_model_mutation_blocked(&root).unwrap());
        assert_eq!(s.goal_status(&root).unwrap()["report_barrier"], 1);
    }
    #[test]
    fn a_partial_completion_during_origin_eos_freezes_only_the_results_in_its_message() {
        let (_home, mut s, root) = fixture();
        start(&mut s, &root);
        admit(&mut s, &root, "origin");
        job(&mut s, &root, "origin", "A");
        job(&mut s, &root, "origin", "B");
        stage(
            &mut s,
            &root,
            "origin",
            json!({"disposition":"wait","jobs":["A","B"]}),
        );
        s.execute("UPDATE jobs SET status='completed' WHERE id='A'", &[])
            .unwrap();
        let v = s.goal_finalize(&root, "origin", true, 3.0).unwrap();
        assert_eq!(v["goal"]["state"], "active");
        assert_eq!(v["running_jobs"], 1);
        assert_eq!(v["completion_jobs"], json!(["A"]));
        assert!(!s.goal_wait_ready(&root, 4.0).unwrap());
        let first = s
            .goal_intent(
                &root,
                &v,
                "completion",
                "A completed; B is still running",
                4.0,
            )
            .unwrap()
            .unwrap();
        assert_eq!(first["completion_jobs"], json!(["A"]));
        s.goal_claim_dispatch(first["id"].as_str().unwrap())
            .unwrap();
        s.execute("UPDATE jobs SET status='completed' WHERE id='B'", &[])
            .unwrap();
        s.goal_accept_dispatch(
            first["id"].as_str().unwrap(),
            Some("queue-A"),
            "wake-A",
            5.0,
        )
        .unwrap();
        let jobs = s.goal_snapshot(&root).unwrap()["jobs"]
            .as_array()
            .unwrap()
            .clone();
        assert_eq!(
            jobs.iter().find(|j| j["job"] == "A").unwrap()["notified"],
            1
        );
        assert_eq!(
            jobs.iter().find(|j| j["job"] == "B").unwrap()["notified"],
            0
        );
        stage(
            &mut s,
            &root,
            "wake-A",
            json!({"disposition":"wait","jobs":["A","B"]}),
        );
        let v = s.goal_finalize(&root, "wake-A", true, 6.0).unwrap();
        assert_eq!(v["goal"]["state"], "active");
        assert_eq!(v["completion_jobs"], json!(["B"]));
        let second = s
            .goal_intent(&root, &v, "completion", "B completed", 7.0)
            .unwrap()
            .unwrap();
        assert_eq!(second["completion_jobs"], json!(["B"]));
        s.goal_claim_dispatch(second["id"].as_str().unwrap())
            .unwrap();
        s.goal_accept_dispatch(
            second["id"].as_str().unwrap(),
            Some("queue-B"),
            "wake-B",
            8.0,
        )
        .unwrap();
        stage(
            &mut s,
            &root,
            "wake-B",
            json!({"disposition":"wait","jobs":["A","B"]}),
        );
        assert_eq!(
            s.goal_finalize(&root, "wake-B", true, 9.0).unwrap()["goal"]["state"],
            "needs_input"
        );
        assert!(
            s.goal_completion_results(&root).unwrap()["results"]
                .as_array()
                .unwrap()
                .is_empty()
        );
        assert!(
            s.test_goal_intent(&root, "completion", "duplicate", 10.0)
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn an_accepted_partial_completion_does_not_poll_or_repeat_while_another_job_runs() {
        let (home, mut s, root) = fixture();
        start(&mut s, &root);
        admit(&mut s, &root, "origin");
        job(&mut s, &root, "origin", "A");
        job(&mut s, &root, "origin", "B");
        stage(
            &mut s,
            &root,
            "origin",
            json!({"disposition":"wait","jobs":["A","B"]}),
        );
        assert_eq!(
            s.goal_finalize(&root, "origin", true, 2.0).unwrap()["goal"]["state"],
            "waiting"
        );
        s.execute("UPDATE jobs SET status='completed' WHERE id='A'", &[])
            .unwrap();
        assert!(s.goal_wait_ready(&root, 3.0).unwrap());
        assert_eq!(s.goal_status(&root).unwrap()["running_jobs"], 1);
        let first = s
            .test_goal_intent(&root, "completion", "A completed", 4.0)
            .unwrap()
            .unwrap();
        s.goal_claim_dispatch(first["id"].as_str().unwrap())
            .unwrap();
        s.goal_accept_dispatch(
            first["id"].as_str().unwrap(),
            Some("queue-A"),
            "wake-A",
            5.0,
        )
        .unwrap();
        stage(
            &mut s,
            &root,
            "wake-A",
            json!({"disposition":"wait","jobs":["A","B"]}),
        );
        assert_eq!(
            s.goal_finalize(&root, "wake-A", true, 6.0).unwrap()["goal"]["state"],
            "waiting"
        );
        drop(s);
        let mut s = Store::open(home.path()).unwrap();
        s.execute("UPDATE jobs SET status='completed' WHERE id='A'", &[])
            .unwrap();
        assert!(!s.goal_wait_ready(&root, 10000.0).unwrap());
        assert_eq!(s.goal_completion_results(&root).unwrap()["total"], 0);
        assert!(
            s.test_goal_intent(&root, "review", "must not poll B", 10000.0)
                .unwrap()
                .is_none()
        );
        assert!(s.goal_dispatches(&root).unwrap().is_empty());
        s.execute("UPDATE jobs SET status='completed' WHERE id='B'", &[])
            .unwrap();
        assert!(s.goal_wait_ready(&root, 10001.0).unwrap());
        let second = s
            .test_goal_intent(&root, "completion", "B completed", 10002.0)
            .unwrap()
            .unwrap();
        assert_eq!(second["completion_jobs"], json!(["B"]));
        s.goal_claim_dispatch(second["id"].as_str().unwrap())
            .unwrap();
        s.goal_accept_dispatch(
            second["id"].as_str().unwrap(),
            Some("queue-B"),
            "wake-B",
            10003.0,
        )
        .unwrap();
        stage(
            &mut s,
            &root,
            "wake-B",
            json!({"disposition":"complete","evidence":["verified:A+B"]}),
        );
        assert_eq!(
            s.goal_finalize(&root, "wake-B", true, 10004.0).unwrap()["goal"]["state"],
            "complete"
        );
        assert_eq!(
            s.goal_snapshot(&root).unwrap()["dispatches"]
                .as_array()
                .unwrap()
                .len(),
            2
        );
    }

    #[test]
    fn invalidating_a_reserved_completion_does_not_consume_its_event() {
        for action in ["pause", "input"] {
            let (_home, mut s, root) = fixture();
            start(&mut s, &root);
            admit(&mut s, &root, "origin");
            job(&mut s, &root, "origin", "A");
            job(&mut s, &root, "origin", "B");
            stage(
                &mut s,
                &root,
                "origin",
                json!({"disposition":"wait","jobs":["A","B"]}),
            );
            s.goal_finalize(&root, "origin", true, 2.0).unwrap();
            s.execute("UPDATE jobs SET status='completed' WHERE id='A'", &[])
                .unwrap();
            s.goal_wait_ready(&root, 3.0).unwrap();
            let old = s
                .test_goal_intent(&root, "completion", "A completed", 4.0)
                .unwrap()
                .unwrap();
            if action == "pause" {
                mutate(&mut s, &root, "pause");
                mutate(&mut s, &root, "resume");
            } else {
                input(&mut s, &root, false, 5.0);
            }
            assert!(
                s.goal_claim_dispatch(old["id"].as_str().unwrap())
                    .unwrap()
                    .is_none()
            );
            assert_eq!(s.goal_completion_results(&root).unwrap()["total"], 1);
            let fresh = s
                .test_goal_intent(&root, "completion", "A is still unnotified", 6.0)
                .unwrap()
                .unwrap();
            assert_eq!(fresh["completion_jobs"], json!(["A"]));
            s.goal_claim_dispatch(fresh["id"].as_str().unwrap())
                .unwrap();
            assert_eq!(s.goal_completion_results(&root).unwrap()["total"], 1);
            s.goal_accept_dispatch(fresh["id"].as_str().unwrap(), Some("queue"), "wake-A", 7.0)
                .unwrap();
            assert_eq!(s.goal_completion_results(&root).unwrap()["total"], 0);
        }
    }
    #[test]
    fn an_unknown_other_job_blocks_a_prepared_partial_completion() {
        let (home, mut s, root) = fixture();
        start(&mut s, &root);
        admit(&mut s, &root, "origin");
        job(&mut s, &root, "origin", "A");
        job(&mut s, &root, "origin", "B");
        stage(
            &mut s,
            &root,
            "origin",
            json!({"disposition":"wait","jobs":["A","B"]}),
        );
        s.execute("UPDATE jobs SET status='completed' WHERE id='A'", &[])
            .unwrap();
        let expected = s.goal_finalize(&root, "origin", true, 2.0).unwrap();
        s.execute("UPDATE jobs SET status='lost' WHERE id='B'", &[])
            .unwrap();
        assert!(
            s.goal_intent(&root, &expected, "completion", "A completed", 3.0)
                .unwrap()
                .is_none()
        );
        assert_eq!(
            s.goal_status(&root).unwrap()["goal"]["state"],
            "needs_input"
        );
        let saved: Value = serde_json::from_slice(
            &std::fs::read(
                home.path()
                    .join("codex-plus/goals")
                    .join(format!("{root}.json")),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(saved["goal"]["state"], "needs_input");
        assert!(s.goal_dispatches(&root).unwrap().is_empty());
    }
}
fn decode(mut row: Value) -> Result<Value> {
    if let Some(text) = row["checkpoint"].as_str() {
        row["checkpoint"] = serde_json::from_str(text)?;
    }
    Ok(row)
}

impl Store {
    pub(crate) fn init_goals(&mut self) -> Result<()> {
        self.transaction(true, |s| {
            for sql in [
                "CREATE TABLE IF NOT EXISTS goal_roots (root TEXT PRIMARY KEY,input_epoch INTEGER NOT NULL DEFAULT 0,report_barrier INTEGER NOT NULL DEFAULT 0,report_id TEXT,report_delivered INTEGER NOT NULL DEFAULT 0,completed_visible INTEGER NOT NULL DEFAULT 1)",
                "CREATE TABLE IF NOT EXISTS goals (id TEXT PRIMARY KEY,root TEXT NOT NULL,revision INTEGER NOT NULL,current INTEGER NOT NULL,state TEXT NOT NULL,objective TEXT NOT NULL,checkpoint TEXT,no_progress INTEGER NOT NULL DEFAULT 0,review_seconds INTEGER,review_due REAL,error TEXT,created REAL NOT NULL,updated REAL NOT NULL)",
                "CREATE UNIQUE INDEX IF NOT EXISTS goal_current_root ON goals(root) WHERE current=1",
                "CREATE TABLE IF NOT EXISTS goal_turns (root TEXT NOT NULL,goal TEXT NOT NULL,revision INTEGER NOT NULL,turn TEXT NOT NULL,input_epoch INTEGER NOT NULL,disposition TEXT,finalized INTEGER NOT NULL DEFAULT 0,outcome TEXT,created REAL NOT NULL,PRIMARY KEY(root,turn,input_epoch))",
                "CREATE TABLE IF NOT EXISTS goal_dispatches (id TEXT PRIMARY KEY,root TEXT NOT NULL,goal TEXT NOT NULL,revision INTEGER NOT NULL,input_epoch INTEGER NOT NULL,client TEXT UNIQUE NOT NULL,message TEXT NOT NULL,kind TEXT NOT NULL,state TEXT NOT NULL,queue TEXT,accepted_turn TEXT,error TEXT,report_id TEXT,created REAL NOT NULL)",
                "CREATE UNIQUE INDEX IF NOT EXISTS goal_one_dispatch ON goal_dispatches(root) WHERE state IN ('ready','submitting','pending','starting','accepted','uncertain')",
                "CREATE INDEX IF NOT EXISTS goal_dispatch_binding ON goal_dispatches(root,goal,revision,input_epoch)",
                "CREATE TABLE IF NOT EXISTS goal_jobs (goal TEXT NOT NULL,job TEXT NOT NULL,turn TEXT NOT NULL,input_epoch INTEGER NOT NULL,PRIMARY KEY(goal,job))",
                "CREATE TABLE IF NOT EXISTS goal_dispatch_jobs (dispatch TEXT NOT NULL,job TEXT NOT NULL,PRIMARY KEY(dispatch,job))",
                "CREATE TABLE IF NOT EXISTS goal_evidence (goal TEXT NOT NULL,reference TEXT NOT NULL,PRIMARY KEY(goal,reference))",
                "CREATE TABLE IF NOT EXISTS goal_events (id INTEGER PRIMARY KEY,goal TEXT NOT NULL,revision INTEGER NOT NULL,event TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS goal_provenance (root TEXT PRIMARY KEY,value TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS goal_job_reports (job TEXT PRIMARY KEY,report_id TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS goal_inputs (root TEXT NOT NULL,submission_id TEXT NOT NULL,input_epoch INTEGER NOT NULL,state TEXT NOT NULL,turn TEXT,created REAL NOT NULL,updated REAL NOT NULL,PRIMARY KEY(root,submission_id))",
                "CREATE TABLE IF NOT EXISTS goal_turn_issues (root TEXT NOT NULL,turn TEXT NOT NULL,input_epoch INTEGER NOT NULL,created REAL NOT NULL,PRIMARY KEY(root,turn,input_epoch))",
            ] { s.execute(sql,&[])?; }
            if !s.rows("PRAGMA table_info(goal_dispatches)",&[])?.iter().any(|r|r["name"]=="report_id") {
                s.execute("ALTER TABLE goal_dispatches ADD COLUMN report_id TEXT",&[])?;
            }
            if !s.rows("PRAGMA table_info(goal_dispatches)",&[])?.iter().any(|r|r["name"]=="settlement_started") {
                s.execute("ALTER TABLE goal_dispatches ADD COLUMN settlement_started REAL",&[])?;
            }
            if !s.rows("PRAGMA table_info(goal_roots)",&[])?.iter().any(|r|r["name"]=="pending_input") {
                s.execute("ALTER TABLE goal_roots ADD COLUMN pending_input TEXT",&[])?;
            }
            if !s.rows("PRAGMA table_info(goal_jobs)",&[])?.iter().any(|r|r["name"]=="notified") {
                s.execute("ALTER TABLE goal_jobs ADD COLUMN notified INTEGER NOT NULL DEFAULT 0",&[])?;
            }
            Ok(())
        })
    }

    fn goal_root(&mut self, root: &str) -> Result<()> {
        Uuid::parse_str(root)?;
        self.execute(
            "INSERT OR IGNORE INTO goal_roots(root) VALUES (?)",
            &[json!(root)],
        )
    }
    fn goal_current(&mut self, root: &str) -> Result<Option<Value>> {
        self.first(
            "SELECT * FROM goals WHERE root=? AND current=1",
            &[json!(root)],
        )?
        .map(decode)
        .transpose()
    }
    fn goal_expected(&mut self, root: &str, id: &str, revision: i64) -> Result<Value> {
        let goal = self
            .goal_current(root)?
            .ok_or_else(|| anyhow::anyhow!("no current goal"))?;
        ensure!(
            goal["id"] == id && goal["revision"] == revision,
            "stale goal identity or revision"
        );
        Ok(goal)
    }
    fn goal_invalidate(&mut self, root: &str) -> Result<()> {
        self.execute(
            "UPDATE goal_dispatches SET state='invalidated' WHERE root=? AND state='ready'",
            &[json!(root)],
        )
    }
    fn goal_event(&mut self, goal: &Value, event: &str, payload: &Value, now: f64) -> Result<()> {
        self.execute(
            "INSERT INTO goal_events(goal,revision,event,payload,created) VALUES (?,?,?,?,?)",
            &[
                goal["id"].clone(),
                goal["revision"].clone(),
                json!(event),
                json!(payload.to_string()),
                json!(now),
            ],
        )
    }

    pub fn goal_start(
        &mut self,
        root: &str,
        objective: &str,
        review_seconds: Option<i64>,
        paused: bool,
        now: f64,
    ) -> Result<Value> {
        ensure!(
            !objective.trim().is_empty()
                && crate::local::ascii_json(&json!(objective))?.len() <= 8192,
            "objective must be nonempty and at most 8192 bytes"
        );
        ensure!(
            review_seconds.is_none_or(|n| n == 3600 || n == 7200),
            "review interval must be 3600 or 7200 seconds"
        );
        self.transaction(true, |s| {
            if let Some(goal) = s.goal_current(root)? {
                let hidden_complete = goal["state"] == "complete"
                    && s.first(
                        "SELECT completed_visible FROM goal_roots WHERE root=?",
                        &[json!(root)],
                    )?
                    .is_some_and(|m| m["completed_visible"] == 0);
                ensure!(
                    goal["state"] == "cleared" || hidden_complete,
                    "replacing a goal requires its expected identity and revision"
                );
            }
            s.goal_start_inner(root, objective, review_seconds, paused, now)
        })?;
        self.goal_publish(root)
    }

    pub fn goal_replace(
        &mut self,
        root: &str,
        id: &str,
        revision: i64,
        objective: &str,
        review_seconds: Option<i64>,
        paused: bool,
        now: f64,
    ) -> Result<Value> {
        ensure!(
            !objective.trim().is_empty()
                && crate::local::ascii_json(&json!(objective))?.len() <= 8192,
            "objective must be nonempty and at most 8192 bytes"
        );
        ensure!(
            review_seconds.is_none_or(|n| n == 3600 || n == 7200),
            "review interval must be 3600 or 7200 seconds"
        );
        self.transaction(true, |s| {
            s.goal_expected(root, id, revision)?;
            s.goal_start_inner(root, objective, review_seconds, paused, now)
        })?;
        self.goal_publish(root)
    }

    fn goal_start_inner(
        &mut self,
        root: &str,
        objective: &str,
        review_seconds: Option<i64>,
        paused: bool,
        now: f64,
    ) -> Result<()> {
        let s = self;
        s.goal_root(root)?;
        s.goal_invalidate(root)?;
        s.goal_retire_uncertain(root)?;
        if let Some(old) = s.goal_current(root)? {
            s.goal_event(&old, "replaced", &Value::Null, now)?;
            s.execute("UPDATE goals SET current=0,state=CASE WHEN state IN ('complete','cleared') THEN state ELSE 'replaced' END,revision=revision+1,updated=? WHERE id=?", &[json!(now),old["id"].clone()])?;
        }
        s.execute("UPDATE goal_roots SET input_epoch=input_epoch+1,report_barrier=0,completed_visible=1 WHERE root=?", &[json!(root)])?;
        s.execute("INSERT INTO goals(id,root,revision,current,state,objective,review_seconds,review_due,created,updated) VALUES (?,?,1,1,?,?,?,?,?,?)", &[json!(Uuid::new_v4().to_string()),json!(root),json!(if paused {"paused"} else {"active"}),json!(objective),json!(review_seconds),json!(review_seconds.map(|n|now+n as f64)),json!(now),json!(now)])?;
        let goal = s.goal_current(root)?.unwrap();
        s.goal_event(&goal, "start", &Value::Null, now)
    }

    fn goal_projection(&mut self, root: &str) -> Result<Value> {
        let meta=self.first("SELECT * FROM goal_roots WHERE root=?", &[json!(root)])?.unwrap_or_else(||json!({"input_epoch":0,"report_barrier":0,"report_id":null,"report_delivered":0,"completed_visible":1}));
        let mut goal = self.goal_current(root)?.unwrap_or(Value::Null);
        if goal["state"] == "cleared"
            || (goal["state"] == "complete" && meta["completed_visible"] == 0)
        {
            goal = Value::Null;
        }
        if goal.is_object() {
            let count=self.first("SELECT COUNT(*) AS count FROM goal_jobs JOIN jobs ON jobs.id=goal_jobs.job WHERE goal_jobs.goal=? AND jobs.status='armed'", &[goal["id"].clone()])?.unwrap();
            goal["running_jobs"] = count["count"].clone();
            goal["waiting_jobs"] = count["count"].clone();
        }
        let completion = if goal.is_object() {
            self.goal_completion_results_for(&goal)?
        } else {
            json!({"total":0,"results":[]})
        };
        let completion_jobs = completion["results"]
            .as_array()
            .unwrap()
            .iter()
            .map(|job| job["id"].clone())
            .collect::<Vec<_>>();
        let binding=self.first("SELECT root,goal,revision,turn,input_epoch FROM goal_turns WHERE root=? AND finalized=0 ORDER BY created DESC,input_epoch DESC LIMIT 1", &[json!(root)])?.unwrap_or(Value::Null);
        let open_dispatches=self.rows(&format!("SELECT id,goal,revision,state,client,queue,accepted_turn,error FROM goal_dispatches WHERE root=? AND {OPEN} LIMIT 1"), &[json!(root)])?;
        let running_jobs=self.first("SELECT COUNT(*) AS count FROM goal_jobs JOIN jobs ON jobs.id=goal_jobs.job JOIN goals ON goals.id=goal_jobs.goal WHERE goals.root=? AND jobs.status='armed'", &[json!(root)])?.unwrap()["count"].clone();
        let attention=open_dispatches.iter().find(|d|d["state"]=="uncertain").map(|d|json!({"state":"needs_input","dispatch_id":d["id"],"reason":"An earlier goal submission has an uncertain outcome; reconcile it before any further autonomous goal dispatch."}));
        let pending_input=self.first("SELECT submission_id,input_epoch,state,turn,created,updated FROM goal_inputs WHERE root=? AND submission_id=?", &[json!(root),meta["pending_input"].clone()])?.unwrap_or(Value::Null);
        let attention = if pending_input["state"] == "uncertain" {
            Some(
                json!({"state":"needs_input","reason":"The latest user input admission is uncertain; reconcile that exact submission before goal continuation."}),
            )
        } else {
            attention
        };
        Ok(
            json!({"version":1,"root":root,"input_epoch":meta["input_epoch"],"report_barrier":meta["report_barrier"],"report_id":meta["report_id"],"report_delivered":meta["report_delivered"],"goal":goal,"binding":binding,"open_dispatches":open_dispatches,"attention":attention,"running_jobs":running_jobs,"pending_input":pending_input,"pending_input_epoch":pending_input["input_epoch"],"pending_input_state":pending_input["state"],"submission_id":pending_input["submission_id"],"completion_pending":completion["total"],"completion_jobs":completion_jobs}),
        )
    }

    pub fn goal_snapshot(&mut self, root: &str) -> Result<Value> {
        let mut view = self.goal_projection(root)?;
        let history = self
            .rows(
                "SELECT * FROM goals WHERE root=? ORDER BY created,id",
                &[json!(root)],
            )?
            .into_iter()
            .map(decode)
            .collect::<Result<Vec<_>>>()?;
        let jobs=self.rows("SELECT goal_jobs.*,jobs.status,jobs.kind,jobs.thread,jobs.exit_code FROM goal_jobs JOIN jobs ON jobs.id=goal_jobs.job JOIN goals ON goals.id=goal_jobs.goal WHERE goals.root=?", &[json!(root)])?;
        let events=self.rows("SELECT goal_events.* FROM goal_events JOIN goals ON goals.id=goal_events.goal WHERE goals.root=? ORDER BY goal_events.id", &[json!(root)])?;
        let dispatches = self.rows(
            "SELECT * FROM goal_dispatches WHERE root=? ORDER BY created,id",
            &[json!(root)],
        )?;
        view["history"] = json!(history);
        view["jobs"] = json!(jobs);
        view["events"] = json!(events);
        view["dispatches"] = json!(
            dispatches
                .into_iter()
                .map(|dispatch| self.goal_dispatch_view(dispatch))
                .collect::<Result<Vec<_>>>()?
        );
        Ok(view)
    }

    pub fn goal_status(&mut self, root: &str) -> Result<Value> {
        self.goal_projection(root)
    }

    fn goal_completion_results_for(&mut self, goal: &Value) -> Result<Value> {
        let jobs = if goal["checkpoint"]["disposition"] == "wait" {
            goal["checkpoint"]["jobs"].clone()
        } else {
            json!([])
        };
        let args = [goal["id"].clone(), json!(jobs.to_string())];
        let total=self.first("SELECT COUNT(*) AS count FROM goal_jobs g JOIN jobs j ON j.id=g.job WHERE g.goal=? AND g.notified=0 AND j.status IN ('completed','failed','interrupted') AND j.id IN (SELECT value FROM json_each(?))",&args)?.unwrap()["count"].clone();
        let mut results=self.rows("SELECT j.id,j.kind,j.thread,j.turn,j.item,j.execution,j.status,j.exit_code FROM goal_jobs g JOIN jobs j ON j.id=g.job WHERE g.goal=? AND g.notified=0 AND j.status IN ('completed','failed','interrupted') AND j.id IN (SELECT value FROM json_each(?)) ORDER BY j.id LIMIT 20",&args)?;
        crate::ledger::add_execution_refs(
            &self.directory,
            goal["root"].as_str().unwrap(),
            &mut results,
        );
        Ok(json!({"total":total,"results":results}))
    }

    pub fn goal_completion_results(&mut self, root: &str) -> Result<Value> {
        match self.goal_current(root)? {
            Some(goal) => self.goal_completion_results_for(&goal),
            None => Ok(json!({"total":0,"results":[]})),
        }
    }

    fn goal_dispatch_view(&mut self, mut dispatch: Value) -> Result<Value> {
        let jobs = self.rows(
            "SELECT job FROM goal_dispatch_jobs WHERE dispatch=? ORDER BY job",
            &[dispatch["id"].clone()],
        )?;
        dispatch["completion_jobs"] = json!(
            jobs.into_iter()
                .map(|job| job["job"].clone())
                .collect::<Vec<_>>()
        );
        Ok(dispatch)
    }

    pub fn goal_publish(&mut self, root: &str) -> Result<Value> {
        use std::os::unix::fs::PermissionsExt;
        Uuid::parse_str(root)?;
        let directory = self.directory.join("goals");
        if let Ok(meta) = std::fs::symlink_metadata(&directory) {
            ensure!(
                meta.is_dir() && !meta.file_type().is_symlink(),
                "goal projection directory must be a real directory"
            );
        } else {
            std::fs::create_dir(&directory)?;
        }
        std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o700))?;
        self.transaction(true, |s| {
            let view = s.goal_projection(root)?;
            ensure!(
                crate::local::ascii_json(&view)?.len() <= 65536,
                "goal projection exceeds status size limit"
            );
            crate::local::atomic_json(&directory.join(format!("{root}.json")), &view)?;
            Ok(view)
        })
    }

    pub fn goal_roots(&mut self) -> Result<Vec<String>> {
        Ok(self
            .rows("SELECT root FROM goal_roots ORDER BY root", &[])?
            .into_iter()
            .filter_map(|r| r["root"].as_str().map(str::to_owned))
            .collect())
    }
    pub fn goal_turns(&mut self, root: &str) -> Result<Vec<Value>> {
        self.rows(
            "SELECT * FROM goal_turns WHERE root=? AND finalized=0 ORDER BY created,input_epoch",
            &[json!(root)],
        )
    }

    pub fn goal_mutate(
        &mut self,
        root: &str,
        id: &str,
        revision: i64,
        action: &str,
        payload: &Value,
        now: f64,
    ) -> Result<Value> {
        ensure!(
            crate::local::ascii_json(payload)?.len() <= 16384,
            "goal mutation payload exceeds 16384 bytes"
        );
        self.transaction(true, |s| {
            let goal=s.goal_expected(root,id,revision)?;
            let state=match action {
                "pause"=>"paused","resume"=>"active","clear"=>"cleared","complete"=>"complete","edit"|"review"=>goal["state"].as_str().unwrap(),_=>bail!("unknown goal action"),
            };
            ensure!(action!="resume" || !matches!(goal["state"].as_str(),Some("complete"|"cleared"|"replaced")),"goal cannot be resumed");
            let objective=if action=="edit" {required(payload,"objective")?} else {goal["objective"].as_str().unwrap()};
            ensure!(crate::local::ascii_json(&json!(objective))?.len()<=8192,"objective exceeds 8192 encoded bytes");
            if action=="complete" {ensure!(payload["evidence"].as_array().is_some_and(|a|!a.is_empty() && a.iter().all(|r|r.as_str().is_some_and(|t|!t.trim().is_empty()))),"completion evidence references required");}
            let mut review=payload.get("review_seconds").cloned().unwrap_or_else(||goal["review_seconds"].clone());
            if review==0 {review=Value::Null;}
            ensure!(review.is_null() || matches!(review.as_i64(),Some(3600|7200)),"review interval must be 3600 or 7200 seconds");
            s.goal_invalidate(root)?;
            s.execute("UPDATE goals SET revision=revision+1,state=?,objective=?,review_seconds=?,review_due=?,error=NULL,no_progress=0,updated=? WHERE id=?", &[json!(state),json!(objective),review.clone(),json!(review.as_i64().map(|n|now+n as f64)),json!(now),json!(id)])?;
            if action=="complete" {s.execute("UPDATE goal_roots SET completed_visible=1 WHERE root=?",&[json!(root)])?;}
            if matches!(action,"resume"|"edit") {s.execute("UPDATE goal_roots SET input_epoch=input_epoch+1,report_barrier=0 WHERE root=?", &[json!(root)])?;}
            if action=="resume" {s.goal_retire_uncertain(root)?;}
            s.goal_event(&goal,action,payload,now)
        })?;
        self.goal_publish(root)
    }

    pub fn goal_input(&mut self, root: &str, renewed: bool, now: f64) -> Result<Value> {
        self.goal_input_submission(root, &Uuid::new_v4().to_string(), renewed, now)
    }

    pub fn goal_input_submission(
        &mut self,
        root: &str,
        submission_id: &str,
        renewed: bool,
        now: f64,
    ) -> Result<Value> {
        Uuid::parse_str(submission_id)?;
        self.transaction(true, |s| {
            s.goal_root(root)?;
            if s.exists("SELECT 1 FROM goal_inputs WHERE root=? AND submission_id=?",&[json!(root),json!(submission_id)])? {return Ok(());}
            s.goal_invalidate(root)?;
            s.execute("UPDATE goal_turns SET finalized=1,outcome='input_superseded' WHERE root=? AND finalized=0", &[json!(root)])?;
            s.execute("UPDATE goal_roots SET input_epoch=input_epoch+1,completed_visible=0,report_barrier=CASE WHEN ? THEN 0 ELSE report_barrier END,pending_input=? WHERE root=?", &[json!(renewed),json!(submission_id),json!(root)])?;
            s.execute("UPDATE goals SET revision=revision+1,state=CASE WHEN ? AND state='needs_input' THEN 'active' ELSE state END,no_progress=CASE WHEN ? THEN 0 ELSE no_progress END,error=CASE WHEN ? THEN NULL ELSE error END,updated=? WHERE root=? AND current=1", &[json!(renewed),json!(renewed),json!(renewed),json!(now),json!(root)])?;
            s.execute("INSERT INTO goal_inputs(root,submission_id,input_epoch,state,created,updated) SELECT root,?,input_epoch,'submitting',?,? FROM goal_roots WHERE root=?",&[json!(submission_id),json!(now),json!(now),json!(root)])?;
            Ok(())
        })?;
        self.goal_publish(root)
    }

    pub fn goal_input_settle(
        &mut self,
        root: &str,
        submission_id: &str,
        turn: Option<&str>,
        outcome: &str,
        now: f64,
    ) -> Result<Value> {
        ensure!(
            matches!(outcome, "accepted" | "failed" | "uncertain"),
            "invalid user input outcome"
        );
        ensure!(
            outcome != "accepted" || turn.is_some_and(|t| !t.is_empty() && t.len() <= 256),
            "accepted user input requires its exact turn"
        );
        self.transaction(true,|s| {
            let input=s.first("SELECT * FROM goal_inputs WHERE root=? AND submission_id=?",&[json!(root),json!(submission_id)])?.ok_or_else(||anyhow::anyhow!("unknown user input submission"))?;
            if input["state"]=="accepted" || input["state"]=="failed" {
                ensure!(input["state"]==outcome && (turn.is_none() || input["turn"]==json!(turn)),"user input already has a different terminal outcome");
                return Ok(());
            }
            s.execute("UPDATE goal_inputs SET state=?,turn=COALESCE(turn,?),updated=? WHERE root=? AND submission_id=?",&[json!(outcome),json!(turn),json!(now),json!(root),json!(submission_id)])?;
            if outcome=="uncertain" {
                s.execute("UPDATE goals SET state='needs_input',error='User input admission is uncertain',updated=? WHERE root=? AND current=1 AND state IN ('active','waiting') AND EXISTS(SELECT 1 FROM goal_roots WHERE root=? AND pending_input=?)",&[json!(now),json!(root),json!(root),json!(submission_id)])?;
            } else {
                s.execute("UPDATE goal_roots SET pending_input=NULL WHERE root=? AND pending_input=?",&[json!(root),json!(submission_id)])?;
            }
            Ok(())
        })?;
        self.goal_publish(root)
    }

    pub fn goal_admit(
        &mut self,
        root: &str,
        id: &str,
        revision: i64,
        turn: &str,
        input_epoch: i64,
        now: f64,
    ) -> Result<Value> {
        ensure!(
            !turn.is_empty() && turn.len() <= 256,
            "turn identity required and must not exceed 256 bytes"
        );
        self.transaction(true, |s| {
            let goal=s.goal_expected(root,id,revision)?;
            let meta=s.first("SELECT * FROM goal_roots WHERE root=?", &[json!(root)])?.unwrap();
            ensure!(goal["state"]=="active" && meta["input_epoch"]==input_epoch && meta["report_barrier"]==0 && meta["pending_input"].is_null(),"input epoch, state, pending user submission or recovery barrier prevents admission");
            if let Some(old)=s.first("SELECT * FROM goal_turns WHERE root=? AND turn=? AND input_epoch=?", &[json!(root),json!(turn),json!(input_epoch)])? {
                ensure!(old["goal"]==id && old["revision"]==revision && old["finalized"]==0,"turn already admitted or finalized");
                return Ok(());
            }
            ensure!(!s.exists("SELECT 1 FROM goal_turns WHERE root=? AND finalized=0", &[json!(root)])?,"another goal turn is unresolved");
            s.execute("INSERT INTO goal_turns(root,goal,revision,turn,input_epoch,created) VALUES (?,?,?,?,?,?)", &[json!(root),json!(id),json!(revision),json!(turn),json!(input_epoch),json!(now)])
        })?;
        self.goal_publish(root)
    }

    pub fn goal_checkpoint(
        &mut self,
        root: &str,
        id: &str,
        revision: i64,
        turn: &str,
        input_epoch: i64,
        disposition: &Value,
        now: f64,
    ) -> Result<Value> {
        ensure!(
            crate::local::ascii_json(disposition)?.len() <= 16384,
            "checkpoint exceeds 16384 bytes"
        );
        self.transaction(true, |s| {
            let goal=s.goal_expected(root,id,revision)?;
            let meta=s.first("SELECT * FROM goal_roots WHERE root=?", &[json!(root)])?.unwrap();
            ensure!(meta["input_epoch"]==input_epoch && meta["report_barrier"]==0 && goal["state"]=="active","stale input or inactive goal");
            ensure!(s.exists("SELECT 1 FROM goal_turns WHERE root=? AND goal=? AND revision=? AND turn=? AND input_epoch=? AND finalized=0", &[json!(root),json!(id),json!(revision),json!(turn),json!(input_epoch)])?,"checkpoint requires exact admitted goal turn");
            let kind=required(disposition,"disposition")?;
            ensure!(matches!(kind,"continue"|"wait"|"needs_input"|"complete"),"invalid disposition");
            if let Some(refs)=disposition.get("evidence") {ensure!(refs.as_array().is_some_and(|a|a.iter().all(|r|r.as_str().is_some_and(|t|!t.trim().is_empty()))),"evidence must be stable nonempty references");}
            match kind {
                "continue"=>{required(disposition,"next_action")?;},
                "needs_input"=>{required(disposition,"reason")?;},
                "complete"=>ensure!(disposition["evidence"].as_array().is_some_and(|a|!a.is_empty()),"completion evidence references required"),
                "wait"=>{
                    let jobs=disposition["jobs"].as_array().ok_or_else(||anyhow::anyhow!("exact job IDs required"))?;
                    ensure!(!jobs.is_empty(),"wait requires jobs");
                    let mut distinct=HashSet::new();
                    for job in jobs {
                        let job=job.as_str().ok_or_else(||anyhow::anyhow!("job ID must be text"))?;
                        ensure!(distinct.insert(job),"duplicate wait job");
                        ensure!(s.exists("SELECT 1 FROM jobs JOIN parks ON parks.id=jobs.park JOIN goal_jobs ON goal_jobs.job=jobs.id JOIN callbacks ON callbacks.id=jobs.callback WHERE jobs.id=? AND parks.parent=? AND goal_jobs.goal=? AND callbacks.state IN ('goal_owned','goal_settled') AND parks.state IN ('armed','complete') AND jobs.status IN ('armed','completed','failed','interrupted')", &[json!(job),json!(root),json!(id)])?,"wait job must already belong to this root and goal, with a goal-owned callback marker");
                        ensure!(!s.exists("SELECT 1 FROM goal_jobs WHERE job=? AND goal<>?", &[json!(job),json!(id)])?,"job belongs to another goal");
                    }
                },_=>unreachable!(),
            }
            s.execute("UPDATE goal_turns SET disposition=? WHERE root=? AND turn=? AND input_epoch=?", &[json!(disposition.to_string()),json!(root),json!(turn),json!(input_epoch)])?;
            s.goal_event(&goal,"checkpoint_staged",disposition,now)
        })?;
        self.goal_publish(root)
    }

    pub fn goal_finalize(
        &mut self,
        root: &str,
        turn: &str,
        succeeded: bool,
        now: f64,
    ) -> Result<Value> {
        self.transaction(true, |s| {
            s.goal_reconcile_accepted_inner(root,now)?;
            s.execute("UPDATE goal_dispatches SET state='done' WHERE root=? AND accepted_turn=? AND state IN ('submitting','pending','starting','accepted','uncertain')", &[json!(root),json!(turn)])?;
            for admitted in s.goal_turns(root)?.into_iter().filter(|t|t["turn"]==turn) {
                s.execute("UPDATE goal_turns SET finalized=1,outcome=? WHERE root=? AND turn=? AND input_epoch=?", &[json!(if succeeded {"completed"} else {"failed"}),json!(root),json!(turn),admitted["input_epoch"].clone()])?;
                let Some(goal)=s.goal_current(root)? else {continue};
                let meta=s.first("SELECT * FROM goal_roots WHERE root=?", &[json!(root)])?.unwrap();
                if goal["id"]!=admitted["goal"] || goal["revision"]!=admitted["revision"] || goal["state"]!="active" || meta["input_epoch"]!=admitted["input_epoch"] {continue;}
                s.goal_invalidate(root)?;
                let disposition:Option<Value>=admitted["disposition"].as_str().map(serde_json::from_str).transpose()?;
                let mut state="needs_input";
                let mut error=Some(if succeeded {"Goal turn ended without a checkpoint"} else {"Goal turn failed or was interrupted"}.to_owned());
                let mut no_progress=goal["no_progress"].as_i64().unwrap();
                if succeeded && meta["report_barrier"]==0 {
                    if let Some(d)=&disposition {
                        if d["disposition"]=="continue" {
                            let mut progress=false;
                            for reference in d["evidence"].as_array().into_iter().flatten() {
                                if !s.exists("SELECT 1 FROM goal_evidence WHERE goal=? AND reference=?", &[goal["id"].clone(),reference.clone()])? {progress=true;}
                                s.execute("INSERT OR IGNORE INTO goal_evidence(goal,reference) VALUES (?,?)", &[goal["id"].clone(),reference.clone()])?;
                            }
                            no_progress=if progress {0} else {no_progress+1};
                        }
                        state=match d["disposition"].as_str().unwrap() {"continue"=>"active","wait"=>"waiting","complete"=>"complete",_=>"needs_input"};
                        error=d["reason"].as_str().map(str::to_owned);
                        if no_progress>=2 && d["disposition"]=="continue" {state="needs_input";error=Some("Two goal turns supplied no new evidence references".into());}
                        if d["disposition"]=="wait" {
                            for job in d["jobs"].as_array().unwrap() {s.execute("INSERT OR IGNORE INTO goal_jobs(goal,job,turn,input_epoch) VALUES (?,?,?,?)", &[goal["id"].clone(),job.clone(),json!(turn),admitted["input_epoch"].clone()])?;}
                            if state=="waiting" {
                                match s.goal_running_wait(&goal["id"],d)? {
                                    Some(false)=>state="active",Some(true)=>{},None=>{state="needs_input";error=Some("Wait has no remaining work or a registration/outcome is unknown".into());},
                                }
                            }
                        }
                    }
                }
                s.execute("UPDATE goals SET revision=revision+1,state=?,checkpoint=?,no_progress=?,error=?,updated=? WHERE id=?", &[json!(state),disposition.as_ref().map(|d|json!(d.to_string())).unwrap_or(Value::Null),json!(no_progress),json!(error),json!(now),goal["id"].clone()])?;
                if state=="complete" {s.execute("UPDATE goal_roots SET completed_visible=1 WHERE root=?",&[json!(root)])?;}
                s.goal_event(&goal,"turn_finalized",&json!({"turn":turn,"succeeded":succeeded,"state":state}),now)?;
            }
            Ok(())
        })?;
        self.goal_publish(root)
    }

    fn goal_bind_accepted(&mut self, dispatch: &Value, now: f64) -> Result<bool> {
        if dispatch["kind"] == "report" || dispatch["accepted_turn"].is_null() {
            return Ok(false);
        }
        let root = dispatch["root"].as_str().unwrap();
        if dispatch["kind"] == "completion" {
            self.execute("UPDATE goal_jobs SET notified=1 WHERE goal=? AND job IN (SELECT job FROM goal_dispatch_jobs WHERE dispatch=?)",&[dispatch["goal"].clone(),dispatch["id"].clone()])?;
        }
        let Some(goal) = self.goal_current(root)? else {
            return Ok(false);
        };
        let meta = self
            .first("SELECT * FROM goal_roots WHERE root=?", &[json!(root)])?
            .unwrap();
        if goal["id"] != dispatch["goal"]
            || goal["revision"] != dispatch["revision"]
            || meta["input_epoch"] != dispatch["input_epoch"]
            || goal["state"] != "active"
            || meta["report_barrier"] != 0
            || !meta["pending_input"].is_null()
        {
            return Ok(false);
        }
        let args = [
            json!(root),
            dispatch["accepted_turn"].clone(),
            dispatch["input_epoch"].clone(),
        ];
        if let Some(existing) = self.first(
            "SELECT goal,revision FROM goal_turns WHERE root=? AND turn=? AND input_epoch=?",
            &args,
        )? {
            if existing["goal"] != dispatch["goal"] || existing["revision"] != dispatch["revision"]
            {
                self.execute("UPDATE goals SET state='needs_input',error='Accepted goal turn conflicts with an existing admission',updated=? WHERE id=?",&[json!(now),goal["id"].clone()])?;
            }
            return Ok(false);
        }
        self.execute("INSERT INTO goal_turns(root,goal,revision,turn,input_epoch,created) VALUES (?,?,?,?,?,?)",&[json!(root),dispatch["goal"].clone(),dispatch["revision"].clone(),dispatch["accepted_turn"].clone(),dispatch["input_epoch"].clone(),json!(now)])?;
        Ok(true)
    }

    fn goal_reconcile_accepted_inner(&mut self, root: &str, now: f64) -> Result<bool> {
        let dispatches=self.rows("SELECT d.* FROM goals g JOIN goal_roots r ON r.root=g.root JOIN goal_dispatches d ON d.root=g.root AND d.goal=g.id AND d.revision=g.revision AND d.input_epoch=r.input_epoch WHERE g.root=? AND g.current=1 AND g.state='active' AND r.report_barrier=0 AND r.pending_input IS NULL AND d.accepted_turn IS NOT NULL AND d.kind<>'report' AND d.state NOT IN ('ready','invalidated','failed') AND NOT EXISTS(SELECT 1 FROM goal_turns t WHERE t.root=d.root AND t.turn=d.accepted_turn AND t.input_epoch=d.input_epoch) ORDER BY d.created,d.id",&[json!(root)])?;
        let mut changed = false;
        for dispatch in dispatches {
            changed = self.goal_bind_accepted(&dispatch, now)? || changed;
        }
        Ok(changed)
    }

    pub fn goal_reconcile_accepted(&mut self, root: &str, now: f64) -> Result<Value> {
        let changed = self.transaction(true, |s| s.goal_reconcile_accepted_inner(root, now))?;
        if changed {
            self.goal_publish(root)
        } else {
            self.goal_status(root)
        }
    }

    pub fn goal_accept_dispatch(
        &mut self,
        id: &str,
        queue: Option<&str>,
        turn: &str,
        now: f64,
    ) -> Result<Value> {
        self.goal_dispatch_result(id, "accepted", queue, Some(turn), None, now)
    }

    fn goal_running_wait(&mut self, id: &Value, checkpoint: &Value) -> Result<Option<bool>> {
        let mut running = false;
        let mut pending = false;
        for job in checkpoint["jobs"].as_array().into_iter().flatten() {
            let row=self.first("SELECT jobs.status,parks.state,goal_jobs.notified FROM goal_jobs JOIN jobs ON jobs.id=goal_jobs.job JOIN parks ON parks.id=jobs.park WHERE goal_jobs.goal=? AND jobs.id=?", &[id.clone(),job.clone()])?;
            let Some(row) = row else { return Ok(None) };
            if !matches!(row["state"].as_str(), Some("armed" | "complete")) {
                return Ok(None);
            }
            match row["status"].as_str() {
                Some("armed") => running = true,
                Some("completed" | "failed" | "interrupted") => {
                    pending |= row["notified"] == 0;
                }
                _ => return Ok(None),
            }
        }
        Ok(if pending {
            Some(false)
        } else if running {
            Some(true)
        } else {
            None
        })
    }

    pub fn goal_wait_ready(&mut self, root: &str, now: f64) -> Result<bool> {
        let (ready,changed)=self.transaction(true, |s| {
            let Some(goal)=s.goal_current(root)? else {return Ok((false,false))};
            if goal["state"]!="waiting" {return Ok((false,false));}
            match s.goal_running_wait(&goal["id"],&goal["checkpoint"])? {
                Some(true)=>Ok((false,false)),
                Some(false)=>{s.execute("UPDATE goals SET state='active',updated=? WHERE id=?", &[json!(now),goal["id"].clone()])?;Ok((true,true))},
                None=>{s.execute("UPDATE goals SET state='needs_input',revision=revision+1,error='Wait has no remaining work or a registration/outcome is unknown',updated=? WHERE id=?", &[json!(now),goal["id"].clone()])?;Ok((false,true))},
            }
        })?;
        if changed {
            self.goal_publish(root)?;
        }
        Ok(ready)
    }

    pub fn goal_intent(
        &mut self,
        root: &str,
        expected: &Value,
        kind: &str,
        message: &str,
        now: f64,
    ) -> Result<Option<Value>> {
        ensure!(
            matches!(kind, "continue" | "completion" | "review" | "report"),
            "unknown goal dispatch kind"
        );
        ensure!(
            !message.trim().is_empty() && message.len() <= 65536,
            "dispatch message required and must not exceed 65536 bytes"
        );
        let result=self.transaction(true, |s| {
            let Some(goal)=s.goal_current(root)? else {return Ok(None)};
            let meta=s.first("SELECT * FROM goal_roots WHERE root=?", &[json!(root)])?.unwrap();
            if expected["root"]!=root || expected["goal"]["id"]!=goal["id"] || expected["goal"]["revision"]!=goal["revision"] || expected["input_epoch"]!=meta["input_epoch"] || (kind=="report" && expected["report_id"]!=meta["report_id"]) {return Ok(None);}
            if !meta["pending_input"].is_null() {return Ok(None);}
            let report=kind=="report";
            if if report {meta["report_barrier"]!=1 || meta["report_delivered"]!=0 || !matches!(goal["state"].as_str(),Some("active"|"waiting"|"needs_input"))} else {goal["state"]!="active" || meta["report_barrier"]!=0} {return Ok(None);}
            if s.exists(&format!("SELECT 1 FROM goal_dispatches WHERE root=? AND {OPEN}"), &[json!(root)])? || (!report && s.exists("SELECT 1 FROM goal_turns WHERE root=? AND finalized=0", &[json!(root)])?) {return Ok(None);}
            if kind=="review" && (goal["review_seconds"].is_null() || goal["review_due"].as_f64().unwrap_or(f64::INFINITY)>now) {return Ok(None);}
            let mut completion_jobs=Vec::new();
            if kind=="completion" {
                let Some(jobs)=expected["completion_jobs"].as_array().filter(|jobs|!jobs.is_empty() && jobs.len()<=20) else {return Ok(None)};
                if s.goal_running_wait(&goal["id"],&goal["checkpoint"])?.is_none() {
                    s.execute("UPDATE goals SET state='needs_input',revision=revision+1,error='Wait has no remaining work or a registration/outcome is unknown',updated=? WHERE id=?",&[json!(now),goal["id"].clone()])?;
                    return Ok(None);
                }
                let mut distinct=HashSet::new();
                for job in jobs {
                    let Some(id)=job.as_str() else {return Ok(None)};
                    if !distinct.insert(id) || !s.exists("SELECT 1 FROM goal_jobs g JOIN jobs j ON j.id=g.job JOIN parks p ON p.id=j.park WHERE g.goal=? AND g.job=? AND g.notified=0 AND j.status IN ('completed','failed','interrupted') AND p.state IN ('armed','complete') AND j.id IN (SELECT value FROM json_each(?))",&[goal["id"].clone(),json!(id),json!(goal["checkpoint"]["jobs"].to_string())])? {return Ok(None);}
                    completion_jobs.push(json!(id));
                }
            }
            if !report && goal["review_seconds"].is_number() {s.execute("UPDATE goals SET review_due=? WHERE id=?", &[json!(now+goal["review_seconds"].as_f64().unwrap()),goal["id"].clone()])?;}
            let id=Uuid::new_v4().to_string();
            s.execute("INSERT INTO goal_dispatches(id,root,goal,revision,input_epoch,client,message,kind,state,created) VALUES (?,?,?,?,?,?,?,?,'ready',?)", &[json!(id),json!(root),goal["id"].clone(),goal["revision"].clone(),meta["input_epoch"].clone(),json!(Uuid::new_v4().to_string()),json!(message),json!(kind),json!(now)])?;
            if report {s.execute("UPDATE goal_dispatches SET report_id=? WHERE id=?", &[meta["report_id"].clone(),json!(id)])?;}
            for job in completion_jobs {s.execute("INSERT INTO goal_dispatch_jobs(dispatch,job) VALUES (?,?)",&[json!(id),job])?;}
            let dispatch=s.first("SELECT * FROM goal_dispatches WHERE id=?", &[json!(id)])?.unwrap();
            Ok(Some(s.goal_dispatch_view(dispatch)?))
        })?;
        self.goal_publish(root)?;
        Ok(result)
    }

    pub fn goal_report_intent(
        &mut self,
        root: &str,
        expected: &Value,
        message: &str,
        now: f64,
    ) -> Result<Option<Value>> {
        self.goal_intent(root, expected, "report", message, now)
    }

    #[cfg(test)]
    fn test_goal_intent(
        &mut self,
        root: &str,
        kind: &str,
        message: &str,
        now: f64,
    ) -> Result<Option<Value>> {
        let expected = self.goal_status(root)?;
        self.goal_intent(root, &expected, kind, message, now)
    }

    #[cfg(test)]
    fn test_goal_report_intent(
        &mut self,
        root: &str,
        message: &str,
        now: f64,
    ) -> Result<Option<Value>> {
        let expected = self.goal_status(root)?;
        self.goal_report_intent(root, &expected, message, now)
    }

    pub fn goal_claim_dispatch(&mut self, id: &str) -> Result<Option<Value>> {
        self.transaction(true, |s| {
            let Some(dispatch) = s.first(
                "SELECT * FROM goal_dispatches WHERE id=? AND state='ready'",
                &[json!(id)],
            )?
            else {
                return Ok(None);
            };
            let root = dispatch["root"].as_str().unwrap();
            let current = s.goal_current(root)?;
            let meta = s
                .first("SELECT * FROM goal_roots WHERE root=?", &[json!(root)])?
                .unwrap();
            if !meta["pending_input"].is_null() {
                return Ok(None);
            }
            if dispatch["kind"] != "report"
                && s.exists(
                    "SELECT 1 FROM goal_turns WHERE root=? AND finalized=0",
                    &[json!(root)],
                )?
            {
                return Ok(None);
            }
            let valid = if dispatch["kind"] == "report" {
                meta["report_barrier"] == 1
                    && meta["report_delivered"] == 0
                    && meta["report_id"] == dispatch["report_id"]
                    && meta["input_epoch"] == dispatch["input_epoch"]
                    && current.is_some_and(|g| {
                        g["id"] == dispatch["goal"]
                            && g["revision"] == dispatch["revision"]
                            && matches!(
                                g["state"].as_str(),
                                Some("active" | "waiting" | "needs_input")
                            )
                    })
            } else {
                current.is_some_and(|g| {
                    g["id"] == dispatch["goal"]
                        && g["revision"] == dispatch["revision"]
                        && g["state"] == "active"
                }) && meta["input_epoch"] == dispatch["input_epoch"]
                    && meta["report_barrier"] == 0
            };
            if !valid {
                s.execute(
                    "UPDATE goal_dispatches SET state='invalidated' WHERE id=?",
                    &[json!(id)],
                )?;
                return Ok(None);
            }
            s.execute(
                "UPDATE goal_dispatches SET state='submitting' WHERE id=?",
                &[json!(id)],
            )?;
            let dispatch = s
                .first("SELECT * FROM goal_dispatches WHERE id=?", &[json!(id)])?
                .unwrap();
            Ok(Some(s.goal_dispatch_view(dispatch)?))
        })
    }

    pub fn goal_dispatches(&mut self, root: &str) -> Result<Vec<Value>> {
        let rows = self.rows(
            &format!("SELECT * FROM goal_dispatches WHERE root=? AND {OPEN} ORDER BY created,id"),
            &[json!(root)],
        )?;
        rows.into_iter()
            .map(|dispatch| self.goal_dispatch_view(dispatch))
            .collect()
    }

    pub fn goal_dispatch_result(
        &mut self,
        id: &str,
        state: &str,
        queue: Option<&str>,
        accepted_turn: Option<&str>,
        error: Option<&str>,
        now: f64,
    ) -> Result<Value> {
        ensure!(
            matches!(
                state,
                "pending" | "starting" | "accepted" | "uncertain" | "done" | "failed"
            ),
            "invalid dispatch result state"
        );
        ensure!(
            crate::local::ascii_json(&json!(error))?.len() <= 4096,
            "dispatch error exceeds 4096 bytes"
        );
        ensure!(
            queue.is_none_or(|q| q.len() <= 512)
                && accepted_turn.is_none_or(|t| !t.is_empty() && t.len() <= 256),
            "dispatch queue or turn identity exceeds bound"
        );
        let result=self.transaction(true, |s| {
            let dispatch=s.first("SELECT * FROM goal_dispatches WHERE id=?", &[json!(id)])?.ok_or_else(||anyhow::anyhow!("unknown goal dispatch"))?;
            ensure!(dispatch["state"]!="ready" && dispatch["state"]!="invalidated","dispatch was not submitted");
            if matches!(dispatch["state"].as_str(),Some("done"|"failed")) {return Ok(dispatch);}
            if let Some(q)=queue {ensure!(dispatch["queue"].is_null() || dispatch["queue"]==q,"queue identity changed");}
            if let Some(t)=accepted_turn {ensure!(dispatch["accepted_turn"].is_null() || dispatch["accepted_turn"]==t,"accepted turn identity changed");}
            s.execute("UPDATE goal_dispatches SET state=?,queue=COALESCE(queue,?),accepted_turn=COALESCE(accepted_turn,?),error=? WHERE id=?", &[json!(state),json!(queue),json!(accepted_turn),json!(error),json!(id)])?;
            if matches!(state,"pending"|"starting") && dispatch["state"]!=state {
                s.execute("UPDATE goal_dispatches SET settlement_started=? WHERE id=?",&[json!(now),json!(id)])?;
            }
            if state=="accepted" && dispatch["kind"]=="report" {s.execute("UPDATE goal_roots SET report_delivered=1 WHERE root=? AND report_id=?", &[dispatch["root"].clone(),dispatch["report_id"].clone()])?;}
            if state=="uncertain" {s.execute("UPDATE goals SET state='needs_input',error=?,updated=? WHERE id=? AND current=1 AND revision=? AND state IN ('active','waiting')", &[json!(error.unwrap_or("Goal delivery outcome is uncertain")),json!(now),dispatch["goal"].clone(),dispatch["revision"].clone()])?;}
            let accepted=s.first("SELECT * FROM goal_dispatches WHERE id=?", &[json!(id)])?.unwrap();
            if state=="accepted" {s.goal_bind_accepted(&accepted,now)?;}
            s.goal_dispatch_view(accepted)
        })?;
        self.goal_publish(result["root"].as_str().unwrap())?;
        Ok(result)
    }

    pub fn goal_recovery(
        &mut self,
        root: &str,
        report_id: &str,
        reason: &str,
        now: f64,
    ) -> Result<Value> {
        ensure!(
            !report_id.is_empty()
                && crate::local::ascii_json(&json!(report_id))?.len() <= 512
                && !reason.is_empty()
                && crate::local::ascii_json(&json!(reason))?.len() <= 4096,
            "report identity or reason is empty or too long"
        );
        self.transaction(true, |s| {
            s.goal_root(root)?;
            let meta=s.first("SELECT * FROM goal_roots WHERE root=?", &[json!(root)])?.unwrap();
            // Restarting again cannot repeat the report or manufacture renewed direction.
            if meta["report_id"]==report_id || meta["report_barrier"]==1 {return Ok(());}
            s.execute("UPDATE goal_roots SET report_barrier=1,report_id=?,report_delivered=0,input_epoch=input_epoch+1 WHERE root=?", &[json!(report_id),json!(root)])?;
            s.goal_invalidate(root)?;
            s.execute("UPDATE goals SET state=CASE WHEN state='paused' THEN state ELSE 'needs_input' END,revision=revision+1,error=?,updated=? WHERE root=? AND current=1 AND state NOT IN ('complete','cleared','replaced')", &[json!(reason),json!(now),json!(root)])
        })?;
        self.goal_publish(root)
    }

    pub fn goal_report_delivered(&mut self, root: &str, report_id: &str) -> Result<Value> {
        self.execute(
            "UPDATE goal_roots SET report_delivered=1 WHERE root=? AND report_id=?",
            &[json!(root), json!(report_id)],
        )?;
        self.goal_publish(root)
    }

    pub fn goal_model_mutation_blocked(&mut self, root: &str) -> Result<bool> {
        // Recovery revokes old turn authority before a report is prepared or visible.
        self.exists("SELECT 1 FROM goal_roots r WHERE r.root=? AND r.report_barrier=1 AND NOT EXISTS(SELECT 1 FROM goal_inputs i WHERE i.root=r.root AND i.state='accepted' AND i.input_epoch=r.input_epoch) LIMIT 1",&[json!(root)])
    }

    pub fn goal_report_jobs(&mut self, root: &str) -> Result<Vec<Value>> {
        self.rows("SELECT goal_jobs.goal,jobs.* FROM goal_jobs JOIN jobs ON jobs.id=goal_jobs.job JOIN goals ON goals.id=goal_jobs.goal WHERE goals.root=? AND jobs.report=1 AND jobs.status IN ('completed','failed','interrupted','lost') AND NOT EXISTS(SELECT 1 FROM goal_job_reports WHERE goal_job_reports.job=jobs.id) ORDER BY jobs.id", &[json!(root)])
    }

    pub fn goal_report_results(&mut self, root: &str, report_id: &str) -> Result<Value> {
        let total=self.first("SELECT COUNT(*) AS count FROM goal_job_reports JOIN jobs ON jobs.id=goal_job_reports.job JOIN goal_jobs ON goal_jobs.job=jobs.id JOIN goals ON goals.id=goal_jobs.goal WHERE goals.root=? AND goal_job_reports.report_id=?",&[json!(root),json!(report_id)])?.unwrap()["count"].clone();
        let mut results=self.rows("SELECT goal_jobs.goal,jobs.id,jobs.kind,jobs.thread,jobs.turn,jobs.item,jobs.execution,jobs.status,jobs.exit_code FROM goal_job_reports JOIN jobs ON jobs.id=goal_job_reports.job JOIN goal_jobs ON goal_jobs.job=jobs.id JOIN goals ON goals.id=goal_jobs.goal WHERE goals.root=? AND goal_job_reports.report_id=? ORDER BY jobs.id LIMIT 20",&[json!(root),json!(report_id)])?;
        crate::ledger::add_execution_refs(&self.directory, root, &mut results);
        Ok(json!({"report_id":report_id,"total":total,"results":results}))
    }

    pub fn goal_reconcile_reports(&mut self, root: &str, now: f64) -> Result<Value> {
        let changed=self.transaction(true, |s| {
            let jobs=s.goal_report_jobs(root)?;
            if jobs.is_empty() {return Ok(false);}
            s.goal_root(root)?;
            let meta=s.first("SELECT * FROM goal_roots WHERE root=?",&[json!(root)])?.unwrap();
            let current=s.goal_current(root)?;
            let eligible=current.is_some_and(|g| matches!(g["state"].as_str(),Some("active"|"waiting"|"needs_input")) && jobs.iter().any(|job|job["goal"]==g["id"]));
            let report_id=if meta["report_barrier"]==1 {meta["report_id"].as_str().unwrap().to_owned()} else {Uuid::new_v4().to_string()};
            for job in jobs {s.execute("INSERT INTO goal_job_reports(job,report_id) VALUES (?,?)",&[job["id"].clone(),json!(report_id)])?;}
            if eligible && meta["report_barrier"]!=1 {
                s.execute("UPDATE goal_roots SET report_barrier=1,report_id=?,report_delivered=0,input_epoch=input_epoch+1 WHERE root=?",&[json!(report_id),json!(root)])?;
                s.goal_invalidate(root)?;
                s.execute("UPDATE goals SET state='needs_input',revision=revision+1,error='Recovered goal results are report only; renewed user direction is required',updated=? WHERE root=? AND current=1 AND state IN ('active','waiting','needs_input')",&[json!(now),json!(root)])?;
            }
            Ok(true)
        })?;
        if changed {
            self.goal_publish(root)
        } else {
            self.goal_status(root)
        }
    }

    pub fn goal_set_error(&mut self, root: &str, reason: &str, now: f64) -> Result<Value> {
        ensure!(
            crate::local::ascii_json(&json!(reason))?.len() <= 4096,
            "goal error exceeds 4096 encoded bytes"
        );
        self.transaction(true, |s| {
            s.goal_invalidate(root)?;
            s.execute("UPDATE goals SET state='needs_input',revision=revision+1,error=?,updated=? WHERE root=? AND current=1 AND state IN ('active','waiting')", &[json!(reason),json!(now),json!(root)])
        })?;
        self.goal_publish(root)
    }

    fn goal_retire_uncertain(&mut self, root: &str) -> Result<()> {
        self.execute("UPDATE goal_turns SET finalized=1,outcome='uncertain_superseded' WHERE root=? AND finalized=0 AND EXISTS(SELECT 1 FROM goal_turn_issues i WHERE i.root=goal_turns.root AND i.turn=goal_turns.turn AND i.input_epoch=goal_turns.input_epoch)",&[json!(root)])
    }

    fn goal_turn_issue(&mut self, admitted: &Value, reason: &str, now: f64) -> Result<bool> {
        let root = admitted["root"].as_str().unwrap();
        let args = [
            json!(root),
            admitted["turn"].clone(),
            admitted["input_epoch"].clone(),
        ];
        if self.exists(
            "SELECT 1 FROM goal_turn_issues WHERE root=? AND turn=? AND input_epoch=?",
            &args,
        )? {
            return Ok(false);
        }
        self.execute(
            "INSERT INTO goal_turn_issues(root,turn,input_epoch,created) VALUES (?,?,?,?)",
            &[
                args[0].clone(),
                args[1].clone(),
                args[2].clone(),
                json!(now),
            ],
        )?;
        self.goal_event(
            &json!({"id":admitted["goal"],"revision":admitted["revision"]}),
            "turn_uncertain",
            &json!({"turn":admitted["turn"],"input_epoch":admitted["input_epoch"],"reason":reason}),
            now,
        )?;
        if let Some(goal) = self.goal_current(root)? {
            let meta = self
                .first(
                    "SELECT input_epoch FROM goal_roots WHERE root=?",
                    &[json!(root)],
                )?
                .unwrap();
            if goal["id"] == admitted["goal"]
                && goal["revision"] == admitted["revision"]
                && meta["input_epoch"] == admitted["input_epoch"]
                && matches!(goal["state"].as_str(), Some("active" | "waiting"))
            {
                self.goal_invalidate(root)?;
                self.execute("UPDATE goals SET state='needs_input',revision=revision+1,error=?,updated=? WHERE id=?",&[json!(reason),json!(now),goal["id"].clone()])?;
            }
            let renewed=goal["id"]!=admitted["goal"] || self.exists("SELECT 1 FROM goal_events WHERE goal=? AND event='resume' AND revision>=? LIMIT 1",&[admitted["goal"].clone(),admitted["revision"].clone()])?;
            if renewed {
                self.execute("UPDATE goal_turns SET finalized=1,outcome='uncertain_superseded' WHERE root=? AND turn=? AND input_epoch=? AND finalized=0",&args)?;
            }
        }
        Ok(true)
    }

    pub fn goal_turn_uncertain(
        &mut self,
        root: &str,
        turn: &str,
        reason: &str,
        now: f64,
    ) -> Result<Value> {
        ensure!(
            !reason.is_empty() && crate::local::ascii_json(&json!(reason))?.len() <= 4096,
            "turn uncertainty reason is empty or too long"
        );
        let changed = self.transaction(true, |s| {
            let turns = s.rows(
                "SELECT * FROM goal_turns WHERE root=? AND turn=? AND finalized=0",
                &[json!(root), json!(turn)],
            )?;
            let mut changed = false;
            for admitted in turns {
                changed = s.goal_turn_issue(&admitted, reason, now)? || changed;
            }
            Ok(changed)
        })?;
        if changed {
            self.goal_publish(root)
        } else {
            self.goal_status(root)
        }
    }

    pub fn goal_origin_interrupted(
        &mut self,
        root: &str,
        park_id: &str,
        now: f64,
    ) -> Result<Value> {
        let changed=self.transaction(true,|s| {
            ensure!(s.exists("SELECT 1 FROM parks WHERE id=? AND parent=?",&[json!(park_id),json!(root)])?,"park is not owned by this root");
            let turns=s.rows("SELECT DISTINCT goal_turns.* FROM parks JOIN jobs ON jobs.park=parks.id JOIN goal_jobs ON goal_jobs.job=jobs.id JOIN goal_turns ON goal_turns.root=parks.parent AND goal_turns.goal=goal_jobs.goal AND goal_turns.turn=goal_jobs.turn AND goal_turns.input_epoch=goal_jobs.input_epoch WHERE parks.id=? AND parks.parent=?",&[json!(park_id),json!(root)])?;
            let mut changed=false;
            for admitted in turns {changed=s.goal_turn_issue(&admitted,"Goal origin interrupted; running jobs remain observed and require reassessment.",now)? || changed;}
            Ok(changed)
        })?;
        if changed {
            self.goal_publish(root)
        } else {
            self.goal_status(root)
        }
    }

    pub fn goal_provenance(
        &mut self,
        root: &str,
        provenance: Option<&Value>,
    ) -> Result<Option<Value>> {
        if let Some(value) = provenance {
            self.execute("INSERT INTO goal_provenance(root,value) VALUES (?,?) ON CONFLICT(root) DO UPDATE SET value=excluded.value", &[json!(root),json!(value.to_string())])?;
        }
        self.first(
            "SELECT value FROM goal_provenance WHERE root=?",
            &[json!(root)],
        )?
        .map(|r| Ok(serde_json::from_str(r["value"].as_str().unwrap())?))
        .transpose()
    }

    pub(crate) fn goal_bind_jobs(
        &mut self,
        root: &str,
        origin: &str,
        jobs: &[&crate::ledger::ResolvedJob],
    ) -> Result<()> {
        let turn=self.first("SELECT * FROM goal_turns WHERE root=? AND turn=? AND finalized=0 ORDER BY input_epoch DESC LIMIT 1", &[json!(root),json!(origin)])?;
        let mut bindings = Vec::new();
        for job in jobs {
            let binding = if let Some(execution) = &job.execution {
                let home = self
                    .directory
                    .parent()
                    .ok_or_else(|| anyhow::anyhow!("state directory has no home"))?;
                let record = crate::executions::read(home, root, execution)?;
                let saved = &record["origin"];
                if !saved["goal"].is_object() {
                    continue;
                }
                ensure!(
                    saved["turn"] == origin,
                    "execution origin differs from its watch"
                );
                Some(self.first("SELECT * FROM goal_turns WHERE root=? AND goal=? AND revision=? AND turn=? AND input_epoch=?", &[json!(root),saved["goal"]["id"].clone(),saved["goal"]["revision"].clone(),saved["turn"].clone(),saved["input_epoch"].clone()])?.ok_or_else(||anyhow::anyhow!("execution goal origin has no exact admitted turn"))?)
            } else {
                turn.clone()
            };
            if let Some(binding) = binding {
                bindings.push((job, binding));
            }
        }
        if bindings.is_empty() {
            return Ok(());
        }
        // Legacy coordinators cannot dispatch or batch these result markers on rollback.
        let callback = Uuid::new_v4().to_string();
        self.execute(
            "INSERT INTO callbacks(id,parent,client,state,message) VALUES (?,?,?,'goal_owned',?)",
            &[
                json!(callback),
                json!(root),
                json!(Uuid::new_v4().to_string()),
                json!("Plus goal-owned results; continuation requires the goal-aware protocol"),
            ],
        )?;
        for (job, turn) in bindings {
            self.execute(
                "INSERT OR IGNORE INTO goal_jobs(goal,job,turn,input_epoch) VALUES (?,?,?,?)",
                &[
                    turn["goal"].clone(),
                    json!(job.id),
                    json!(origin),
                    turn["input_epoch"].clone(),
                ],
            )?;
            self.execute(
                "UPDATE jobs SET callback=? WHERE id=? AND callback IS NULL",
                &[json!(callback), json!(job.id)],
            )?;
        }
        Ok(())
    }
}
