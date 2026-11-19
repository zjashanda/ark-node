from pathlib import Path

from ark_node.result_layout import build_manifest, create_layout, validate_layout
from ark_node.runstate import canonical_hash, can_start_device, input_is_current, progress_snapshot, validate_input


def valid_input():
    return {
        "task_id": "NOAH-72", "run_id": "run-1", "input_revision": 1,
        "config_hash": "abc", "desired_state": "RUN", "effective_from": "2026-11-13 14:00:00",
        "state": "EXECUTION_CHECK",
    }


def test_input_and_device_gate():
    payload = valid_input()
    assert validate_input(payload) == ()
    assert can_start_device(payload, {"APPROVAL_PASS": True, "RESULT_LAYOUT_PASS": True})
    assert input_is_current(payload, 1, "abc")
    assert not input_is_current(payload, 2, "abc")


def test_invalid_unconsumed_input_cannot_start():
    payload = valid_input()
    payload["state"] = "RECEIVED"
    assert not can_start_device(payload, {"APPROVAL_PASS": True})


def test_result_layout_and_manifest(tmp_path: Path):
    create_layout(tmp_path)
    for relative in ("README.md", "manifest.json", "summary.md", "receipt.json",
                     "events/tool.log", "events/events.jsonl", "events/rounds.jsonl",
                     "logs/runner.log", "logs/window.log", "logs/cloud.log"):
        (tmp_path / relative).write_text("{}\n", encoding="utf-8")
    assert validate_layout(tmp_path)["pass"]
    manifest = build_manifest("NOAH-72", "run-1", "abc", "def")
    assert manifest["layout_version"] == "lstest-result-v3"


def test_progress_contains_revision_and_file_evidence():
    event = progress_snapshot(valid_input(), 3, {"events/tool.log": {"bytes": 10}}, 3)
    assert event["event"] == "PROGRESS_5M"
    assert event["input_revision"] == 1
    assert event["files"]["events/tool.log"]["bytes"] == 10


def test_canonical_hash_is_stable():
    assert canonical_hash({"b": 2, "a": 1}) == canonical_hash({"a": 1, "b": 2})
