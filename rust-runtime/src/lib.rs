pub mod advisor;
pub mod background;
pub mod evidence;
pub mod execution_admission;
pub mod executions;
pub mod goal_command;
pub mod goals;
pub mod job_command;
pub mod ledger;
pub mod local;
pub mod maintenance;
pub mod python_text;
pub mod rpc;
pub mod runner;
pub mod upgrade;

pub const COORDINATOR_PROTOCOL: u64 = 4;
pub const UPGRADE_EXIT: i32 = 75;
