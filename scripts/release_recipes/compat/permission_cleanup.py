"""Use the release runner's audited ownership predicate."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from release_ownership import capture_baseline, control_ancestors, inspect_processes, process_identity
