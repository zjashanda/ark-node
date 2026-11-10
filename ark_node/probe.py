"""节点能力探测：输出 ``ark-node-capability.v1``。

采集内容
--------
* 工具链：python / git / ssh / scp / curl / tar / powershell / usb-tree，
  只保留短版本号，不携带多行原始输出。
* 资源：CPU、内存、任务盘剩余空间，取不到就给 ``null``，绝不崩溃。
* 环境变量：只比对 ``config/env-keys.json`` 里的**键名**，只输出缺失键名，
  **绝不输出任何值**。
* 网络：DNS 与 HTTPS 可达性（仅用于能力画像，不传业务数据）。

``--deep`` 额外采集 ``~/.ssh`` 下的**文件名**、监听端口、git 是否配置了
``user.email``（只输出布尔值）。
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import ssl
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import SCHEMA_CAPABILITY, __version__, identity, util

NETWORK_HOST = "github.com"
_VERSION_RE = re.compile(r"\d+(?:\.\d+){0,3}")
_PORT_RE = re.compile(r":(\d{1,5})\s")


# --------------------------------------------------------------------- 工具


def _find_exe(*names: str) -> Tuple[Optional[str], Optional[str]]:
    """按顺序查找可执行文件，返回 ``(名字, 绝对路径)``。"""
    for name in names:
        path = util.which(name)
        if path:
            return name, path
    return None, None


def _version_token(text: str) -> Optional[str]:
    """从版本输出里截取短版本号（如 ``2.43.0``）。"""
    match = _VERSION_RE.search(text or "")
    return match.group(0) if match else None


def _tool(names: Tuple[str, ...], want_version: bool = True) -> Dict[str, Any]:
    _name, path = _find_exe(*names)
    if not path:
        return {"present": False, "version": None} if want_version else {"present": False}
    if not want_version:
        return {"present": True}
    _rc, out, err = util.run_cmd([path, "--version"], timeout=8)
    return {"present": True, "version": _version_token(out) or _version_token(err)}


def _usb_tree_present() -> bool:
    if util.which("usb-tree") or util.which("usb_tree"):
        return True
    try:
        return (util.home_dir() / "skills" / "usb-tree").is_dir()
    except OSError:
        return False


def _probe_tools() -> Dict[str, Any]:
    return {
        "python": _tool(("python3", "python", "py")),
        "git": _tool(("git",)),
        "ssh": _tool(("ssh",)),
        "scp": _tool(("scp",), want_version=False),
        "curl": _tool(("curl",)),
        "tar": _tool(("tar",), want_version=False),
        "powershell": _tool(("powershell", "pwsh")),
        "usb_tree": {"present": _usb_tree_present()},
    }


# --------------------------------------------------------------------- 资源


def _linux_memory_bytes() -> Optional[float]:
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    return float(parts[1]) * 1024.0
    except (OSError, IndexError, ValueError):
        return None
    return None


def _windows_memory_bytes() -> Optional[float]:
    rc, out, _err = util.run_cmd(
        ["wmic", "ComputerSystem", "get", "TotalPhysicalMemory", "/value"], timeout=8
    )
    if rc == 0:
        match = re.search(r"TotalPhysicalMemory=(\d+)", out or "")
        if match:
            return float(match.group(1))
    _rc, out, _err = util.run_cmd(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory",
        ],
        timeout=8,
    )
    match = re.search(r"\d{6,}", out or "")
    return float(match.group(0)) if match else None


def _memory_total_gb() -> Optional[float]:
    value: Optional[float] = None
    try:
        if util.is_windows():
            value = _windows_memory_bytes()
        elif sys.platform == "darwin":
            rc, out, _err = util.run_cmd(["sysctl", "-n", "hw.memsize"], timeout=8)
            if rc == 0 and (out or "").strip():
                value = float(out.strip())
        else:
            value = _linux_memory_bytes()
    except (OSError, ValueError, TypeError):
        value = None
    if not value:
        return None
    return round(value / (1024.0 ** 3), 1)


def _probe_resources(task_root: Path) -> Dict[str, Any]:
    disk: Dict[str, Any] = {"path": str(task_root), "free_gb": None, "total_gb": None}
    try:
        target = task_root if task_root.exists() else Path.home()
        usage = shutil.disk_usage(str(target))
        disk["free_gb"] = round(usage.free / (1024.0 ** 3), 1)
        disk["total_gb"] = round(usage.total / (1024.0 ** 3), 1)
    except OSError:
        pass
    return {
        "cpu_count": os.cpu_count(),
        "memory_total_gb": _memory_total_gb(),
        "disk": disk,
    }


# --------------------------------------------------------------------- 环境


def _env_key_names() -> List[str]:
    """读取 ``config/env-keys.json`` 的键名清单（文件缺失即空）。"""
    path = util.repo_root() / "config" / "env-keys.json"
    if not path.is_file():
        return []
    try:
        data = util.read_json(path)
    except (OSError, ValueError):
        return []

    names: List[str] = []
    if isinstance(data, list):
        names = [str(item) for item in data]
    elif isinstance(data, dict):
        keys = data.get("keys")
        if isinstance(keys, list):
            names = [str(item) for item in keys]
        else:
            names = [str(key) for key in data.keys()]

    deduped: List[str] = []
    for name in names:
        clean = name.strip()
        if clean and clean not in deduped:
            deduped.append(clean)
    return deduped


def _env_file_present() -> bool:
    try:
        home = util.home_dir()
        return (home / "env").is_file() or (home / "env.json").is_file()
    except OSError:
        return False


def _probe_env() -> Dict[str, Any]:
    names = _env_key_names()
    version = (os.environ.get("ARK_ENV_VERSION") or "").strip() or None
    present = [name for name in names if name in os.environ]

    if version or present:
        source = "process"
    elif _env_file_present():
        source = "file"
    else:
        source = "absent"

    return {
        "version": version,
        "source": source,
        "missing_keys": [name for name in names if name not in os.environ],
        "key_count": len(names),
    }


# --------------------------------------------------------------------- 网络


def _dns_ok(host: str = NETWORK_HOST) -> bool:
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return True
    except OSError:
        return False


def _https_ok(host: str = NETWORK_HOST, timeout: float = 3.0) -> bool:
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host):
                return True
    except (OSError, ssl.SSLError, ValueError):
        return False


def _probe_network() -> Dict[str, bool]:
    return {"dns": _dns_ok(), "https": _https_ok()}


# --------------------------------------------------------------------- 深度


def _ssh_key_names() -> List[str]:
    """只返回 ``~/.ssh`` 下 id_* / *.pub 的**文件名**。"""
    names: List[str] = []
    try:
        for entry in (Path.home() / ".ssh").iterdir():
            if not entry.is_file():
                continue
            if entry.name.startswith("id_") or entry.name.endswith(".pub"):
                names.append(entry.name)
    except OSError:
        return []
    return sorted(names)


def _proc_net_ports() -> List[int]:
    ports = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, encoding="utf-8") as fh:
                next(fh, None)
                for line in fh:
                    parts = line.split()
                    if len(parts) < 4 or parts[3] != "0A":  # 0A == LISTEN
                        continue
                    _host, _sep, port_hex = parts[1].rpartition(":")
                    if port_hex:
                        ports.add(int(port_hex, 16))
        except (OSError, ValueError):
            continue
    return sorted(ports)


def _netstat_ports() -> List[int]:
    path = util.which("netstat")
    if not path:
        return []
    _rc, out, err = util.run_cmd([path, "-an"], timeout=8)
    ports = set()
    for line in f"{out}\n{err}".splitlines():
        if "LISTEN" not in line.upper():
            continue
        match = _PORT_RE.search(line + " ")
        if match:
            port = int(match.group(1))
            if 0 <= port <= 65535:
                ports.add(port)
    return sorted(ports)


def _listening_ports() -> List[int]:
    try:
        if util.is_windows() or sys.platform == "darwin":
            return _netstat_ports()
        return _proc_net_ports()
    except Exception:  # 端口探测属于尽力而为，任何异常都降级为空
        return []


def _git_email_set() -> bool:
    path = util.which("git")
    if not path:
        return False
    rc, out, _err = util.run_cmd([path, "config", "--global", "user.email"], timeout=8)
    return rc == 0 and bool((out or "").strip())


def _probe_deep() -> Dict[str, Any]:
    return {
        "ssh_keys": _ssh_key_names(),
        # 无仓库上下文，不猜测远端；保留空列表以稳定 schema。
        "git_remotes": [],
        "listening_ports": _listening_ports(),
        "git_version": _tool(("git",)).get("version"),
        "git_email_set": _git_email_set(),
    }


# --------------------------------------------------------------------- 汇总


def task_root_path() -> Path:
    return Path(os.environ.get("ARK_TASK_ROOT") or "~/.ark/tasks").expanduser()


def _writable(path: Path) -> bool:
    try:
        return path.exists() and os.access(str(path), os.W_OK)
    except OSError:
        return False


def collect(deep: bool = False) -> Dict[str, Any]:
    """采集能力画像，返回 ``ark-node-capability.v1`` 字典。"""
    node = identity.resolve()
    task_root = task_root_path()

    capabilities: Dict[str, Any] = {"network": _probe_network()}
    if deep:
        capabilities["deep"] = _probe_deep()

    return {
        "schema": SCHEMA_CAPABILITY,
        "ark_node_version": __version__,
        "generated_at": util.now_display(),
        "node": {
            "node_id": node.get("node_id"),
            "hostname": node.get("hostname"),
            "platform": node.get("platform"),
            "arch": node.get("arch"),
            "role": node.get("role"),
        },
        "tools": _probe_tools(),
        "resources": _probe_resources(task_root),
        "env": _probe_env(),
        "task_root": {
            "path": str(task_root),
            "exists": task_root.exists(),
            "writable": _writable(task_root),
        },
        "capabilities": capabilities,
    }


def run(args: Any) -> int:
    """``ark-node probe`` 入口：默认即机器可读 JSON。"""
    try:
        payload = collect(deep=bool(getattr(args, "deep", False)))
        out = getattr(args, "out", None)
        if out:
            util.atomic_write_json(out, payload, 0o600)
        util.emit(payload)
    except Exception as exc:  # 兜底：任何异常转成可读错误，不带 traceback
        util.info("probe: " + util.redact(str(exc)))
        return util.EXIT_FAIL
    return util.EXIT_OK
