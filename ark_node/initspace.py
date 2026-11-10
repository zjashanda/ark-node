"""任务目录与运行时目录初始化（幂等）。

两条路径
--------
* **运行时目录**：``$ARK_NODE_HOME``（默认 ``~/.config/ark``）下固定创建
  ``cache / spool / quarantine / logs``，权限 700。
* **任务目录**：``--base-dir``（默认 ``~/.ark/tasks`` 或 ``ARK_TASK_ROOT``）下
  按 ``YYYYMMDD-HHMMSS-slug`` 建目录；同一 slug 已存在时复用最新目录，绝不
  重复堆积。

``--dry-run`` 只计算并打印路径，**不创建任何文件**。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import identity, util

SCHEMA_TASK = "ark-node-task.v1"
DEFAULT_TASK_ROOT = "~/.ark/tasks"
MAX_SLUG_LEN = 40
RUNTIME_SUBDIRS = ("cache", "spool", "quarantine", "logs")
TASK_SUBDIRS = ("logs", "artifacts", "tmp")

_SLUG_INVALID = re.compile(r"[^a-z0-9._-]+")


def sanitize_slug(raw: str) -> str:
    """slug 规范化：小写、非 ``[a-z0-9._-]`` 字符折叠为 ``-``、最长 40。"""
    text = (raw or "").strip().lower()
    text = _SLUG_INVALID.sub("-", text).strip("-")
    if len(text) > MAX_SLUG_LEN:
        text = text[:MAX_SLUG_LEN].rstrip("-")
    return text


def resolve_base_dir(base_dir: Optional[str] = None) -> Path:
    """任务根目录：``--base-dir`` > ``ARK_TASK_ROOT`` > ``~/.ark/tasks``。"""
    value = base_dir or os.environ.get("ARK_TASK_ROOT") or DEFAULT_TASK_ROOT
    return Path(value).expanduser()


def runtime_dirs() -> List[Path]:
    """运行时目录清单（含 ark home 本身）。"""
    home = util.home_dir()
    return [home] + [home / name for name in RUNTIME_SUBDIRS]


def find_existing_task(base: Path, slug: str) -> Optional[Path]:
    """在 base 下查找 ``YYYYMMDD-HHMMSS-<slug>``，返回最新的一个。"""
    pattern = re.compile(r"^\d{8}-\d{6}-" + re.escape(slug) + r"$")
    try:
        if not base.is_dir():
            return None
        matches = [p for p in base.iterdir() if p.is_dir() and pattern.match(p.name)]
    except OSError:
        return None
    if not matches:
        return None
    return max(matches, key=lambda p: p.name)


def _ensure_runtime() -> List[Path]:
    dirs = runtime_dirs()
    for path in dirs:
        util.ensure_private_dir(path)
    return dirs


def _create_task(base: Path, slug: str) -> Tuple[Path, List[Path], bool]:
    """创建或复用任务目录，返回 ``(task_dir, subdirs, reused)``。"""
    base.mkdir(parents=True, exist_ok=True)

    existing = find_existing_task(base, slug)
    if existing is not None:
        task_dir, reused = existing, True
    else:
        task_dir = base / f"{util.now_stamp()}-{slug}"
        task_dir.mkdir(parents=True, exist_ok=True)
        reused = False

    subdirs: List[Path] = []
    for name in TASK_SUBDIRS:
        path = task_dir / name
        path.mkdir(parents=True, exist_ok=True)
        subdirs.append(path)

    meta = task_dir / ".ark-task.json"
    if not meta.exists():
        util.atomic_write_json(
            meta,
            {
                "schema": SCHEMA_TASK,
                "slug": slug,
                "dir": str(task_dir),
                "created_at": util.now_display(),
                "node_id": identity.resolve()["node_id"],
            },
            0o600,
        )
    return task_dir, subdirs, reused


def _plan(base: Path, slug: Optional[str]) -> Dict[str, Any]:
    """dry-run 用的纯计算版本：不落盘、不解析身份。"""
    task_dir: Optional[Path] = None
    task_dirs: List[Path] = []
    reused = False
    if slug:
        existing = find_existing_task(base, slug)
        if existing is not None:
            task_dir, reused = existing, True
        else:
            task_dir = base / f"{util.now_stamp()}-{slug}"
        task_dirs = [task_dir / name for name in TASK_SUBDIRS]
    return {
        "task_dir": task_dir,
        "task_dirs": task_dirs,
        "reused": reused,
    }


def _payload(
    base: Path,
    runtime: List[Path],
    task_dir: Optional[Path],
    task_dirs: List[Path],
    reused: bool,
    dry_run: bool,
) -> Dict[str, Any]:
    return {
        "schema": SCHEMA_TASK,
        "dry_run": dry_run,
        "base_dir": str(base),
        "runtime_dirs": [str(p) for p in runtime],
        "task_dir": str(task_dir) if task_dir else None,
        "task_dirs": [str(p) for p in task_dirs],
        "reused": reused,
    }


def _emit(payload: Dict[str, Any], json_mode: bool) -> None:
    if json_mode:
        util.emit(payload)
        return
    for path in payload["runtime_dirs"]:
        print(path)
    if payload["task_dir"]:
        if payload["reused"]:
            print(f"reused: {payload['task_dir']}")
        else:
            print(payload["task_dir"])
        for path in payload["task_dirs"]:
            print(path)


def run(args: Any) -> int:
    """``ark-node init`` 入口。"""
    json_mode = bool(getattr(args, "json", False))
    dry_run = bool(getattr(args, "dry_run", False))
    raw_task = getattr(args, "task", None)
    slug = sanitize_slug(raw_task) if raw_task else None

    if raw_task and not slug:
        util.info("init: 任务 slug 无效（清洗后为空）")
        return util.EXIT_USAGE

    base = resolve_base_dir(getattr(args, "base_dir", None))
    runtime = runtime_dirs()

    if dry_run:
        plan = _plan(base, slug)
        _emit(_payload(base, runtime, plan["task_dir"], plan["task_dirs"], plan["reused"], True), json_mode)
        return util.EXIT_OK

    try:
        runtime = _ensure_runtime()
        task_dir: Optional[Path] = None
        task_dirs: List[Path] = []
        reused = False
        if slug:
            task_dir, task_dirs, reused = _create_task(base, slug)
    except OSError as exc:
        util.info("init: " + util.redact(str(exc)))
        return util.EXIT_USAGE

    _emit(_payload(base, runtime, task_dir, task_dirs, reused, False), json_mode)
    return util.EXIT_OK
