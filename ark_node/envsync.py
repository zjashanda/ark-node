"""节点环境变量同步（``ark-node env`` 子命令）。

三种作用域
==========
真源只有一个：ark-env HTTP 服务（``/ark/version.json`` 与 ``/ark/env.json``），
本模块只做客户端，不生产任何配置值。落盘方式按作用域分成三类：

``user``
    写 ``~/.config/ark/env``（mode 600），并在 ``~/.bashrc`` 追加一行
    ``[ -f ~/.config/ark/env ] && . ~/.config/ark/env``。只影响当前用户，
    不需要 root；改完必须新开 shell/进程才生效。

``system``
    写 ``/etc/ark/env``（mode 600）与 ``/etc/profile.d/ark-env.sh`` 钩子。
    影响整机所有用户，需要 root；权限不足直接报错，不尝试降级。

``machine``（仅 Windows）
    逐键调用 ``[Environment]::SetEnvironmentVariable(name, value, 'Machine')``，
    影响整机（含服务与 daemon）。必须先提权：检测不到管理员身份时返回
    ``EXIT_NEEDS_ELEVATION``，**绝不静默降级**到用户级——降级会让调用方
    误以为整机已就绪。

安全边界
========
口令只允许来自 ``--pass-stdin``、``ARK_HTTP_PASS`` 或口令文件，任何输出、
状态文件、日志都不出现口令；状态文件只记录**键名**（``changed_keys`` /
``skipped_keys``），永不记录值。目标 env 文件本身是凭据载体，因此固定
mode 600，目录 700。

幂等性
======
只重写发生变化的键：保留无关键与原始顺序，新键追加到末尾；内容完全没变
时不重写文件、不产生备份。``ARK_ENV_VERSION`` 由远端版本号单独维护。
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import SCHEMA_ENV_STATE, util

SCHEMA_SOURCES = "ark-env-sources.v1"
DEFAULT_USER = "ark"
VERSION_KEY = "ARK_ENV_VERSION"

SYSTEM_ENV_FILE = "/etc/ark/env"
SYSTEM_HOOK_FILE = "/etc/profile.d/ark-env.sh"
USER_HOOK_LINE = "[ -f ~/.config/ark/env ] && . ~/.config/ark/env"
SYSTEM_HOOK_LINE = "[ -f /etc/ark/env ] && . /etc/ark/env"

# 只认同 KEY=VALUE 形态的变量键，其余行（注释/空行/怪异写法）原样保留。
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Windows 机器级作用域相关的 PowerShell 片段（都是常量，值不拼进脚本字符串）。
_PS_ADMIN = (
    "([Security.Principal.WindowsPrincipal]"
    "[Security.Principal.WindowsIdentity]::GetCurrent())"
    ".IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)"
)
_PS_READ_MACHINE = "[Environment]::GetEnvironmentVariables('Machine') | ConvertTo-Json -Compress"

_PS_BIN = "powershell"


class EnvError(Exception):
    """带退出码的同步失败，由 :func:`run` 转成返回码，绝不抛 traceback。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------- 入口


def run(args: Any) -> int:
    """执行 ``ark-node env``，返回进程退出码（0 成功）。"""
    try:
        state = _execute(args)
    except EnvError as exc:
        util.info(f"ark-node: {exc.message}")
        return exc.code
    except OSError as exc:
        util.info(f"ark-node: 文件操作失败：{exc.strerror or exc}")
        return util.EXIT_FAIL

    if bool(getattr(args, "json", False)):
        util.emit(util.redact_obj(state))
    else:
        for line in _human_lines(state):
            sys.stdout.write(line + "\n")
    return util.EXIT_OK


def _execute(args: Any) -> Dict[str, Any]:
    scope = str(getattr(args, "scope", None) or "user")
    _check_scope(scope)

    home = util.home_dir()
    offline = getattr(args, "offline", None)

    if offline:
        base = "offline"
        version, updates, skipped = _read_offline(Path(str(offline)))
    else:
        base = _resolve_base(args)
        user, password, source = _resolve_credentials(args, home)
        if scope == "user" and source in ("stdin", "env"):
            _store_password(_auth_path(args, home), user, password)
        version, updates, skipped = _fetch_remote(base, user, password, _timeout(args))

    target = _target_path(scope, home)
    if scope == "machine":
        existing_map = _machine_env()
        existing_text = ""
    else:
        existing_text = _read_text(target)
        existing_map = _env_map(existing_text)

    local_before = existing_map.get(VERSION_KEY, "")
    changed = [(key, value) for key, value in updates if existing_map.get(key) != value]
    mode = "apply" if (getattr(args, "apply", False) and not getattr(args, "diff", False)) else "diff"

    if mode == "apply":
        if scope == "machine":
            for key, value in changed:
                _machine_set(key, value)
            _machine_set(VERSION_KEY, version)
        else:
            _apply_to_file(target, scope, existing_text, changed, version, home)

    state: Dict[str, Any] = {
        "schema": SCHEMA_ENV_STATE,
        "base": base,
        "remote_version": version,
        "local_version_before": local_before,
        "scope": scope,
        "mode": mode,
        "changed_keys": [key for key, _ in changed],
        "skipped_keys": skipped,
        "target_file": "machine" if scope == "machine" else str(target),
        "applied_at": util.now_display(),
    }

    util.ensure_private_dir(home)
    util.atomic_write_json(home / "env-state.json", state, 0o600)
    return state


