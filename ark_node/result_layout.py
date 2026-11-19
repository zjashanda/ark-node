"""Canonical lstest result package helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .runstate import LAYOUT_VERSION, REQUIRED_LAYOUT_DIRS, REQUIRED_LAYOUT_FILES, validate_result_layout


def create_layout(root: Path) -> None:
    """Create the canonical result tree; callers populate files atomically."""
    for relative in REQUIRED_LAYOUT_DIRS:
        (root / relative).mkdir(parents=True, exist_ok=True)


def build_manifest(task_id: str, run_id: str, profile_commit: str, profile_sha256: str) -> Dict[str, Any]:
    return {
        "schema": "ark.result-manifest.v1",
        "layout_version": LAYOUT_VERSION,
        "task_id": task_id,
        "run_id": run_id,
        "profile_commit": profile_commit,
        "profile_sha256": profile_sha256,
        "required_dirs": list(REQUIRED_LAYOUT_DIRS),
        "required_files": list(REQUIRED_LAYOUT_FILES),
    }


def validate_layout(root: Path) -> Dict[str, Any]:
    errors = list(validate_result_layout(root))
    return {"schema": "ark.result-layout-validator.v1", "layout_version": LAYOUT_VERSION,
            "pass": not errors, "errors": errors}
