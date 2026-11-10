#!/usr/bin/env python3
"""时间格式检查器：强制“北京时间、无时区后缀”的展示契约。

用法::

    python3 tools/time_lint.py [PATH ...]

不带路径时检查仓库内 git 跟踪的文件（git 不可用时回退到 os.walk 遍历仓库根）。
每条违规输出 ``path:line: rule: source``，结尾给一行汇总；有违规返回 1，
全部干净返回 0，用法错误返回 2。

约定
----
本文件里的检测用正则本身含有“违规形状”的字面量，因此这些行统一带
``# noqa: time-lint`` 标记，扫描器会整行跳过——与 ``tools/secret_scan.py``
使用 ``# noqa: secret-scan`` 的做法一致。检查器必须能干净地检查它自己。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

VERSION = "time-lint 0.1.0"
NOQA_MARKER = "noqa: time-lint"
REPO_ROOT = Path(__file__).resolve().parent.parent

SKIP_DIR_NAMES = {".git", "__pycache__"}
SKIP_SUFFIXES = {".pyc"}

_USAGE = (
    "usage: python3 tools/time_lint.py [--json] [--list-rules] [--version] [PATH ...]\n"
    "Checks that date-time output uses Beijing time with no timezone designator.\n"
)

# --------------------------------------------------------------------- 规则

RULES: List[Tuple[str, str]] = [
    ("iso-t-separator", "date-time literal using 'T' as the date/time separator (use a space)"),
    ("iso-z-suffix", "time literal carrying a trailing 'Z' UTC designator (forbidden)"),
    ("utc-offset", "time literal followed by a numeric UTC offset (forbidden)"),
    ("rfc3339-mention", "recommendation to emit an RFC-3339/ISO time string (forbidden)"),  # noqa: time-lint
    ("doc-placeholder", "documentation placeholder that prescribes a 'T' date-time shape"),
    ("stamp-in-field", "non-raw JSON field holding a compact stamp (stamps belong in names only)"),
]

T_SEPARATOR = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")  # noqa: time-lint
Z_SUFFIX = re.compile(r"\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")  # noqa: time-lint
UTC_OFFSET = re.compile(r"\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?[+-]\d{2}:?\d{2}\b")  # noqa: time-lint
TIME_RECOMMENDATION = re.compile(r"(?:RFC3339|rfc3339|isoformat\(|utcnow\(|utcfromtimestamp\()")  # noqa: time-lint
DOC_PLACEHOLDER = re.compile(r"(?:YYYY|yyyy)-(?:MM|mm)-(?:DD|dd)\[?[Tt]|%Y-%m-%dT")  # noqa: time-lint
STAMP_FIELD = re.compile(r"""["']([A-Za-z0-9_.\-]+)["']\s*:\s*["'](\d{8}-\d{6})["']""")  # noqa: time-lint
RAW_KEY = re.compile(r"""(?:["']raw_[A-Za-z0-9_.\-]*["']|(?<![\w])raw_[A-Za-z0-9_]*)\s*[:=]""")  # noqa: time-lint

# 规则 id 本身也含被禁词元，用拼接构造，避免源码里出现完整字面量。
MENTION_RULE_ID = "rfc" + "3339-mention"

VALUE_RULES: List[Tuple[str, Any]] = [
    ("iso-t-separator", T_SEPARATOR),
    ("iso-z-suffix", Z_SUFFIX),
    ("utc-offset", UTC_OFFSET),
]


def _line_hits(line: str) -> List[str]:
    """返回该行命中的规则 id 列表（顺序稳定）。"""
    hits: List[str] = []
    raw_exempt = bool(RAW_KEY.search(line))
    if not raw_exempt:
        for rule, pattern in VALUE_RULES:
            if pattern.search(line):
                hits.append(rule)
    if TIME_RECOMMENDATION.search(line):
        hits.append(MENTION_RULE_ID)
    if DOC_PLACEHOLDER.search(line):
        hits.append("doc-placeholder")
    for match in STAMP_FIELD.finditer(line):
        key = match.group(1)
        if key.startswith("raw_"):
            continue
        hits.append("stamp-in-field")
        break
    return hits


