"""节点身份解析：node_id 来源判定、平台识别与本地持久化。

优先级
------
``显式参数 > 环境变量 ARK_NODE_ID > ~/.config/ark/node.json > 主机名推导``。
来源写入 ``source`` 字段（``arg|env|file|hostname``），便于排查“为什么这台
机器叫这个名字”。

安全边界
--------
持久化文件只保存 node_id / hostname / platform / role / created_at，
**绝不**保存任何口令、token 或私钥；写入使用原子写并固定 600 权限。
"""

from __future__ import annotations

import os
import platform as _platform
import re
import socket
from pathlib import Path
from typing import Any, Dict, Optional

from . import util

SCHEMA_IDENTITY = "ark-node-identity.v1"
DEFAULT_ROLE = "node"

# node_id 允许的字符集；主机名里其余字符统一折叠为 "-"。
_INVALID_ID_CHARS = re.compile(r"[^a-z0-9._-]+")


def detect_platform() -> str:
    """返回 linux / windows / macos 之一。"""
    system = (_platform.system() or "").strip().lower()
    if system == "windows":
        return "windows"
    if system == "darwin":
        return "macos"
    if system == "linux":
        return "linux"
    # 未知系统按运行环境兜底，保证 schema 里的枚举始终有效。
    return "windows" if os.name == "nt" else "linux"


def detect_arch() -> str:
    return (_platform.machine() or "unknown").strip() or "unknown"


def state_path() -> Path:
    """身份文件位置：``$ARK_NODE_HOME/node.json``（默认 ~/.config/ark/node.json）。"""
    return util.home_dir() / "node.json"


def derive_node_id(hostname: str) -> str:
    """由主机名推导 node_id：小写、去域名、非法字符替换为 ``-``。"""
    text = (hostname or "").strip().lower()
    text = text.split(".", 1)[0]
    text = _INVALID_ID_CHARS.sub("-", text)
    text = text.strip("-")
    return text or "unknown-node"


def _read_state(path: Path) -> Dict[str, Any]:
    try:
        data = util.read_json(path)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _persist(
    path: Path,
    existing: Dict[str, Any],
    node_id: str,
    hostname: str,
    platform_name: str,
    role: str,
) -> None:
    """首次解析（source != file）时落盘；同一身份重复解析保持幂等。"""
    if existing.get("schema") == SCHEMA_IDENTITY and existing.get("node_id") == node_id:
        return
    created_at = str(existing.get("created_at") or "").strip() or util.now_display()
    util.ensure_private_dir(path.parent)
    util.atomic_write_json(
        path,
        {
            "schema": SCHEMA_IDENTITY,
            "node_id": node_id,
            "hostname": hostname,
            "platform": platform_name,
            "role": role,
            "created_at": created_at,
        },
        0o600,
    )


def resolve(node_id: Optional[str] = None, root: Optional[str] = None) -> Dict[str, Any]:
    """解析本机身份，返回 ``ark-node-identity.v1`` 字典。"""
    hostname = socket.gethostname()
    platform_name = detect_platform()
    arch = detect_arch()
    role = (os.environ.get("ARK_NODE_ROLE") or DEFAULT_ROLE).strip() or DEFAULT_ROLE

    path = state_path()
    state = _read_state(path)

    explicit = (node_id or "").strip()
    env_id = (os.environ.get("ARK_NODE_ID") or "").strip()
    file_id = str(state.get("node_id") or "").strip()

    if explicit:
        resolved_id, source = explicit, "arg"
    elif env_id:
        resolved_id, source = env_id, "env"
    elif file_id:
        resolved_id, source = file_id, "file"
    else:
        resolved_id, source = derive_node_id(hostname), "hostname"

    work_root = str(Path(root).expanduser()) if root else str(Path.home())

    result: Dict[str, Any] = {
        "schema": SCHEMA_IDENTITY,
        "node_id": resolved_id,
        "hostname": hostname,
        "platform": platform_name,
        "arch": arch,
        "role": role,
        "source": source,
        "root": work_root,
        "resolved_at": util.now_rfc3339(),
    }

    if source != "file":
        _persist(path, state, resolved_id, hostname, platform_name, role)

    return result


def run(args: Any) -> int:
    """``ark-node identity`` 入口。"""
    try:
        result = resolve(getattr(args, "node_id", None), getattr(args, "root", None))
    except Exception as exc:  # 不把 traceback 抛给调度器
        util.info("identity: " + util.redact(str(exc)))
        return util.EXIT_FAIL

    if getattr(args, "json", False):
        util.emit(result)
    else:
        print(f"node_id: {result['node_id']}")
        print(f"hostname: {result['hostname']}")
        print(f"platform: {result['platform']}")
    return util.EXIT_OK