def _check_scope(scope: str) -> None:
    if scope not in ("user", "system", "machine"):
        raise EnvError(util.EXIT_USAGE, f"未知作用域：{scope}（可选 user / system / machine）")
    if scope == "machine" and not util.is_windows():
        raise EnvError(
            util.EXIT_USAGE,
            "--scope machine 只用于 Windows 机器级环境变量；Linux 请改用 --scope system",
        )
    if scope == "machine" and not _is_elevated():
        raise EnvError(
            util.EXIT_NEEDS_ELEVATION,
            "--scope machine 需要管理员权限（当前未提权）；请以管理员身份重开，或改用 --scope user",
        )


# --------------------------------------------------------------------- 入口解析


def _resolve_base(args: Any) -> str:
    explicit = _clean(getattr(args, "base", None))
    if explicit:
        return explicit.rstrip("/")

    env_base = _clean(os.environ.get("ARK_ENV_BASE_URL"))
    if env_base:
        return env_base.rstrip("/")

    candidates = _source_candidates()
    if not candidates:
        raise EnvError(
            util.EXIT_USAGE,
            "缺少分发入口：请用 --base 指定 ark-env 服务地址（或设置 ARK_ENV_BASE_URL，"
            "或写入 ~/.config/ark/env-sources.json）",
        )

    timeout = _timeout(args)
    for candidate in candidates:
        base = candidate.rstrip("/")
        if _health_ok(base, timeout):
            return base
    raise EnvError(
        util.EXIT_NETWORK,
        "候选入口均不可达：请用 --base 指定可用的 ark-env 服务地址",
    )


def _source_candidates() -> List[str]:
    path = util.home_dir() / "env-sources.json"
    if not path.exists():
        return []
    try:
        data = util.read_json(path)
    except (OSError, ValueError):
        raise EnvError(util.EXIT_USAGE, "~/.config/ark/env-sources.json 无法解析为 JSON")
    if not isinstance(data, dict):
        raise EnvError(util.EXIT_USAGE, "~/.config/ark/env-sources.json 顶层必须是对象")
    raw = data.get("candidates")
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        value = _clean(item)
        if value:
            out.append(value)
    return out


def _health_ok(base: str, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=min(timeout, 10.0)) as resp:
            return 200 <= int(resp.getcode() or 0) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _auth_path(args: Any, home: Path) -> Path:
    explicit = getattr(args, "auth_file", None)
    if explicit:
        return Path(str(explicit)).expanduser()
    name = "auth.json" if util.is_windows() else "auth"
    return home / name


def _resolve_credentials(args: Any, home: Path) -> Tuple[str, str, str]:
    user = _clean(getattr(args, "user", None)) or _clean(os.environ.get("ARK_HTTP_USER")) or DEFAULT_USER

    if getattr(args, "pass_stdin", False):
        return user, _read_stdin_secret(), "stdin"

    password = os.environ.get("ARK_HTTP_PASS") or ""
    if password:
        return user, password, "env"

    path = _auth_path(args, home)
    stored = _read_auth_file(path)
    if not stored and util.is_windows():
        # Windows 上兼容旧版 ark-env.ps1 留下的裸口令文件。
        stored = _read_auth_file(home / "auth")
    if stored:
        return user, stored, "file"

    raise EnvError(
        util.EXIT_USAGE,
        "缺少 HTTP 口令：请用 --pass-stdin 从标准输入提供，或设置 ARK_HTTP_PASS，"
        "或写入口令文件（--auth-file）",
    )


def _read_stdin_secret() -> str:
    line = sys.stdin.readline()
    if line == "":
        raise EnvError(util.EXIT_USAGE, "stdin 未提供口令：--pass-stdin 需要从标准输入读取一行")
    return line.rstrip("\r\n")