# --------------------------------------------------------------------- 文件


def _walk(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIR_NAMES)
        for name in sorted(filenames):
            candidate = Path(dirpath) / name
            if candidate.suffix.lower() in SKIP_SUFFIXES:
                continue
            yield candidate


def _git_tracked(root: Path) -> Optional[List[Path]]:
    """git 可用时返回仓库内被跟踪的文件；不可用返回 None。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    names = [name for name in proc.stdout.decode("utf-8", errors="surrogateescape").split("\0") if name]
    files: List[Path] = []
    for name in names:
        candidate = root / name
        if candidate.suffix.lower() in SKIP_SUFFIXES:
            continue
        if any(part in SKIP_DIR_NAMES for part in candidate.parts):
            continue
        files.append(candidate)
    return files


def _collect(paths: List[str]) -> List[Path]:
    if not paths:
        tracked = _git_tracked(REPO_ROOT)
        candidates = tracked if tracked is not None else list(_walk(REPO_ROOT))
    else:
        candidates = []
        for raw in paths:
            target = Path(raw)
            if target.is_dir():
                candidates.extend(_walk(target))
            else:
                candidates.append(target)

    seen = set()
    files: List[Path] = []
    for candidate in candidates:
        if candidate.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            key = str(candidate.resolve())
        except OSError:
            key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        files.append(candidate)
    return files


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except (ValueError, OSError):
        return str(path)


# --------------------------------------------------------------------- 输出


def _report(violations: List[Dict[str, Any]], checked: int, as_json: bool) -> int:
    try:
        if as_json:
            payload = {
                "violations": violations,
                "checked": checked,
                "violations_count": len(violations),
            }
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        else:
            for item in violations:
                sys.stdout.write(
                    f"{item['path']}:{item['line']}: {item['rule']}: {item['text']}\n"
                )
            sys.stdout.write(
                f"time-lint: {len(violations)} violation(s) in {checked} file(s) checked\n"
            )
        sys.stdout.flush()
    except BrokenPipeError:
        # 输出被 head 等消费方提前关闭时安静退出，不打印回溯。
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        return 1
    return 1 if violations else 0


def main(argv: List[str]) -> int:
    paths: List[str] = []
    as_json = False
    for arg in argv:
        if arg in ("-h", "--help"):
            sys.stdout.write(_USAGE)
            return 0
        if arg == "--version":
            sys.stdout.write(VERSION + "\n")
            return 0
        if arg == "--list-rules":
            for rule, description in RULES:
                sys.stdout.write(f"{rule}: {description}\n")
            return 0
        if arg == "--json":
            as_json = True
            continue
        if arg.startswith("-"):
            sys.stderr.write(f"time-lint: unknown option: {arg}\n")
            sys.stderr.write(_USAGE)
            return 2
        paths.append(arg)

    for raw in paths:
        if not Path(raw).exists():
            sys.stderr.write(f"time-lint: no such path: {raw}\n")
            return 2

    violations: List[Dict[str, Any]] = []
    checked = 0
    for path in _collect(paths):
        try:
            raw_bytes = path.read_bytes()
        except OSError as exc:
            sys.stderr.write(f"time-lint: cannot read {path}: {exc}\n")
            continue
        if b"\x00" in raw_bytes:
            continue
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue
        checked += 1
        display = _display_path(path)
        for number, line in enumerate(text.splitlines(), 1):
            if NOQA_MARKER in line:
                continue
            for rule in _line_hits(line):
                violations.append(
                    {"path": display, "line": number, "rule": rule, "text": line.strip()}
                )

    violations.sort(key=lambda item: (item["path"], item["line"], item["rule"]))
    return _report(violations, checked, as_json)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
