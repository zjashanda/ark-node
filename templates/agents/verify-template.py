#!/usr/bin/env python3
"""NoahsArk 智能体模板校验器：下载后先验，再信任。

用法::

    python3 verify-template.py                     # 校验脚本所在目录
    python3 verify-template.py --dir /some/where   # 校验指定目录
    python3 verify-template.py --json              # 机器可读结果

校验内容
--------
1. ``manifest.json`` 结构合法、字段齐全、版本与对象路径自洽；
2. 每个发布文件存在，字节数与 sha256 与 manifest 完全一致；
3. 发布包齐套（模板正文 / profile / 校验器三角色都在）；
4. 时间字段符合平台口径（北京时间、无时区标记、秒级精度）；
5. profile 的必填字段与角色约束成立（硬件台架并发必须为 1）；
6. 文件里没有凭据（token、私钥、URL 内嵌口令、高熵密钥串）。

退出码::

    0 全部通过
    1 有检查项失败（内容不可信）
    2 用法错误
    3 没有可校验的对象（manifest 缺失或不可读）

本脚本零第三方依赖，只用标准库：目标工作区不装任何包也能跑。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "1.0.0"
MANIFEST_SCHEMA = "ark.agent-template.manifest.v1"
PROFILE_SCHEMA = "ark.agent-profile.v1"
REQUIRED_ROLES = ("template", "profile", "verifier")
REQUIRED_MANIFEST_KEYS = (
    "schema",
    "name",
    "version",
    "channel",
    "published_at",
    "git",
    "oss",
    "compatible",
    "files",
)

TIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
# 内容摘要字段不是凭据，扫描时整行放行
DIGEST_LINE = re.compile(  # noqa: secret-scan
    r'"(?:content_|evidence_)?sha256"\s*:\s*"[0-9a-f]{64}"'
    r'|"commit"\s*:\s*"[0-9a-f]{40}"'
)

SECRET_RULES: List[Tuple[str, str]] = [
    ("github-token", r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    ("github-pat", r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    ("aliyun-ak", r"\bLTAI[0-9A-Za-z]{12,}"),
    ("aws-ak", r"\bAKIA[0-9A-Z]{12,}"),
    ("private-key", "-----BEGIN" + r"[ \t]+[A-Z ]*PRIVATE KEY-----"),
    ("url-credentials", r"[A-Za-z][A-Za-z0-9+.\-]*://[^/\s:@]+:[^/\s@]+@"),
]
COMPILED_SECRETS = [(name, re.compile(pattern)) for name, pattern in SECRET_RULES]
HEX_BLOB = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{32,}(?![0-9A-Za-z])")  # noqa: secret-scan
B64_BLOB = re.compile(r"(?<![0-9A-Za-z+/=])[A-Za-z0-9+/]{32,}={0,2}(?![0-9A-Za-z+/=])")  # noqa: secret-scan

PROFILE_REQUIRED = (
    "schema",
    "agent_name",
    "role",
    "description",
    "runtime.runtime_id",
    "runtime.model",
    "runtime.max_concurrent_tasks",
    "paths.task_root_windows",
    "paths.task_root_linux",
    "paths.task_dir_pattern_linux",
    "paths.subdirs",
    "sources.git.canonical_repo",
    "sources.skillhub.manifest",
    "storage.root",
    "time.display_format",
    "time.stamp_format",
    "env_keys",
    "loop",
    "supervision.rules",
    "approval.requires",
)
PROFILE_ROLES = ("coordinator", "node-executor", "hardware-bench", "knowledge-curator")
EXPECTED_LOOP = ["ark-dispatch", "execute", "ark-archive", "receipt", "ark-kb", "next-dispatch"]
EXPECTED_SUPERVISION = ["EXECUTION_CHECK", "PROGRESS_5M", "STATE_DRIFT", "RUNNING_NO_EVIDENCE"]
EXPECTED_SUBDIRS = ["plan", "assets", "work", "logs", "result", "receipts", "manifest"]

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_NO_SOURCE = 3


class Check:
    __slots__ = ("name", "ok", "detail")

    def __init__(self, name: str, ok: bool, detail: str = "") -> None:
        self.name = name
        self.ok = ok
        self.detail = detail

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "result": "pass" if self.ok else "fail", "detail": self.detail}


def _dig(obj: Any, dotted: str) -> Any:
    current = obj
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _entropy(text: str) -> float:
    counts: Dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    total = len(text)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def _looks_random(token: str) -> bool:
    if len(token) < 32 or len(set(token)) < 12:
        return False
    if not any(char.isdigit() for char in token) or not any(char.isalpha() for char in token):
        return False
    return _entropy(token) >= 3.5


def scan_text(text: str) -> List[str]:
    """逐行找凭据；行内出现摘要字段时跳过该行。"""
    hits: List[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        if "noqa: secret-scan" in line or DIGEST_LINE.search(line):
            continue
        for name, pattern in COMPILED_SECRETS:
            if pattern.search(line):
                hits.append(f"line {number}: {name}")
                break
        else:
            if any(_looks_random(token) for token in HEX_BLOB.findall(line)):
                hits.append(f"line {number}: high-entropy-hex")
                continue
            if any(_looks_random(token) for token in B64_BLOB.findall(line)):
                hits.append(f"line {number}: high-entropy-base64")
    return hits


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_manifest_shape(manifest: Dict[str, Any]) -> List[Check]:
    checks: List[Check] = []
    missing = [key for key in REQUIRED_MANIFEST_KEYS if key not in manifest]
    checks.append(Check("manifest-shape", not missing, "缺少字段: " + ",".join(missing) if missing else "字段齐全"))
    checks.append(
        Check(
            "manifest-schema",
            manifest.get("schema") == MANIFEST_SCHEMA,
            f"schema={manifest.get('schema')}",
        )
    )

    version = str(manifest.get("version", ""))
    version_ok = bool(VERSION_PATTERN.match(version))
    checks.append(Check("manifest-version", version_ok, f"version={version}"))

    published = manifest.get("published_at")
    time_ok = isinstance(published, str) and bool(TIME_PATTERN.match(published))
    checks.append(
        Check(
            "manifest-time-format",
            time_ok,
            "北京时间、无时区标记、秒级" if time_ok else f"published_at 不合口径: {published!r}",
        )
    )

    files = manifest.get("files")
    files_ok = isinstance(files, list) and bool(files)
    detail = f"{len(files)} 个文件" if files_ok else "files 为空或类型错误"
    checks.append(Check("manifest-files", files_ok, detail))

    if files_ok and version_ok:
        prefix = (manifest.get("oss") or {}).get("prefix", "")
        bad = [
            entry.get("object", "")
            for entry in files
            if f"/v{version}/" not in str(entry.get("object", ""))
            or not str(entry.get("object", "")).startswith(str(prefix))
        ]
        checks.append(
            Check(
                "manifest-object-paths",
                not bad,
                "路径与版本/前缀自洽" if not bad else "不自洽: " + ", ".join(bad[:3]),
            )
        )
    return checks


def check_files(manifest: Dict[str, Any], base: Path) -> List[Check]:
    checks: List[Check] = []
    entries = manifest.get("files") or []
    roles = set()
    ok_all = True
    details: List[str] = []

    for entry in entries:
        name = str(entry.get("path", ""))
        roles.add(str(entry.get("role", "")))
        target = base / name
        if not target.exists():
            ok_all = False
            details.append(f"{name}: 缺失")
            continue
        expected_bytes = entry.get("bytes")
        actual_bytes = target.stat().st_size
        if isinstance(expected_bytes, int) and expected_bytes != actual_bytes:
            ok_all = False
            details.append(f"{name}: 字节 {actual_bytes} != {expected_bytes}")
            continue
        expected_digest = str(entry.get("sha256", ""))
        if not DIGEST_PATTERN.match(expected_digest):
            ok_all = False
            details.append(f"{name}: manifest 摘要格式非法")
            continue
        actual_digest = sha256_file(target)
        if actual_digest != expected_digest:
            ok_all = False
            details.append(f"{name}: sha256 不符（期望 {expected_digest[:12]}...，实际 {actual_digest[:12]}...）")

    checks.append(Check("file-integrity", ok_all, "全部一致" if ok_all else "; ".join(details)))
    missing_roles = [role for role in REQUIRED_ROLES if role not in roles]
    checks.append(
        Check("package-completeness", not missing_roles, "三角色齐备" if not missing_roles else "缺少角色: " + ",".join(missing_roles))
    )
    return checks


def check_secrets(manifest: Dict[str, Any], base: Path) -> List[Check]:
    findings: List[str] = []
    for entry in manifest.get("files") or []:
        target = base / str(entry.get("path", ""))
        if not target.exists():
            continue
        try:
            text = target.read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            findings.append(f"{target.name}: 无法读取（{exc}）")
            continue
        for hit in scan_text(text):
            findings.append(f"{target.name} {hit}")
    return [Check("no-credentials", not findings, "未发现凭据" if not findings else "; ".join(findings[:5]))]


def check_profile(manifest: Dict[str, Any], base: Path) -> List[Check]:
    profile_entry = next(
        (entry for entry in manifest.get("files") or [] if entry.get("role") == "profile"), None
    )
    if profile_entry is None:
        return [Check("profile-schema", False, "发布包里没有 profile")]
    path = base / str(profile_entry.get("path", ""))
    if not path.exists():
        return [Check("profile-schema", False, "profile 文件缺失")]
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [Check("profile-schema", False, f"profile 不是合法 JSON: {exc}")]

    checks: List[Check] = []
    checks.append(Check("profile-schema", profile.get("schema") == PROFILE_SCHEMA, f"schema={profile.get('schema')}"))

    default = profile.get("default") if isinstance(profile.get("default"), dict) else {}
    missing = [key for key in PROFILE_REQUIRED if _dig(default, key) is None]
    checks.append(
        Check("profile-required-fields", not missing, "必填齐全" if not missing else "缺少: " + ",".join(missing))
    )

    role = str(default.get("role", ""))
    checks.append(Check("profile-role-enum", role in PROFILE_ROLES, f"role={role}"))

    concurrency = _dig(default, "runtime.max_concurrent_tasks")
    bench = _dig(profile, "roles.hardware-bench.max_concurrent_tasks")
    bench_ok = concurrency == 1 if role == "hardware-bench" else True
    checks.append(
        Check(
            "profile-bench-concurrency",
            bench_ok and bench == 1,
            f"本实例 max_concurrent_tasks={concurrency}, 台架预设={bench}（台架必须为 1）",
        )
    )

    checks.append(
        Check(
            "profile-loop-order",
            _dig(default, "loop") == EXPECTED_LOOP,
            f"loop={_dig(default, 'loop')}",
        )
    )
    checks.append(
        Check(
            "profile-supervision",
            _dig(default, "supervision.rules") == EXPECTED_SUPERVISION,
            f"rules={_dig(default, 'supervision.rules')}",
        )
    )
    checks.append(
        Check(
            "profile-subdirs",
            _dig(default, "paths.subdirs") == EXPECTED_SUBDIRS,
            f"subdirs={_dig(default, 'paths.subdirs')}",
        )
    )

    display = _dig(default, "time.display_format")
    stamp = _dig(default, "time.stamp_format")
    checks.append(
        Check(
            "profile-time-format",
            display == "YYYY-MM-DD HH:MM:SS" and stamp == "YYYYMMDD-HHMMSS",
            f"display={display}, stamp={stamp}",
        )
    )

    env_keys = _dig(default, "env_keys") or []
    keys_ok = isinstance(env_keys, list) and all(
        isinstance(key, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", key) for key in env_keys
    )
    checks.append(
        Check("profile-env-keys", keys_ok, "只有键名" if keys_ok else "env_keys 含非键名内容（可能有值）")
    )
    return checks


def verify(base: Path, manifest_path: Optional[Path] = None) -> Tuple[List[Check], Optional[Dict[str, Any]]]:
    target = manifest_path or (base / "manifest.json")
    if not target.exists():
        raise FileNotFoundError(f"manifest 不存在: {target}")
    manifest = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("manifest 顶层必须是对象")

    checks: List[Check] = []
    checks.extend(check_manifest_shape(manifest))
    checks.extend(check_files(manifest, base))
    checks.extend(check_secrets(manifest, base))
    checks.extend(check_profile(manifest, base))
    return checks, manifest


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="verify-template.py",
        description="校验 NoahsArk 智能体模板发布包的完整性与安全性。",
    )
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parent), help="发布包所在目录")
    parser.add_argument("--manifest", default=None, help="manifest 路径，默认 <dir>/manifest.json")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    base = Path(args.dir).resolve()
    manifest_path = Path(args.manifest).resolve() if args.manifest else None

    try:
        checks, manifest = verify(base, manifest_path)
    except FileNotFoundError as exc:
        sys.stderr.write(f"verify-template: {exc}\n")
        return EXIT_NO_SOURCE
    except (ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"verify-template: manifest 不可读: {exc}\n")
        return EXIT_NO_SOURCE

    failed = [check for check in checks if not check.ok]
    result = "pass" if not failed else "fail"

    if args.json:
        print(
            json.dumps(
                {
                    "schema": "ark.agent-template.verify.v1",
                    "dir": str(base),
                    "template_version": (manifest or {}).get("version"),
                    "checks": [check.as_dict() for check in checks],
                    "result": result,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for check in checks:
            mark = "PASS" if check.ok else "FAIL"
            print(f"[{mark}] {check.name:<28} {check.detail}")
        print()
        print(f"结果: {result}（{len(checks) - len(failed)}/{len(checks)} 项通过）")

    return EXIT_OK if not failed else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