def _read_auth_file(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return text
        if isinstance(data, dict):
            for name in ("pass", "password", "token"):
                value = data.get(name)
                if isinstance(value, str) and value:
                    return value
            return None
        return None
    return text


def _store_password(path: Path, user: str, password: str) -> None:
    """把口令落到权限 600 的文件；只写文件，永不打印。"""
    util.ensure_private_dir(path.parent)
    if util.is_windows() and path.suffix == ".json":
        util.atomic_write_json(path, {"user": user, "pass": password}, 0o600)
    else:
        util.atomic_write(path, password, 0o600)


# --------------------------------------------------------------------- 远端数据


def _fetch_remote(base: str, user: str, password: str, timeout: float) -> Tuple[str, List[Tuple[str, str]], List[Dict[str, str]]]:
    env_obj = _get_json(f"{base}/ark/env.json", user, password, timeout)
    version, updates, skipped = _parse_remote_env(env_obj)

    try:
        version_obj = _get_json(f"{base}/ark/version.json", user, password, timeout)
    except EnvError as exc:
        if exc.code == util.EXIT_FORBIDDEN:
            raise
        # 老部署可能没有 version.json，回退到 env.json 的 __version。
        if not version:
            raise
    else:
        if isinstance(version_obj, dict):
            remote = _clean(version_obj.get("version"))
            if remote:
                version = remote

    if not version:
        raise EnvError(util.EXIT_NETWORK, "远端未提供版本号：/ark/version.json 与 __version 均缺失")
    return version, updates, skipped


def _read_offline(path: Path) -> Tuple[str, List[Tuple[str, str]], List[Dict[str, str]]]:
    if not path.exists():
        raise EnvError(util.EXIT_NETWORK, f"离线快照不存在：{path}")
    try:
        data = util.read_json(path)
    except (OSError, ValueError):
        raise EnvError(util.EXIT_NETWORK, f"离线快照无法解析为 JSON：{path}")
    return _parse_remote_env(data)


def _parse_remote_env(data: Any) -> Tuple[str, List[Tuple[str, str]], List[Dict[str, str]]]:
    if not isinstance(data, dict):
        raise EnvError(util.EXIT_NETWORK, "env.json 顶层必须是 JSON 对象")

    version = _clean(data.get("__version")) or ""
    updates: List[Tuple[str, str]] = []
    skipped: List[Dict[str, str]] = []

    for raw_key, raw_value in data.items():
        key = str(raw_key)
        if key.startswith("__"):
            skipped.append({"key": key, "reason": "internal_prefix"})
            continue
        if not isinstance(raw_value, str):
            skipped.append({"key": key, "reason": "not_a_string"})
            continue
        if not raw_value.strip():
            skipped.append({"key": key, "reason": "empty_value"})
            continue
        updates.append((key, raw_value))

    return version, updates, skipped


def _get_json(url: str, user: str, password: str, timeout: float) -> Any:
    text = _http_get(url, user, password, timeout)
    try:
        return json.loads(text)
    except ValueError:
        raise EnvError(util.EXIT_NETWORK, f"响应不是合法 JSON：{url}")


def _http_get(url: str, user: str, password: str, timeout: float) -> str:
    request = urllib.request.Request(url, method="GET")
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    request.add_header("Authorization", f"Basic {token}")
    request.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.read().decode("utf-8-sig", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise EnvError(
                util.EXIT_FORBIDDEN,
                f"认证失败（HTTP {exc.code}）：请检查用户名与口令",
            )
        raise EnvError(util.EXIT_NETWORK, f"请求失败：HTTP {exc.code} {url}")
    except (urllib.error.URLError, OSError) as exc:
        raise EnvError(util.EXIT_NETWORK, f"网络错误：{_reason(exc)} {url}")


def _reason(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)
    return str(reason) if reason is not None else exc.__class__.__name__


# --------------------------------------------------------------------- 文件解析


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8-sig")


def _line_key(line: str) -> Optional[str]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key = stripped.split("=", 1)[0].strip()
    return key if _KEY_RE.match(key) else None


def _env_map(text: str) -> Dict[str, str]:
    """按首次出现解析 KEY=VALUE；注释、空行、重复键都只取第一份值。"""
    found: Dict[str, str] = {}
    for line in text.splitlines():
        key = _line_key(line)
        if key is None or key in found:
            continue
        found[key] = line.strip().split("=", 1)[1]
    return found


def merge_env(text: str, updates: List[Tuple[str, str]]) -> str:
    """按需改写：变化键就地替换，新键追加，其余行（含注释/空行）原样保留。"""
    lines = text.splitlines()
    index: Dict[str, int] = {}
    keep = [True] * len(lines)
    for position, line in enumerate(lines):
        key = _line_key(line)
        if key is None:
            continue
        if key in index:
            keep[position] = False  # 重复键在改写时收敛到第一处
        else:
            index[key] = position

    appended: List[str] = []
    for key, value in updates:
        row = f"{key}={value}"
        if key in index:
            lines[index[key]] = row
        else:
            appended.append(row)

    out = [line for position, line in enumerate(lines) if keep[position]] + appended
    if not out:
        return ""
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------- 落盘


def _target_path(scope: str, home: Path) -> Path:
    if scope == "system":
        return Path(SYSTEM_ENV_FILE)
    return home / "env"


def _apply_to_file(
    target: Path,
    scope: str,
    existing_text: str,
    changed: List[Tuple[str, str]],
    version: str,
    home: Path,
) -> None:
    merged = merge_env(existing_text, changed)
    merged = merge_env(merged, [(VERSION_KEY, version)])

    if merged != existing_text:
        if existing_text and target.exists():
            _write_backup(home, existing_text)
        _write_target(target, merged, scope)

    if scope == "system":
        try:
            util.atomic_write(Path(SYSTEM_HOOK_FILE), SYSTEM_HOOK_LINE + "\n", 0o600)
        except PermissionError:
            raise EnvError(util.EXIT_FORBIDDEN, f"写入 {SYSTEM_HOOK_FILE} 需要 root 权限")
    else:
        _ensure_line(_rc_path(home), USER_HOOK_LINE)


def _write_target(target: Path, text: str, scope: str) -> None:
    try:
        if scope == "user":
            util.ensure_private_dir(target.parent)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
        util.atomic_write(target, text, 0o600)
    except PermissionError:
        hint = "（system 作用域需要 root 权限）" if scope == "system" else ""
        raise EnvError(util.EXIT_FORBIDDEN, f"写入 {target} 失败：权限不足{hint}")
    except OSError as exc:
        raise EnvError(util.EXIT_FAIL, f"写入 {target} 失败：{exc.strerror or exc}")


def _write_backup(home: Path, text: str) -> None:
    util.ensure_private_dir(home)
    path = home / f"env-backup.{util.now_stamp()}.bak"
    util.atomic_write(path, text, 0o600)


def _rc_path(home: Path) -> Path:
    """``~/.bashrc`` 的位置。

    设置 ``ARK_NODE_HOME``（测试/沙箱）时，把它视为 ``<用户主目录>/.config/ark``，
    于是 rc 文件落在 ``<ARK_NODE_HOME 的父目录>/.bashrc``，绝不碰真实家目录。
    """
    override = os.environ.get("ARK_NODE_HOME")
    if override:
        return Path(override).expanduser().parent / ".bashrc"
    return Path.home() / ".bashrc"


def _ensure_line(path: Path, line: str) -> bool:
    """保证 ``line`` 恰好出现一次；已存在则不动，返回是否新增。"""
    text = _read_text(path)
    if any(item.strip() == line for item in text.splitlines()):
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    mode = 0o644
    if path.exists():
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            pass
    util.atomic_write(path, text + line + "\n", mode)
    return True


# --------------------------------------------------------------------- Windows


def _is_elevated() -> bool:
    code, out, _err = util.run_cmd([_PS_BIN, "-NoProfile", "-Command", _PS_ADMIN], timeout=20.0)
    return code == 0 and out.strip().lower() == "true"


def _machine_env() -> Dict[str, str]:
    code, out, _err = util.run_cmd(
        [_PS_BIN, "-NoProfile", "-NonInteractive", "-Command", _PS_READ_MACHINE],
        timeout=30.0,
    )
    if code != 0 or not out.strip():
        return {}
    try:
        data = json.loads(out)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): "" if value is None else str(value) for key, value in data.items()}


def _machine_set(key: str, value: str) -> None:
    script = "[Environment]::SetEnvironmentVariable('{}', '{}', 'Machine')".format(
        _ps_quote(key), _ps_quote(value)
    )
    code, _out, _err = util.run_cmd(
        [_PS_BIN, "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=30.0,
    )
    if code != 0:
        raise EnvError(util.EXIT_FAIL, f"设置机器级变量 {key} 失败（退出码 {code}）")


def _ps_quote(value: str) -> str:
    return str(value).replace("'", "''")


# --------------------------------------------------------------------- 输出


def _human_lines(state: Dict[str, Any]) -> List[str]:
    verb = "would set" if state["mode"] == "diff" else "set"
    lines = [f"{verb}: {key}" for key in state["changed_keys"]]
    if state["mode"] == "diff":
        lines.append("（预演；加 --apply 才写入）")
    lines.append(
        "remote version={} local={} changed={}".format(
            state["remote_version"],
            state["local_version_before"],
            len(state["changed_keys"]),
        )
    )
    return lines


# --------------------------------------------------------------------- 杂项


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _timeout(args: Any) -> float:
    raw = getattr(args, "timeout", None)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 10.0
    return value if value > 0 else 10.0
