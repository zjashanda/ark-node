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
6. 文件里没有凭据（token、私钥、URL 内嵌口令、高熵密钥串）；
7. 门禁事件契约（``run-state.v1.json``）与 profile 的 ``run_state`` / ``stage_policy`` /
   ``gate_events`` / ``receipt_layers`` 四段自洽，且示例事件能按契约校验。

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

VERSION = "1.7.0"
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
    "identity.role_family",
    "identity.official_role",
    "identity.avatar_icon",
    "identity.avatar_policy",
    "runtime.runtime_id",
    "runtime.model",
    "runtime.node_binding",
    "runtime.max_concurrent_tasks",
    "paths.task_root_windows",
    "paths.task_root_linux",
    "paths.task_dir_pattern",
    "paths.task_id_source",
    "paths.subdirs",
    "paths.root_files",
    "paths.on_violation",
    "sources.git.canonical_repo",
    "sources.skillhub.manifest",
    "storage.root",
    "time.display_format",
    "time.stamp_format",
    "env_keys",
    "loop",
    "supervision.rules",
    "approval.requires",
    "execution_channel",
    "execution_channel.mode",
    "execution_channel.no_visible_terminal",
    "execution_channel.non_interactive",
)
PROFILE_ROLES = ("coordinator", "node-executor", "jinyiwei", "hardware-bench", "knowledge-curator")
EXPECTED_ROLE_ICONS = {
    "coordinator": "🧭",
    "node-executor": "⚙️",
    "jinyiwei": "🥷",
    "hardware-bench": "🔧",
    "knowledge-curator": "📚",
}
EXPECTED_OFFICIAL_ICONS = {
    "mika": "🇨🇳",
    "吏部": "👥",
    "户部": "🧾",
    "礼部": "📜",
    "兵部": "🛡️",
    "刑部": "⚖️",
    "工部": "🛠️",
    "jinyiwei": "🥷",
}
EXPECTED_PREVIEW_FIELDS = ["名称", "部门/层级", "职责", "禁区", "runtime", "模型", "并发", "图标", "固定目录", "验收"]
JINYIWEI_NAME = re.compile(r"^锦衣卫·\S+(?:·[1-9]\d*)?$")
JINYIWEI_FORBIDDEN_NAME_TOKENS = ("listenai", "deepseek", "gpt", "claude", "gemini")
JINYIWEI_MODEL = "listenai/deepseek-flash"
JINYIWEI_FORBIDDEN_SCOPES = ["planning", "coordination", "cross-node", "general-purpose"]
EXPECTED_LOOP = ["ark-dispatch", "execute", "ark-archive", "receipt", "ark-kb", "next-dispatch"]
EXPECTED_SUPERVISION = ["EXECUTION_CHECK", "PROGRESS_5M", "STATE_DRIFT", "RUNNING_NO_EVIDENCE"]
EXPECTED_SUBDIRS = ["assets", "work", "logs", "result", "receipts"]
EXPECTED_ROOT_FILES = ["plan.md", "manifest.json"]
EXPECTED_TASK_ROOTS = {
    "task_root_windows": "D:\\zhsh\\workspace\\tasks",
    "task_root_linux": "/home/zhsh/workspace/tasks",
}
EXPECTED_WORKDIR_VIOLATION = "BLOCKED_WORKDIR_POLICY"
EXPECTED_CHANNEL_VIOLATION = "BLOCKED_EXECUTION_CHANNEL"
EXPECTED_EXECUTION_CHANNEL = {
    "mode": "hidden-background",
    "no_visible_terminal": True,
    "non_interactive": True,
    "stdout_redirect": "logs/",
    "stderr_redirect": "logs/",
    "require_batch_mode": True,
    "launch_record": "logs/launch.json",
    "unverifiable_action": EXPECTED_CHANNEL_VIOLATION,
    "background_and_yield": "forbidden",
}
EXPECTED_FORBIDDEN_LAUNCHERS = (
    "Start-Process",
    "Win32_Process.Create",
    "conhost",
    "wt",
    "powershell -NoExit",
)
EXECUTION_CHANNEL_TOKENS = ("execution_channel", "no_visible_terminal", "non_interactive")
EXECUTION_CHANNEL_HEADING = "## 11. 执行通道与窗口"

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
        prefix = str((manifest.get("oss") or {}).get("prefix", ""))
        channel = str(manifest.get("channel") or "immutable")
        segment = "/latest/" if channel == "latest" else f"/v{version}/"
        bad = [
            str(entry.get("object", ""))
            for entry in files
            if not str(entry.get("object", "")).startswith(prefix)
            or segment not in str(entry.get("object", ""))
            or not str(entry.get("object", "")).endswith("/" + str(entry.get("path", "")))
        ]
        checks.append(
            Check(
                "manifest-object-paths",
                not bad,
                f"渠道 {channel}，路径与版本/前缀自洽"
                if not bad
                else "不自洽: " + ", ".join(bad[:3]),
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

    role_icons = profile.get("role_icons") if isinstance(profile.get("role_icons"), dict) else {}
    roles = profile.get("roles") if isinstance(profile.get("roles"), dict) else {}
    icon_map_ok = all(role_icons.get(name) == icon for name, icon in EXPECTED_ROLE_ICONS.items()) and all(
        isinstance(roles.get(name), dict) and roles[name].get("avatar_icon") == icon
        for name, icon in EXPECTED_ROLE_ICONS.items()
    )
    checks.append(Check("profile-role-icons", icon_map_ok, "稳定图标映射正确" if icon_map_ok else "角色图标映射缺失或漂移"))

    official = profile.get("official_system") if isinstance(profile.get("official_system"), dict) else {}
    ministries = official.get("ministries") if isinstance(official.get("ministries"), dict) else {}
    official_icons = {"mika": _dig(official, "mika.avatar_icon"), "jinyiwei": _dig(official, "jinyiwei.avatar_icon")}
    official_icons.update({name: spec.get("avatar_icon") for name, spec in ministries.items() if isinstance(spec, dict)})
    official_ok = (
        official_icons == EXPECTED_OFFICIAL_ICONS
        and _dig(official, "mika.outside_six_ministries") is True
        and set(ministries) == {"吏部", "户部", "礼部", "兵部", "刑部", "工部"}
        and official.get("default_model") == JINYIWEI_MODEL
        and official.get("preview_required") == EXPECTED_PREVIEW_FIELDS
        and official.get("random_avatar") == "forbidden"
        and official.get("rename_requires_icon_review") is True
    )
    checks.append(Check("profile-official-system", official_ok, "Mika + 六部 + 锦衣卫体系正确" if official_ok else "官职、部徽或创建预览契约漂移"))

    checks.extend(check_profile_identity(default, roles, role_icons, "default"))
    jinyiwei = roles.get("jinyiwei") if isinstance(roles.get("jinyiwei"), dict) else {}
    jinyiwei_contract_ok = (
        jinyiwei.get("role_family") == "node-executor"
        and jinyiwei.get("default_model") == JINYIWEI_MODEL
        and jinyiwei.get("max_concurrent_tasks") == 1
        and jinyiwei.get("runtime_binding") == "required-exact"
        and jinyiwei.get("accepted_dispatchers") == ["Mika", "Issue"]
        and jinyiwei.get("forbidden_scopes") == JINYIWEI_FORBIDDEN_SCOPES
    )
    checks.append(Check("profile-jinyiwei-contract", jinyiwei_contract_ok, "节点专属执行边界正确" if jinyiwei_contract_ok else "锦衣卫预设缺失或边界漂移"))

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
    checks.append(
        Check(
            "profile-root-files",
            _dig(default, "paths.root_files") == EXPECTED_ROOT_FILES,
            f"root_files={_dig(default, 'paths.root_files')}",
        )
    )
    roots = {key: _dig(default, f"paths.{key}") for key in EXPECTED_TASK_ROOTS}
    pattern = _dig(default, "paths.task_dir_pattern")
    violation = _dig(default, "paths.on_violation")
    roots_ok = roots == EXPECTED_TASK_ROOTS and pattern == "<tasks-id>"
    checks.append(
        Check(
            "profile-workdir-policy",
            roots_ok and violation == EXPECTED_WORKDIR_VIOLATION,
            f"roots={roots}, pattern={pattern}, on_violation={violation}",
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


def check_profile_identity(
    instance: Dict[str, Any], roles: Dict[str, Any], role_icons: Dict[str, Any], label: str
) -> List[Check]:
    """校验实例角色、稳定图标；锦衣卫额外校验名称、节点绑定、模型与串行入口。"""
    role = str(instance.get("role", ""))
    identity = instance.get("identity") if isinstance(instance.get("identity"), dict) else {}
    runtime = instance.get("runtime") if isinstance(instance.get("runtime"), dict) else {}
    icon = role_icons.get(role)
    identity_ok = (
        role in roles
        and identity.get("official_role") == role
        and identity.get("role_family") == roles.get(role, {}).get("role_family")
        and identity.get("avatar_icon") == icon
        and identity.get("avatar_policy") == "stable-role-mapping"
    )
    checks = [Check(f"{label}-role-icon", identity_ok, f"role={role}, avatar_icon={identity.get('avatar_icon')}")]
    if role != "jinyiwei":
        return checks
    runtime_id = runtime.get("runtime_id")
    binding = runtime.get("node_binding")
    problems = []
    agent_name = str(instance.get("agent_name", ""))
    if not JINYIWEI_NAME.fullmatch(agent_name) or any(
        token in agent_name.lower() for token in JINYIWEI_FORBIDDEN_NAME_TOKENS
    ):
        problems.append("agent_name")
    if not runtime_id or binding != runtime_id:
        problems.append("runtime.node_binding")
    if runtime.get("model") != JINYIWEI_MODEL:
        problems.append("runtime.model")
    if runtime.get("max_concurrent_tasks") != 1:
        problems.append("runtime.max_concurrent_tasks")
    checks.append(Check(f"{label}-jinyiwei-binding", not problems, "节点、模型和串行入口正确" if not problems else "不符: " + ",".join(problems)))
    return checks



def validate_channel_command(platform: str, command: str) -> List[str]:
    """校验一条命令是否符合执行通道规则。返回违规原因列表，空列表表示通过。

    windows: 隐藏、非交互、无桌面窗口；禁止 Start-Process / Win32_Process.Create
             等未经验证的启动器，禁止一切会弹出控制台的写法。
    linux/macos: 非交互 SSH/Agent 通道，ssh 必须带 -T 与 BatchMode=yes。
    所有平台: 输出必须重定向到任务目录 logs/。
    """
    normalized = (platform or "").strip().lower()
    if normalized == "darwin":
        normalized = "macos"
    if normalized not in ("windows", "linux", "macos"):
        return [f"未知平台: {platform}"]

    text = (command or "").strip()
    if not text:
        return ["命令为空"]
    lowered = text.lower()

    reasons: List[str] = []
    if normalized == "windows":
        for token in EXPECTED_FORBIDDEN_LAUNCHERS:
            if token.lower() in lowered:
                reasons.append(f"出现被禁止的启动器: {token}")
        for token in ("cmd /k", "cmd.exe /k", "start ", "wt.exe", "conhost.exe"):
            if token in lowered:
                reasons.append(f"会弹出可见终端: {token}")
    else:
        if lowered.startswith("ssh") or " ssh " in lowered:
            if " -t " not in f" {lowered} ":
                reasons.append("ssh 缺少 -T：可能分配伪终端")
            if "batchmode=yes" not in lowered:
                reasons.append("ssh 缺少 -o BatchMode=yes：可能等待交互输入")
        for token in ("bash -i", "sh -i", "zsh -i", "sshpass -p", "read -p"):
            if token in lowered:
                reasons.append(f"交互式或明文口令通道: {token}")

    if ">" not in text or "logs" not in lowered:
        reasons.append("输出未重定向到任务目录 logs/")
    return reasons


def check_execution_channel(manifest: Dict[str, Any], base: Path) -> List[Check]:
    checks: List[Check] = []
    entries = manifest.get("files") or []
    profile_entry = next((entry for entry in entries if entry.get("role") == "profile"), None)
    template_entry = next((entry for entry in entries if entry.get("role") == "template"), None)

    channel: Any = None
    if profile_entry is not None:
        path = base / str(profile_entry.get("path", ""))
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
            default = profile.get("default") if isinstance(profile.get("default"), dict) else {}
            channel = default.get("execution_channel")
        except (OSError, json.JSONDecodeError):
            channel = None

    if not isinstance(channel, dict):
        checks.append(Check("profile-execution-channel", False, "profile 缺少 execution_channel 段"))
    else:
        mismatched = {
            key: channel.get(key)
            for key, expected in EXPECTED_EXECUTION_CHANNEL.items()
            if channel.get(key) != expected
        }
        launchers = channel.get("forbidden_launchers")
        launchers_ok = isinstance(launchers, list) and sorted(launchers) == sorted(EXPECTED_FORBIDDEN_LAUNCHERS)
        ok = not mismatched and launchers_ok
        detail = (
            "通道三判据齐备"
            if ok
            else f"不符: {sorted(mismatched)}" + ("" if launchers_ok else " forbidden_launchers 不全")
        )
        checks.append(Check("profile-execution-channel", ok, detail))
        checks.append(
            Check(
                "profile-channel-gate",
                channel.get("unverifiable_action") == EXPECTED_CHANNEL_VIOLATION,
                f"unverifiable_action={channel.get('unverifiable_action')}",
            )
        )

    text = ""
    if template_entry is not None:
        path = base / str(template_entry.get("path", ""))
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
    missing_tokens = [token for token in EXECUTION_CHANNEL_TOKENS if token not in text]
    heading_ok = EXECUTION_CHANNEL_HEADING in text
    checks.append(
        Check(
            "template-execution-channel",
            not missing_tokens and heading_ok,
            "字段与章节齐备"
            if not missing_tokens and heading_ok
            else f"缺少: {missing_tokens} heading={heading_ok}",
        )
    )
    return checks

RUN_STATE_SCHEMA = "ark.run-state.v1"
RUN_STATE_FILE = "run-state.v1.json"
RUN_STATE_EVENTS = ("EXECUTION_CHECK", "PROGRESS_5M", "STAGE_FINISHED", "BLOCKED", "FINAL_RECEIPT")
RUN_STATE_HEADING = "## 14. 状态机与阶段门禁"
RUN_STATE_TOKENS = RUN_STATE_EVENTS + ("run-state.v1.json", "receipts/events/", "receipts/stage-index.json")
ENVELOPE_KEYS = ("schema", "event", "run_id", "issue_id", "stage", "at")
EXPECTED_BLOCKER_CODES = (
    "BLOCKED_SOURCE_MISSING",
    "BLOCKED_EXECUTION_CHANNEL",
    "BLOCKED_WORKDIR_POLICY",
    "BLOCKED_APPROVAL_REQUIRED",
    "BLOCKED_PLAN_INCOMPLETE",
)
EXPECTED_RUN_STATE = {
    "schema": RUN_STATE_SCHEMA,
    "preflight_checks": [
        "runtime_online",
        "agent_runtime_match",
        "workdir_policy",
        "plan_present",
        "launcher_available",
        "hidden_execution_capable",
        "device_preflight",
    ],
    "schema_ref": RUN_STATE_FILE,
    "event_log": "receipts/events/gates-<YYYYMMDD>.jsonl",
    "stage_index": "receipts/stage-index.json",
    "done_by": "human-only",
    "landing_status": "in_review",
}
EXPECTED_STAGE_POLICY = {
    "id_source": "multica-stage-integer",
    "milestone": "not-supported",
    "barrier": "--until-children-done --stage N",
    "rollback": "per-stage",
    "delete_evidence": "forbidden",
}
EXPECTED_GATE_EVENTS = {
    "write_before_work": True,
    "progress_interval_minutes": 5,
    "watchdog_sample_seconds_min": 10,
    "watchdog_sample_seconds_max": 30,
    "missing_event": "BLOCKED",
}
EXPECTED_RECEIPT_LAYERS = {
    "formal": "receipts/run.receipt.json",
    "candidate": "receipts/candidates/",
    "evidence_gap": "receipts/evidence-gaps/",
    "join_key": "run_id",
    "reflow_source": "formal-only",
    "schema_change": "forbidden",
}

EXPECTED_KB_SYNC = {
    "enabled": True,
    "context_schema": "ark.kb-context.v1",
    "delivery_schema": "ark.delivery.v1",
    "query_dims": ["capability", "project", "device", "firmware", "profile"],
    "record_first": False,
    "refresh_points": [
        "stage_boundary",
        "every_30_minutes",
        "every_100_rounds",
        "registry_commit_change",
    ],
    "delivery_path": "receipts/delivery.json",
    "order": ["manifest", "receipt", "archive", "kb_record", "kb_query_back"],
    "query_back_top": 20,
    "no_kb_required_reasons": [
        "no-new-knowledge",
        "pure-inventory",
        "duplicate-of-existing:<entry_id>",
        "sensitive-not-indexable",
    ],
    "incomplete_code": "DELIVERY_INCOMPLETE",
    "layers": {
        "formal": "receipts/run.receipt.json",
        "candidate": "receipts/candidates/",
        "evidence_gap": "receipts/evidence-gaps/",
    },
    "candidate_may_drive_dispatch": False,
}
EXPECTED_KB_SYNC_ORDER = ["manifest", "receipt", "archive", "kb_record", "kb_query_back"]
KB_SYNC_HEADING = "## 16. 知识同步（事件驱动检查点）"
KB_CHECKPOINT_SCHEMA = "ark.checkpoint.v1"
KB_WAL_PREFIX = "receipts/outbox/"
KB_OUTBOX_INDEX = "receipts/outbox/outbox.json"
KB_IDEMPOTENCY_KEY = "sha256(issue_id|run_id|stage|event_seq|evidence_digest)"
KB_TRIGGERS = (
    "EXECUTION_CHECK",
    "STAGE_FINISHED",
    "BLOCKED",
    "FAIL",
    "NEEDS_REVIEW",
    "WATCHDOG_RUNNING_NO_EVIDENCE",
    "WATCHDOG_STATE_DRIFT",
    "WATCHDOG_TIMEOUT",
    "WATCHDOG_HEARTBEAT_LOST",
    "MANUAL_CANCEL",
    "RUNTIME_CRASH",
    "RECOVERY_REPLAY",
)
KB_FINAL_RECEIPT_ROLE = "aggregate-close-only"
KB_QUALITY_TIERS = {
    "formal": "evidence-complete",
    "candidate": "evidence-incomplete",
    "evidence_gap": "no-evidence",
}
KB_SCOPES = ("run", "node", "project", "device_family", "platform", "capability", "global")
KB_SCOPE_INHERIT = "near-to-far"
KB_CROSS_SCOPE_REFERENCE = "derived_from-applies_to-exceptions"
KB_PROMOTION = {
    "min_projects": 2,
    "promote_to": ["capability", "global"],
    "requires_review": True,
    "demotion": "add-applies_to-or-exceptions",
}
KB_OUTBOX_CLOSE_WHEN = (
    "all archive ok and kb ingest recorded-or-skipped and query-back pass-or-deferred"
)
KB_V2_TEMPLATE_TOKENS = (
    "ark.checkpoint.v1",
    "receipts/outbox/checkpoints-",
    "RECOVERY_REPLAY",
    "derived_from",
    "promotion",
    "evidence_gap",
    "aggregate",
    "聚合",
)
KB_SYNC_TOKENS = (
    "ark.kb-context.v1",
    "ark.delivery.v1",
    "receipts/delivery.json",
    "DELIVERY_INCOMPLETE",
    "NO_KB_REQUIRED",
    "kb_query_back_status",
    "receipts/evidence-gaps/",
) + KB_V2_TEMPLATE_TOKENS

TTS_LIBRARY_HEADING = "## 17. 1006 TTS 音频库"
TTS_LIBRARY_TOKENS = (
    TTS_LIBRARY_HEADING,
    "lookup_before_synth",
    "assets/tts/original",
    "work/audio",
    "derived_from_asset_id",
    "receipts/outbox",
    "BLOCKED_TTS_LIBRARY_UNREACHABLE",
    "asset-tools tts-library",
)
EXPECTED_TTS_LIBRARY = {
    "root": "/srv/storage/ark/artifacts/audio/tts-library",
    "index": "index.json",
    "canonical_tool": "asset-tools",
    "lookup_command": "asset-tools tts-library lookup --spec <spec.json>",
    "fetch_command": (
        "asset-tools tts-library fetch --spec <spec.json> --dest assets/tts/original --task-dir ."
    ),
    "publish_command": "asset-tools tts-library publish --staging <dir> --task-dir .",
    "synth_tool": "tts-runtime",
    "synth_engine": "lstts",
    "lookup_before_synth": True,
    "reuse_requires": [
        "quality=formal",
        "status=active",
        "shareable=true",
        "full_cache_key_match",
        "sha256_verified",
        "acl_allowed",
    ],
    "original_dir": "assets/tts/original",
    "derived_dir": "work/audio",
    "hardlink_forbidden": True,
    "derived_requires_asset_id": True,
    "outbox_dir": "receipts/outbox",
    "unreachable_action": "BLOCKED_TTS_LIBRARY_UNREACHABLE",
    "sensitive_default_shareable": False,
    "gc_requires_approval": True,
}

SAMPLE_EXECUTION_CHECK = {
    "schema": RUN_STATE_SCHEMA,
    "event": "EXECUTION_CHECK",
    "run_id": "20261112-150500_noah-66",
    "issue_id": "NOAH-66",
    "agent_id": "01f99187-e47b-4c68-afee-2da2a9819a49",
    "runtime_id": "54b47766-bdca-477a-85d0-825d2248ccac",
    "started": True,
    "stage": 15,
    "first_action": "读取任务目录 plan.md",
    "first_artifact": "plan.md",
    "preflight": {
        "status": "passed",
        "checks": {
            "runtime_online": True,
            "agent_runtime_match": True,
            "workdir_policy": True,
            "plan_present": True,
            "launcher_available": True,
            "hidden_execution_capable": True,
            "device_preflight": True,
        },
        "checked_at": "2026-11-12 15:04:59",
        "checker_version": "execution-check-v1",
    },
    "at": "2026-11-12 15:05:00",
}
SAMPLE_FINAL_RECEIPT = {
    "schema": RUN_STATE_SCHEMA,
    "event": "FINAL_RECEIPT",
    "run_id": "20261112-150500_noah-66",
    "issue_id": "NOAH-66",
    "stage": 15,
    "at": "2026-11-12 15:05:00",
    "result": "PASS",
    "receipt_path": "receipts/run.receipt.json",
    "receipt_sha256": "0" * 64,
    "archive_path": "<results-root>/bszT14/NOAH-66/<run_id>",
}
INVALID_SAMPLES = (
    # started=YES 但 run_id 为空：没有真实 run 不许声称在跑
    dict(SAMPLE_EXECUTION_CHECK, run_id=""),
    # 时间带时区标记：违反北京时间口径
    dict(SAMPLE_EXECUTION_CHECK, at="2026-11-12" + "T15:05:00" + "Z"),
    # 事件名与分支不一致
    dict(SAMPLE_FINAL_RECEIPT, event="EXECUTION_CHECK"),
    # stage 必须是正整数
    dict(SAMPLE_EXECUTION_CHECK, stage=0),
    # 回执文件哈希必须是 64 位小写十六进制
    dict(SAMPLE_FINAL_RECEIPT, receipt_sha256="deadbeef"),
)


def _event_problems(event: Dict[str, Any], spec: Dict[str, Any]) -> List[str]:
    """按 run-state.v1 的单个事件定义做最小校验（只用标准库）。"""
    problems: List[str] = []
    required = spec.get("required") or []
    for key in required:
        if key not in event:
            problems.append(f"缺少必填字段 {key}")
    props = spec.get("properties") or {}
    for key, rules in props.items():
        if key not in event:
            continue
        value = event[key]
        if "const" in rules and value != rules["const"]:
            problems.append(f"{key} 必须是 {rules['const']!r}")
        if "enum" in rules and value not in rules["enum"]:
            problems.append(f"{key} 不在枚举内: {value!r}")
        expected_type = rules.get("type")
        if expected_type == "string" and not isinstance(value, str):
            problems.append(f"{key} 必须是字符串")
        if expected_type == "string" and isinstance(value, str):
            min_length = rules.get("minLength")
            if isinstance(min_length, int) and len(value) < min_length:
                problems.append(f"{key} 至少 {min_length} 字符")
        if expected_type == "boolean" and not isinstance(value, bool):
            problems.append(f"{key} 必须是布尔值")
        if expected_type == "integer" and not isinstance(value, int):
            problems.append(f"{key} 必须是整数")
        if expected_type == "array":
            if not isinstance(value, list):
                problems.append(f"{key} 必须是数组")
            else:
                minimum = rules.get("minItems")
                if isinstance(minimum, int) and len(value) < minimum:
                    problems.append(f"{key} 至少 {minimum} 项")
                item_type = (rules.get("items") or {}).get("type")
                if item_type == "string" and not all(isinstance(item, str) for item in value):
                    problems.append(f"{key} 的每一项都必须是字符串")
                item_min = (rules.get("items") or {}).get("minLength")
                if isinstance(item_min, int) and not all(isinstance(item, str) and len(item) >= item_min for item in value):
                    problems.append(f"{key} 的每一项至少 {item_min} 字符")
        minimum = rules.get("minimum")
        if isinstance(minimum, int) and isinstance(value, int) and value < minimum:
            problems.append(f"{key} 不得小于 {minimum}")
        pattern = rules.get("pattern")
        if isinstance(pattern, str) and isinstance(value, str):
            if not re.match(pattern, value):
                problems.append(f"{key} 不符合模式 {pattern}")
        max_length = rules.get("maxLength")
        if isinstance(max_length, int) and isinstance(value, str) and len(value) > max_length:
            problems.append(f"{key} 过长")
    return problems


def _merge_envelope(spec: Dict[str, Any], envelope: Dict[str, Any]) -> Dict[str, Any]:
    """把信封字段并进单个事件定义：required 取并集，properties 里分支优先。"""
    if not envelope:
        return spec
    merged: Dict[str, Any] = dict(spec)
    required: List[str] = []
    for key in list(envelope.get("required") or []) + list(spec.get("required") or []):
        if key not in required:
            required.append(key)
    merged["required"] = required
    properties: Dict[str, Any] = {}
    properties.update(envelope.get("properties") or {})
    properties.update(spec.get("properties") or {})
    merged["properties"] = properties
    return merged


def _load_run_state(manifest: Dict[str, Any], base: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    entries = manifest.get("files") or []
    entry = next((item for item in entries if str(item.get("role", "")) == "schema"), None)
    path = base / (str(entry.get("path", "")) if entry is not None else RUN_STATE_FILE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"缺少 {RUN_STATE_FILE}"
    except OSError as exc:
        return None, f"读取失败: {exc}"
    except json.JSONDecodeError as exc:
        return None, f"不是合法 JSON: {exc}"
    if not isinstance(data, dict):
        return None, "顶层必须是对象"
    return data, None


def check_run_state(manifest: Dict[str, Any], base: Path) -> List[Check]:
    checks: List[Check] = []
    data, error = _load_run_state(manifest, base)
    if error is not None:
        checks.append(Check("run-state-schema", False, error))
        return checks

    assert data is not None
    marker_ok = data.get("x-ark-schema") == RUN_STATE_SCHEMA
    defs = data.get("$defs")
    defs = defs if isinstance(defs, dict) else {}
    envelope_spec = defs.get("envelope") if isinstance(defs.get("envelope"), dict) else {}
    event_defs: Dict[str, Dict[str, Any]] = {}
    for name, value in defs.items():
        if name == "envelope" or not isinstance(value, dict):
            continue
        event_name = ((value.get("properties") or {}).get("event") or {}).get("const")
        if isinstance(event_name, str):
            event_defs[event_name] = value
    defs_ok = sorted(event_defs) == sorted(RUN_STATE_EVENTS)
    checks.append(
        Check(
            "run-state-schema",
            marker_ok and defs_ok,
            f"x-ark-schema={data.get('x-ark-schema')} 事件数={len(event_defs)}"
            if marker_ok and defs_ok
            else f"标记={marker_ok} 事件定义={sorted(event_defs)}",
        )
    )
    checks.append(
        Check(
            "run-state-envelope",
            isinstance(defs.get("envelope"), dict)
            and all(key in (defs["envelope"].get("required") or []) for key in ENVELOPE_KEYS),
            "共有字段齐备" if isinstance(defs.get("envelope"), dict) else "缺少 envelope 定义",
        )
    )

    missing_envelope: List[str] = []
    for name, spec in event_defs.items():
        if name not in RUN_STATE_EVENTS:
            continue
        required = spec.get("required") or []
        for key in ENVELOPE_KEYS:
            if key not in required:
                missing_envelope.append(f"{name}.{key}")
    checks.append(
        Check(
            "run-state-event-required",
            not missing_envelope,
            "各事件必填字段齐备" if not missing_envelope else "缺少: " + ",".join(missing_envelope[:4]),
        )
    )

    samples = (("EXECUTION_CHECK", SAMPLE_EXECUTION_CHECK), ("FINAL_RECEIPT", SAMPLE_FINAL_RECEIPT))
    sample_problems: List[str] = []
    for name, sample in samples:
        spec = event_defs.get(name)
        if not isinstance(spec, dict):
            sample_problems.append(f"{name} 无定义")
            continue
        spec = _merge_envelope(spec, envelope_spec)
        for problem in _event_problems(sample, spec):
            sample_problems.append(f"{name}: {problem}")
    checks.append(
        Check(
            "run-state-sample-valid",
            not sample_problems,
            "示例事件通过" if not sample_problems else "示例事件失败: " + "; ".join(sample_problems[:3]),
        )
    )

    rejection_failures: List[str] = []
    for index, sample in enumerate(INVALID_SAMPLES, start=1):
        name = str(sample.get("event", ""))
        spec = event_defs.get(name)
        if not isinstance(spec, dict):
            rejection_failures.append(f"样本 {index} 事件无定义")
            continue
        if not _event_problems(sample, _merge_envelope(spec, envelope_spec)):
            rejection_failures.append(f"样本 {index} 应被拒绝却通过了")
    checks.append(
        Check(
            "run-state-sample-rejected",
            not rejection_failures,
            "反例全部被拒绝" if not rejection_failures else "; ".join(rejection_failures[:3]),
        )
    )

    entries = manifest.get("files") or []
    profile_entry = next((item for item in entries if str(item.get("role", "")) == "profile"), None)
    default: Dict[str, Any] = {}
    if profile_entry is not None:
        try:
            profile = json.loads((base / str(profile_entry.get("path", ""))).read_text(encoding="utf-8"))
            value = profile.get("default")
            default = value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            default = {}

    run_state = default.get("run_state") if isinstance(default.get("run_state"), dict) else {}
    mismatched_runs = {
        key: run_state.get(key) for key, expected in EXPECTED_RUN_STATE.items() if run_state.get(key) != expected
    }
    events = run_state.get("events")
    events_ok = isinstance(events, list) and sorted(events) == sorted(RUN_STATE_EVENTS)
    codes = run_state.get("blocker_codes")
    codes_ok = isinstance(codes, list) and sorted(codes) == sorted(EXPECTED_BLOCKER_CODES)
    checks.append(
        Check(
            "profile-run-state",
            not mismatched_runs and events_ok and codes_ok,
            "run_state 段自洽" if not mismatched_runs and events_ok and codes_ok else f"不符: {sorted(mismatched_runs)} events={events_ok} codes={codes_ok}",
        )
    )

    stage_policy = default.get("stage_policy") if isinstance(default.get("stage_policy"), dict) else {}
    mismatched_stage = {
        key: stage_policy.get(key)
        for key, expected in EXPECTED_STAGE_POLICY.items()
        if stage_policy.get(key) != expected
    }
    checks.append(
        Check(
            "profile-stage-policy",
            not mismatched_stage,
            "阶段策略自洽" if not mismatched_stage else f"不符: {sorted(mismatched_stage)}",
        )
    )

    gate_events = default.get("gate_events") if isinstance(default.get("gate_events"), dict) else {}
    mismatched_gate = {
        key: gate_events.get(key)
        for key, expected in EXPECTED_GATE_EVENTS.items()
        if gate_events.get(key) != expected
    }
    checks.append(
        Check(
            "profile-gate-events",
            not mismatched_gate,
            "门禁事件参数自洽" if not mismatched_gate else f"不符: {sorted(mismatched_gate)}",
        )
    )

    layers = default.get("receipt_layers") if isinstance(default.get("receipt_layers"), dict) else {}
    mismatched_layers = {
        key: layers.get(key) for key, expected in EXPECTED_RECEIPT_LAYERS.items() if layers.get(key) != expected
    }
    checks.append(
        Check(
            "profile-receipt-layers",
            not mismatched_layers,
            "回执分层自洽" if not mismatched_layers else f"不符: {sorted(mismatched_layers)}",
        )
    )

    text = ""
    template_entry = next((item for item in entries if str(item.get("role", "")) == "template"), None)
    if template_entry is not None:
        try:
            text = (base / str(template_entry.get("path", ""))).read_text(encoding="utf-8")
        except OSError:
            text = ""
    missing_tokens = [token for token in RUN_STATE_TOKENS if token not in text]
    heading_ok = RUN_STATE_HEADING in text
    checks.append(
        Check(
            "template-run-state",
            not missing_tokens and heading_ok,
            "状态机章节齐备" if not missing_tokens and heading_ok else f"缺少: {missing_tokens} heading={heading_ok}",
        )
    )
    return checks


def kb_checkpoint_problems(block: Dict[str, Any]) -> List[str]:
    """检查点契约（v2）：profile 的 checkpoint / 质量分层 / 作用域 / 提升 / 恢复标志。"""
    problems: List[str] = []
    checkpoint = block.get("checkpoint") if isinstance(block.get("checkpoint"), dict) else {}
    if checkpoint.get("event_schema") != KB_CHECKPOINT_SCHEMA:
        problems.append("checkpoint.event_schema")
    wal = str(checkpoint.get("wal", ""))
    if KB_WAL_PREFIX not in wal or "checkpoints-" not in wal:
        problems.append("checkpoint.wal")
    if checkpoint.get("index") != KB_OUTBOX_INDEX:
        problems.append("checkpoint.index")
    if checkpoint.get("append_only") is not True:
        problems.append("checkpoint.append_only")
    if checkpoint.get("idempotency_key") != KB_IDEMPOTENCY_KEY:
        problems.append("checkpoint.idempotency_key")
    if block.get("final_receipt_role") != KB_FINAL_RECEIPT_ROLE:
        problems.append("final_receipt_role")
    if block.get("quality_tiers") != KB_QUALITY_TIERS:
        problems.append("quality_tiers")
    if block.get("failure_can_be_formal") is not True:
        problems.append("failure_can_be_formal")
    if block.get("failure_outcome_forbidden") != "PASS":
        problems.append("failure_outcome_forbidden")
    if block.get("scopes") != list(KB_SCOPES):
        problems.append("scopes")
    if block.get("scope_inherit") != KB_SCOPE_INHERIT:
        problems.append("scope_inherit")
    if block.get("cross_scope_reference") != KB_CROSS_SCOPE_REFERENCE:
        problems.append("cross_scope_reference")
    if block.get("project_may_override_global") is not False:
        problems.append("project_may_override_global")
    if block.get("promotion") != KB_PROMOTION:
        problems.append("promotion")
    if block.get("outbox_close_when") != KB_OUTBOX_CLOSE_WHEN:
        problems.append("outbox_close_when")
    if block.get("recovery_scan") is not True:
        problems.append("recovery_scan")
    if block.get("recovery_scan_when") != "run-start":
        problems.append("recovery_scan_when")
    if block.get("query_back_per_checkpoint") is not True:
        problems.append("query_back_per_checkpoint")
    return problems


def check_kb_sync(manifest: Dict[str, Any], base: Path) -> List[Check]:
    """知识同步契约：profile 的 kb_sync 段与模板第 16 节双向对齐。"""
    checks: List[Check] = []
    entries = manifest.get("files") or []
    profile_entry = next((item for item in entries if str(item.get("role", "")) == "profile"), None)
    block: Dict[str, Any] = {}
    profile_error: Optional[str] = None
    if profile_entry is None:
        profile_error = "发布包里没有 profile"
    else:
        path = base / str(profile_entry.get("path", ""))
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
            default = profile.get("default") if isinstance(profile.get("default"), dict) else {}
            value = default.get("kb_sync")
            if isinstance(value, dict):
                block = value
            else:
                profile_error = "profile 缺少 kb_sync 段"
        except (OSError, json.JSONDecodeError) as exc:
            profile_error = f"profile 读取失败: {exc}"

    if profile_error is not None:
        checks.append(Check("profile-kb-sync", False, profile_error))
        checks.append(Check("profile-kb-sync-order", False, profile_error))
        checks.append(Check("kb-sync-profile", False, profile_error))
        checks.append(Check("kb-sync-triggers", False, profile_error))
    else:
        mismatched = {
            key: block.get(key) for key, expected in EXPECTED_KB_SYNC.items() if block.get(key) != expected
        }
        checks.append(
            Check(
                "profile-kb-sync",
                not mismatched,
                "kb_sync 段自洽" if not mismatched else f"不符: {sorted(mismatched)}",
            )
        )
        order = block.get("order")
        checks.append(
            Check(
                "profile-kb-sync-order",
                order == EXPECTED_KB_SYNC_ORDER,
                f"order={order}",
            )
        )
        problems = kb_checkpoint_problems(block)
        checks.append(
            Check(
                "kb-sync-profile",
                not problems,
                "检查点 / 质量分层 / 作用域 / 提升 / 恢复标志齐备"
                if not problems
                else f"不符: {problems}",
            )
        )
        triggers = block.get("triggers") if isinstance(block.get("triggers"), list) else []
        triggers_ok = tuple(triggers) == KB_TRIGGERS
        checks.append(
            Check(
                "kb-sync-triggers",
                triggers_ok,
                f"12 个触发器顺序{'正确' if triggers_ok else '不符'}: {triggers}",
            )
        )

    text = ""
    template_entry = next((item for item in entries if str(item.get("role", "")) == "template"), None)
    if template_entry is not None:
        try:
            text = (base / str(template_entry.get("path", ""))).read_text(encoding="utf-8")
        except OSError:
            text = ""
    missing_tokens = [token for token in KB_SYNC_TOKENS if token not in text]
    heading_ok = KB_SYNC_HEADING in text
    checks.append(
        Check(
            "template-kb-sync",
            not missing_tokens and heading_ok,
            "知识同步章节齐备"
            if not missing_tokens and heading_ok
            else f"缺少: {missing_tokens} heading={heading_ok}",
        )
    )
    missing_v2 = [token for token in KB_V2_TEMPLATE_TOKENS if token not in text]
    checks.append(
        Check(
            "kb-sync-template",
            not missing_v2 and heading_ok,
            "事件驱动检查点章节齐备"
            if not missing_v2 and heading_ok
            else f"缺少: {missing_v2} heading={heading_ok}",
        )
    )
    return checks


def check_tts_library(manifest: Dict[str, Any], base: Path) -> List[Check]:
    """TTS 音频库优先：profile 的 tts_library 段与模板第 17 节双向对齐。"""
    checks: List[Check] = []
    entries = manifest.get("files") or []
    profile_entry = next((item for item in entries if str(item.get("role", "")) == "profile"), None)
    block: Dict[str, Any] = {}
    profile_error: Optional[str] = None
    if profile_entry is None:
        profile_error = "发布包里没有 profile"
    else:
        path = base / str(profile_entry.get("path", ""))
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
            default = profile.get("default") if isinstance(profile.get("default"), dict) else {}
            value = default.get("tts_library")
            if isinstance(value, dict):
                block = value
            else:
                profile_error = "profile 缺少 tts_library 段"
        except (OSError, json.JSONDecodeError) as exc:
            profile_error = f"profile 读取失败: {exc}"

    if profile_error is not None:
        checks.append(Check("profile-tts-library", False, profile_error))
    else:
        mismatched = {
            key: block.get(key)
            for key, expected in EXPECTED_TTS_LIBRARY.items()
            if block.get(key) != expected
        }
        checks.append(
            Check(
                "profile-tts-library",
                not mismatched,
                "tts_library 段自洽" if not mismatched else f"不符: {sorted(mismatched)}",
            )
        )

    text = ""
    template_entry = next((item for item in entries if str(item.get("role", "")) == "template"), None)
    if template_entry is not None:
        try:
            text = (base / str(template_entry.get("path", ""))).read_text(encoding="utf-8")
        except OSError:
            text = ""
    missing_tokens = [token for token in TTS_LIBRARY_TOKENS if token not in text]
    heading_ok = TTS_LIBRARY_HEADING in text
    checks.append(
        Check(
            "template-tts-library",
            not missing_tokens and heading_ok,
            "TTS 音频库章节齐备"
            if not missing_tokens and heading_ok
            else f"缺少: {missing_tokens} heading={heading_ok}",
        )
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
    checks.extend(check_execution_channel(manifest, base))
    checks.extend(check_run_state(manifest, base))
    checks.extend(check_kb_sync(manifest, base))
    checks.extend(check_tts_library(manifest, base))
    return checks, manifest


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="verify-template.py",
        description="校验 NoahsArk 智能体模板发布包的完整性与安全性。",
    )
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parent), help="发布包所在目录")
    parser.add_argument("--manifest", default=None, help="manifest 路径，默认 <dir>/manifest.json")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    parser.add_argument("--profile", default=None, help="额外校验一个实例 profile（可为 profile 包装对象）")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    base = Path(args.dir).resolve()
    manifest_path = Path(args.manifest).resolve() if args.manifest else None

    try:
        checks, manifest = verify(base, manifest_path)
        if args.profile:
            source = json.loads(Path(args.profile).read_text(encoding="utf-8"))
            instance = source.get("profile", source) if isinstance(source, dict) else {}
            profile_entry = next(entry for entry in (manifest or {}).get("files", []) if entry.get("role") == "profile")
            schema = json.loads((base / profile_entry["path"]).read_text(encoding="utf-8"))
            checks.extend(check_profile_identity(instance, schema.get("roles", {}), schema.get("role_icons", {}), "instance"))
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
