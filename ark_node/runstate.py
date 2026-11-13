"""Generic NoahsArk execution-control contracts.

This module is deliberately side-effect free: orchestration services and node
adapters can use the same validation before a device action is allowed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Set

SCHEMA = "ark.run-state.v1"
LAYOUT_VERSION = "lstest-result-v3"

INPUT_STATES = ("RECEIVED", "ACKNOWLEDGED", "APPLIED", "EXECUTION_CHECK", "RUNNING")
TERMINAL_STATES = ("BLOCKED", "SUPERSEDED", "COMPLETED", "CANCELLED")
REQUIRED_INPUT_FIELDS = (
    "task_id", "run_id", "input_revision", "config_hash", "desired_state",
    "effective_from",
)
REQUIRED_LAYOUT_FILES = (
    "README.md", "manifest.json", "summary.md", "receipt.json",
    "events/tool.log", "events/events.jsonl", "events/rounds.jsonl",
    "logs/runner.log", "logs/window.log", "logs/cloud.log",
)
REQUIRED_LAYOUT_DIRS = (
    "events", "logs", "logs/serial", "reports", "artifacts", "evidence", "milestones",
)


def canonical_hash(value: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 fingerprint for a JSON-compatible config."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_input(payload: Mapping[str, Any]) -> Sequence[str]:
    """Validate a desired-run snapshot without touching the filesystem."""
    errors = [f"missing:{field}" for field in REQUIRED_INPUT_FIELDS if not payload.get(field)]
    if payload.get("desired_state") not in ("RUN", "STOP", "PAUSE"):
        errors.append("invalid:desired_state")
    revision = payload.get("input_revision")
    if not isinstance(revision, int) or revision < 1:
        errors.append("invalid:input_revision")
    if payload.get("state") and payload["state"] not in INPUT_STATES + TERMINAL_STATES:
        errors.append("invalid:state")
    return tuple(errors)


def can_start_device(payload: Mapping[str, Any], gates: Mapping[str, bool]) -> bool:
    """Return true only when the current input was consumed and all gates pass."""
    if validate_input(payload):
        return False
    if payload.get("state") != "EXECUTION_CHECK":
        return False
    return all(bool(value) for value in gates.values()) and bool(gates)


def input_is_current(payload: Mapping[str, Any], current_revision: int, current_hash: str) -> bool:
    return (
        payload.get("input_revision") == current_revision
        and payload.get("config_hash") == current_hash
        and payload.get("state") in ("APPLIED", "EXECUTION_CHECK", "RUNNING")
    )


def supersede(payload: Mapping[str, Any], replacement_run_id: str) -> Dict[str, Any]:
    """Create a non-destructive supersession record for an older run."""
    result = dict(payload)
    result.update({"state": "SUPERSEDED", "superseded_by": replacement_run_id})
    return result


def validate_result_layout(root: Path) -> Sequence[str]:
    """Validate the canonical lstest result package layout."""
    errors = []
    for relative in REQUIRED_LAYOUT_DIRS:
        if not (root / relative).is_dir():
            errors.append(f"missing_dir:{relative}")
    for relative in REQUIRED_LAYOUT_FILES:
        if not (root / relative).is_file():
            errors.append(f"missing_file:{relative}")
    return tuple(errors)


def progress_snapshot(
    payload: Mapping[str, Any],
    done_rounds: int,
    files: Mapping[str, Mapping[str, Any]],
    last_round: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a JSON-safe PROGRESS_5M event payload."""
    return {
        "event": "PROGRESS_5M",
        "schema": SCHEMA,
        "task_id": payload.get("task_id"),
        "run_id": payload.get("run_id"),
        "input_revision": payload.get("input_revision"),
        "config_hash": payload.get("config_hash"),
        "done_rounds": done_rounds,
        "last_round": last_round,
        "files": dict(files),
    }
