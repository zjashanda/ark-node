"""共享工具：时间契约、退出码、JSON 读写、脱敏、子进程执行。

时间契约（全仓库唯一口径）
--------------------------
对外展示一律使用 **北京时间、无时区后缀**：``YYYY-MM-DD HH:MM:SS``。
机器可读字段使用 RFC3339（``tz`` 或 ``Z``），文件名使用
``YYYYMMDD-HHMMSS``。三种形态都从同一个 ``now_local()`` 推导，避免节点
时区不同导致台账对不上。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------- 退出码

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_NO_SOURCE = 3
EXIT_FORBIDDEN = 4
EXIT_NETWORK = 5
EXIT_NEEDS_ELEVATION = 6

EXIT_CODES = {
    EXIT_OK: "OK",
    EXIT_FAIL: "FAILED",
    EXIT_USAGE: "USAGE",
    EXIT_NO_SOURCE: "NO_SOURCE",
    EXIT_FORBIDDEN: "FORBIDDEN",
    EXIT_NETWORK: "NETWORK",
    EXIT_NEEDS_ELEVATION: "NEEDS_ELEVATION",
}

BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")

# 北京时间无时区展示格式 / 机器可读 RFC3339 / 文件名格式
FMT_DISPLAY = "%Y-%m-%d %H:%M:%S"
FMT_RFC3339 = "%Y-%m-%dT%H:%M:%S%z"
FMT_STAMP = "%Y%m%d-%H%M%S"

# --------------------------------------------------------------------- 时间


def now_local() -> datetime:
    """当前时间（固定按北京时间换算，不依赖节点本地时区）。"""
    return datetime.now(timezone.utc).astimezone(BEIJING)


def now_display() -> str:
    return now_local().strftime(FMT_DISPLAY)


def now_rfc3339() -> str:
    return now_local().strftime(FMT_RFC3339)


def now_stamp() -> str:
    return now_local().strftime(FMT_STAMP)


def parse_display(text: str) -> datetime:
    """解析北京时间无时区字符串，返回带 +08:00 的 datetime。"""
    naive = datetime.strptime(text.strip(), FMT_DISPLAY)
    return naive.replace(tzinfo=BEIJING)


# --------------------------------------------------------------------- 输出


def emit(obj: Any) -> None:
    """JSON 输出（保证 UTF-8、中文不转义、稳定缩进）。"""
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def info(msg: str) -> None:
    sys.stderr.write(f"{msg}\n")


def die(msg: str, code: int = EXIT_USAGE) -> "None":
    sys.stderr.write(f"ark-node: {msg}\n")
    raise SystemExit(code)


# --------------------------------------------------------------------- 文件


def read_json(path: Path | str) -> Any:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def atomic_write(path: Path | str, data: str, mode: int = 0o600) -> Path:
    """原子写：先写同目录临时文件再 rename，避免半写状态。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".ark-node-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return target


def atomic_write_json(path: Path | str, obj: Any, mode: int = 0o600) -> Path:
    return atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", mode)


def sha256_file(path: Path | str, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def human_bytes(n: int | float) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{value:.1f}TB"


# --------------------------------------------------------------------- 脱敏

# 任何写入日志/JSON 的自由文本都要过这里：命中即替换为 <redacted>。
_SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),               # GitHub token
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),             # GitHub fine-grained PAT
    re.compile(r"(?i)\bLTAI[A-Za-z0-9]{12,}"),               # 阿里云 AK
    re.compile(r"(?i)\b(xox[baprs]-[A-Za-z0-9-]{10,})"),     # Slack
    re.compile(r"(?i)\b(?:password|passwd|pass|token|secret|access_key_secret)\s*[=:]\s*\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b[A-Za-z0-9._%+-]+:[^@\s/]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),  # user:pass@host
]


def redact(text: str) -> str:
    out = str(text)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("<redacted>", out)
    return out


def redact_obj(value: Any) -> Any:
    """递归脱敏；键名像凭据的直接把值换掉，值里的 token 形态也替换。"""
    if isinstance(value, dict):
        clean = {}
        for key, val in value.items():
            if looks_secret_key(str(key)):
                clean[key] = "<redacted>" if val else val
            else:
                clean[key] = redact_obj(val)
        return clean
    if isinstance(value, list):
        return [redact_obj(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


SECRET_KEY_HINT = (
    "password", "passwd", "pass", "token", "secret", "credential",
    "private_key", "access_key", "apikey", "api_key", "auth",
)


def looks_secret_key(name: str) -> bool:
    low = name.lower()
    return any(hint in low for hint in SECRET_KEY_HINT)


# --------------------------------------------------------------------- 进程


def is_windows() -> bool:
    return os.name == "nt"


def which(name: str) -> str | None:
    return shutil.which(name)


def run_cmd(
    cmd: Iterable[str],
    timeout: float = 15.0,
    cwd: str | None = None,
    env: dict | None = None,
) -> tuple[int, str, str]:
    """执行命令并返回 (rc, stdout, stderr)。

    绝不使用 shell=True；超时返回 124，缺少可执行文件返回 127，
    输出统一按 utf-8 宽松解码（Windows 目标机常见 GBK/CP936 不再崩）。
    """
    argv = [str(part) for part in cmd]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            cwd=cwd,
            env=env if env is not None else os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout:.0f}s"
    except FileNotFoundError as exc:
        return 127, "", f"missing binary: {exc}"
    except OSError as exc:
        return 126, "", f"exec error: {exc}"
    return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------- 路径

DEFAULT_HOME = Path(os.environ.get("ARK_NODE_HOME") or (Path.home() / ".config" / "ark"))


def home_dir() -> Path:
    return Path(os.environ.get("ARK_NODE_HOME") or (Path.home() / ".config" / "ark")).expanduser()


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def ensure_private_dir(path: Path | str) -> Path:
    target = Path(path).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    if not is_windows():
        try:
            os.chmod(target, 0o700)
        except OSError:
            pass
    return target


def rel_or_abs(path: Path | str) -> str:
    """日志里优先给出仓库相对路径，避免把本机绝对路径写进公开台账。"""
    p = Path(path).resolve()
    try:
        return str(p.relative_to(repo_root()))
    except ValueError:
        return str(p)
