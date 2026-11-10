"""ark-mesh：节点到节点的制品取件通道（status / index / fetch / archive）。

职责边界
--------
本模块**只做节点之间的制品搬运与完整性校验**，不承载任何业务逻辑：
它不知道制品代表什么、不解析业务元数据、不决定取哪一版、也不触发下游流程。
业务语义一律留在调用方（任务编排、发布脚本、人工操作），这里只回答
“某个已登记的制品能不能从某台节点取到，取到的字节是否与台账一致”。

子命令
------
    ark-node fetch status  [--json] [--timeout S] [--config PATH]
    ark-node fetch index   --name X [--project P] [--version V] [--sha256 H]
                           [--node N --path PATH] [--remove-node N]
    ark-node fetch fetch   --name X --dest D [--dest-node N] [--timeout S] [--force]
    ark-node fetch fetch   --source NODE --path P --dest D [--dest-node N]
    ark-node fetch archive --name X --to-node N [--version V] [--project P]

设计口径
--------
* 来源优先级：本地缓存 → 台账内各来源（按 priority）→ 全部失败才报 BLOCKED；
* 每次成功写 ``artifact-manifest.json``；
* 失败写 ``fetch-failure-report.json``，逐条列出尝试过的来源与错误，
  不允许只回一句“本地没有”；
* 下载后 SHA-256 与期望值不符时，文件移入 ``<home>/quarantine/`` 隔离，
  附带 ``.sha256-mismatch`` 后缀与 ``.json`` 旁注，绝不删除用户数据；
* 所有对外 JSON 先经 :func:`ark_node.util.redact_obj` 脱敏再落盘。

凭据
----
只使用 ``pass_env`` 间接引用：节点口令从**配置里命名的环境变量**读取，
sshpass 只消费 ``SSHPASS``；本模块绝不打印、回显、写入或持久化口令值，
错误文本只允许出现环境变量名。

标准库实现，兼容 Python 3.8。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from . import util

# --------------------------------------------------------------------- 常量

SCHEMA = "ark-mesh-sources.v1"
MANIFEST_SCHEMA = "ark-artifact-manifest.v1"
FAILURE_SCHEMA = "ark-fetch-failure.v1"
MANIFEST_NAME = "artifact-manifest.json"
FAILURE_NAME = "fetch-failure-report.json"

# 缺配置时指向仓库内的脱敏示例（相对仓库根）
EXAMPLE_CONFIG = "config/mesh-sources.example.json"

PASSWORD_AUTHS = ("sshpass-env", "password")
DEFAULT_PASS_ENV = "ARK_NODE_SSH_PASS"

# 凭据类文件名/路径一律拒绝搬运（宁可报错，不碰密钥）
FORBIDDEN = re.compile(
    r"(^|[./\\_\-\s])("
    r"\.env(\..*)?|env|password|passwd|pass|token|secret|credential[s]?|"
    r"key|keys|pem|p12|pfx|ppk|id_rsa|id_ed25519|npmrc|netrc|cookie|cookies|"
    r"auth\.json|git-credentials"
    r")([./\\_\-\s]|$)",
    re.IGNORECASE,
)

# 连通性类失败：用于把“全部来源都连不上”映射到 EXIT_NETWORK
_NETWORK_RESULTS = frozenset({"unreachable", "copy_failed"})


class MissingPass(Exception):
    """节点需要口令环境变量，但当前进程没有设置。异常文本只含变量名。"""


# --------------------------------------------------------------------- 配置


def default_config_path() -> Path:
    """默认来源索引：``<home>/.config/ark/mesh-sources.json``。

    ``util.home_dir()`` 已经表达 ``~/.config/ark``（并尊重 ``ARK_NODE_HOME``），
    所以这里不再硬编码任何兜底主机。
    """
    return util.home_dir() / "mesh-sources.json"


def resolve_config_path(explicit: Optional[str] = None) -> Path:
    """配置优先级：显式 ``--config`` → ``ARK_MESH_CONFIG`` → 默认路径。"""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("ARK_MESH_CONFIG")
    if env:
        return Path(env).expanduser()
    return default_config_path()


def load_config(path: Optional[str]) -> dict:
    cfg_path = resolve_config_path(path)
    if not cfg_path.exists():
        util.die(
            f"来源索引不存在: {cfg_path}"
            f"（复制 {EXAMPLE_CONFIG} 后按实际环境填写，或用 --config / ARK_MESH_CONFIG 指定）",
            util.EXIT_USAGE,
        )
    try:
        cfg = util.read_json(cfg_path)
    except (OSError, ValueError) as exc:
        util.die(f"来源索引无法解析: {cfg_path}: {exc}", util.EXIT_USAGE)
    if not isinstance(cfg, dict):
        util.die(f"来源索引顶层必须是对象: {cfg_path}", util.EXIT_USAGE)
    if cfg.get("schema") != SCHEMA:
        util.die(f"来源索引 schema 不是 {SCHEMA}: {cfg_path}", util.EXIT_USAGE)
    cfg["_path"] = str(cfg_path)
    cfg.setdefault("nodes", {})
    cfg.setdefault("artifacts", {})
    # sshpass 只认 SSHPASS；由节点的 pass_env 映射过来，口令仍只存在于环境变量里
    for node in cfg["nodes"].values():
        if node.get("auth") in PASSWORD_AUTHS:
            env_name = node.get("pass_env") or DEFAULT_PASS_ENV
            src = os.environ.get(env_name)
            if src and not os.environ.get("SSHPASS"):
                os.environ["SSHPASS"] = src
    return cfg


def save_config(cfg: dict) -> None:
    path = Path(cfg["_path"])
    payload = {key: value for key, value in cfg.items() if key != "_path"}
    util.atomic_write_json(path, util.redact_obj(payload))


# --------------------------------------------------------------------- 传输


def node_spec(cfg: dict, name: str) -> dict:
    node = cfg["nodes"].get(name)
    if not node:
        existing = ", ".join(sorted(cfg["nodes"])) or "（空）"
        util.die(f"来源索引里没有节点 {name}；已有: {existing}", util.EXIT_USAGE)
    return node


def _uses_password(node: dict) -> bool:
    return node.get("auth") in PASSWORD_AUTHS


def _pass_env_name(node: dict) -> str:
    return node.get("pass_env") or DEFAULT_PASS_ENV


def is_windows(node: dict) -> bool:
    """目标节点是否 Windows（与本地平台无关）。"""
    return str(node.get("kind", "linux")).lower() == "windows"


def ssh_base(node: dict) -> list:
    batch = "no" if _uses_password(node) else "yes"
    return [
        "ssh",
        "-o",
        f"BatchMode={batch}",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=no",
        "-p",
        str(node.get("port", 22)),
    ]


def remote_shell(cfg: dict, name: str) -> list:
    """返回执行远端命令的前缀（ssh / sshpass+ssh）。"""
    node = node_spec(cfg, name)
    cmd = list(ssh_base(node))
    if _uses_password(node):
        env_name = _pass_env_name(node)
        if not os.environ.get(env_name):
            raise MissingPass(f"节点 {name} 需要环境变量 {env_name}（本进程未设置）")
        cmd = ["sshpass", "-e"] + cmd
    return cmd + [f"{node['user']}@{node['host']}"]


def remote_copy_prefix(cfg: dict, name: str) -> list:
    node = node_spec(cfg, name)
    batch = "no" if _uses_password(node) else "yes"
    cmd = [
        "scp",
        "-o",
        f"BatchMode={batch}",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=no",
        "-P",
        str(node.get("port", 22)),
    ]
    if _uses_password(node):
        env_name = _pass_env_name(node)
        if not os.environ.get(env_name):
            raise MissingPass(f"节点 {name} 需要环境变量 {env_name}（本进程未设置）")
        cmd = ["sshpass", "-e"] + cmd
    return cmd


def ps_encoded(script: str) -> str:
    """把 PowerShell 脚本编码成 ``-EncodedCommand``，避免引号转义地狱。"""
    blob = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return f"powershell -NoProfile -NonInteractive -EncodedCommand {blob}"


def shq(value: str) -> str:
    """POSIX shell 单引号转义。"""
    return "'" + value.replace("'", "'\\''") + "'"


def _clean_remote_err(text: str) -> str:
    return re.sub(r"CLIXML|<Objs.*", "", text, flags=re.S).strip()


def _last_line(text: str, default: str) -> str:
    for line in reversed((text or "").splitlines()):
        if line.strip():
            return line.strip()[:200]
    return default


def remote_hash(cfg: dict, name: str, path: str, timeout: float) -> tuple:
    """返回 (sha256 | None, 错误描述)。"""
    node = node_spec(cfg, name)
    if is_windows(node):
        script = (
            f"if (Test-Path -LiteralPath '{path}') "
            f"{{ (Get-FileHash -LiteralPath '{path}' -Algorithm SHA256).Hash }} "
            "else { Write-Output 'ARKMESH_MISSING' }"
        )
        rc, out, err = util.run_cmd(remote_shell(cfg, name) + [ps_encoded(script)], timeout)
        text = out.strip()
        err = _clean_remote_err(err)
        if rc != 0:
            return None, f"hash 失败(rc={rc}): {(err or text)[:200]}"
        if "ARKMESH_MISSING" in text:
            return None, "源文件不存在"
    else:
        script = (
            f"if [ -f {shq(path)} ]; then sha256sum {shq(path)} | cut -d' ' -f1; "
            f"elif [ -d {shq(path)} ]; then echo ARKMESH_ISDIR; else echo ARKMESH_MISSING; fi"
        )
        rc, out, err = util.run_cmd(remote_shell(cfg, name) + [script], timeout)
        text = out.strip()
        if rc != 0:
            return None, f"hash 失败(rc={rc}): {(_clean_remote_err(err) or text)[:200]}"
        if "ARKMESH_MISSING" in text:
            return None, "源文件不存在"
        if "ARKMESH_ISDIR" in text:
            return None, "路径是目录"
    match = re.search(r"\b([0-9A-Fa-f]{64})\b", text)
    if not match:
        return None, f"无法解析 hash 输出: {text[:120]}"
    return match.group(1).lower(), ""


def ensure_dir(cfg: dict, name: str, path: str, timeout: float) -> tuple:
    node = node_spec(cfg, name)
    if is_windows(node):
        script = (
            f"New-Item -ItemType Directory -Force -Path '{path}' | Out-Null; "
            "Write-Output 'ARKMESH_OK'"
        )
        cmd = remote_shell(cfg, name) + [ps_encoded(script)]
    else:
        cmd = remote_shell(cfg, name) + [f"mkdir -p {shq(path)} && echo ARKMESH_OK"]
    rc, out, err = util.run_cmd(cmd, timeout)
    if rc != 0 or "ARKMESH_OK" not in out:
        clean = _clean_remote_err(err)
        return False, f"建目录失败(rc={rc}): {(clean or out)[:200]}"
    return True, ""


def probe_node(cfg: dict, name: str, timeout: float) -> tuple:
    node = node_spec(cfg, name)
    started = time.time()
    try:
        return _probe_node(cfg, name, node, timeout, started)
    except MissingPass as exc:
        return False, f"SKIP: {exc}", time.time() - started


def _probe_node(cfg: dict, name: str, node: dict, timeout: float, started: float) -> tuple:
    rc, out, err = util.run_cmd(remote_shell(cfg, name) + ["hostname"], timeout)
    elapsed = time.time() - started
    if rc != 0:
        clean = _clean_remote_err(err)
        detail = _last_line(clean or out, f"rc={rc}")
        return False, detail, elapsed
    return True, (out.strip().splitlines()[0] if out.strip() else ""), elapsed


def scp_fetch(cfg: dict, name: str, remote_path: str, local_dest: Path, timeout: float) -> tuple:
    node = node_spec(cfg, name)
    local_dest.parent.mkdir(parents=True, exist_ok=True)
    remote = remote_path.replace("\\", "/") if is_windows(node) else remote_path
    cmd = remote_copy_prefix(cfg, name) + [
        f"{node['user']}@{node['host']}:{remote}",
        str(local_dest),
    ]
    rc, out, err = util.run_cmd(cmd, timeout)
    if rc != 0:
        clean = _clean_remote_err(err)
        return False, (clean or out).strip()[:300] or f"rc={rc}"
    return True, ""


def scp_push(cfg: dict, name: str, local_src: Path, remote_path: str, timeout: float) -> tuple:
    node = node_spec(cfg, name)
    remote = remote_path.replace("\\", "/") if is_windows(node) else remote_path
    cmd = remote_copy_prefix(cfg, name) + [
        str(local_src),
        f"{node['user']}@{node['host']}:{remote}",
    ]
    rc, out, err = util.run_cmd(cmd, timeout)
    if rc != 0:
        clean = _clean_remote_err(err)
        return False, (clean or out).strip()[:300] or f"rc={rc}"
    return True, ""


# --------------------------------------------------------------------- 隔离


def quarantine_dir() -> Path:
    return util.home_dir() / "quarantine"


def quarantine_file(
    path: Path,
    expected: Optional[str],
    actual: Optional[str],
    *,
    reason: str = "sha256-mismatch",
) -> Path:
    """把校验失败的文件移入隔离目录，写旁注；返回隔离后路径。

    只移动、不删除：文件本身是我们的下载副本，移动失败也不影响用户数据。
    """
    target_dir = util.ensure_private_dir(quarantine_dir())
    target = target_dir / f"{path.name}.{reason}"
    if target.exists():
        target = target_dir / f"{path.name}.{reason}.{util.now_stamp()}"
    shutil.move(str(path), str(target))
    sidecar = target.parent / (target.name + ".json")
    note = {
        "schema": "ark-quarantine.v1",
        "reason": reason,
        "quarantined_file": str(target),
        "original_file": str(path),
        "expected_sha256": expected,
        "actual_sha256": actual,
        "detected_at": util.now_display(),
    }
    try:
        util.atomic_write_json(sidecar, util.redact_obj(note))
    except OSError:
        pass
    return target


def verify_staged(staged: Path, expected: Optional[str], *, name: Optional[str] = None) -> tuple:
    """下载落盘后的最终校验；不符即隔离并返回失败。"""
    if not expected:
        return True, ""
    actual = util.sha256_file(staged)
    if actual.lower() == str(expected).lower():
        return True, ""
    where = quarantine_file(staged, str(expected).lower(), actual.lower())
    label = name or staged.name
    return (
        False,
        f"{label} sha256 不符：期望 {str(expected)[:16]}… 实际 {actual[:16]}…，已隔离到 {where}",
    )


# --------------------------------------------------------------------- 台账


def _has_traversal(value: str) -> bool:
    return any(part == ".." for part in value.replace("\\", "/").split("/"))


def _is_forbidden_name(value: str) -> bool:
    if not value:
        return False
    if _has_traversal(value):
        return True
    base = Path(value).name
    if FORBIDDEN.search(base) or FORBIDDEN.search(value):
        return True
    # *.bak 形式的备份同样可能藏着凭据
    if base.lower().endswith(".bak") and FORBIDDEN.search(base[:-4]):
        return True
    return False


def guard(job: dict) -> None:
    """在网络/ssh 之前拦截凭据类路径与 ``..`` 穿越。"""
    for field in ("name", "path"):
        value = str(job.get(field, "") or "")
        if _is_forbidden_name(value):
            util.die(
                f"拒绝搬运凭据类文件或越界路径（命中 {field}={value}）。"
                "凭据不允许经 ark-mesh 自动复制，请在目标节点用官方方式单独配置。",
                util.EXIT_FORBIDDEN,
            )


def candidates(cfg: dict, artifact: dict, skip_nodes: set) -> list:
    """按 priority 排序的来源清单；0 优先级表示本地缓存，由 fetch 单独处理。"""
    locs = [dict(loc, _index=index) for index, loc in enumerate(artifact.get("locations", []))]
    locs = [loc for loc in locs if loc.get("node") not in skip_nodes]
    return sorted(locs, key=lambda loc: (loc.get("priority", 100), loc["_index"]))


def cache_path(cfg: dict, artifact: dict) -> Path:
    base = Path(
        os.environ.get(
            "ARK_MESH_CACHE",
            cfg.get("cache", {}).get("dir", "~/.cache/ark-mesh"),
        )
    ).expanduser()
    digest = artifact.get("sha256") or "nohash"
    return base / digest[:16] / (artifact.get("filename") or artifact["name"].split("/")[-1])


def write_manifest(dest_dir: Path, record: dict, local: bool) -> Path:
    """在目的目录追加一条制品记录（保留历史，按 sha256+destination 幂等去重）。"""
    if not local:
        return dest_dir / MANIFEST_NAME  # 远端清单由调用方单独写
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / MANIFEST_NAME
    data: dict = {"schema": MANIFEST_SCHEMA, "artifacts": []}
    if path.exists():
        try:
            loaded = util.read_json(path)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {"schema": MANIFEST_SCHEMA, "artifacts": []}
    arts = [
        art
        for art in data.get("artifacts", [])
        if not (
            art.get("sha256") == record.get("sha256")
            and art.get("destination") == record.get("destination")
        )
    ]
    arts.append(util.redact_obj(record))
    data["artifacts"] = arts
    data["schema"] = data.get("schema") or MANIFEST_SCHEMA
    data["updated_at"] = util.now_display()
    util.atomic_write_json(path, util.redact_obj(data), mode=0o644)
    return path


def manifest_path_on(cfg: dict, dest_node: Optional[str], dest_dir: str) -> str:
    if not dest_node:
        return str(Path(dest_dir) / MANIFEST_NAME)
    node = node_spec(cfg, dest_node)
    sep = "\\" if is_windows(node) else "/"
    return dest_dir.rstrip("\\/") + sep + MANIFEST_NAME


def write_remote_manifest(
    cfg: dict, dest_node: str, dest_dir: str, record: dict, timeout: float
) -> tuple:
    """把一条记录追加进目的节点上的 manifest（用目标节点自己的解释器）。"""
    node = node_spec(cfg, dest_node)
    payload = base64.b64encode(
        json.dumps(util.redact_obj(record), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    stamp = util.now_display()
    if is_windows(node):
        safe_dir = dest_dir.replace("'", "''")
        script = (
            "$ErrorActionPreference='Stop';"
            f"$dir='{safe_dir}';"
            f"$raw=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'));"
            "$rec=$raw | ConvertFrom-Json;"
            "$p=Join-Path $dir 'artifact-manifest.json';"
            "if (Test-Path -LiteralPath $p) { $d=Get-Content -LiteralPath $p -Raw | ConvertFrom-Json } "
            "else { $d=[pscustomobject]@{schema='ark-artifact-manifest.v1';artifacts=@()} };"
            "$list=@($d.artifacts);"
            "$list=@($list | Where-Object { -not ($_.sha256 -eq $rec.sha256 -and $_.destination -eq $rec.destination) });"
            "$list+=$rec;"
            f"$out=[pscustomobject]@{{schema='ark-artifact-manifest.v1';artifacts=$list;updated_at='{stamp}'}};"
            "$out | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $p -Encoding UTF8;"
            "Write-Output 'ARKMESH_OK'"
        )
        cmd = remote_shell(cfg, dest_node) + [ps_encoded(script)]
    else:
        script = (
            "import base64,json,pathlib;"
            f"d=pathlib.Path({dest_dir!r});d.mkdir(parents=True,exist_ok=True);"
            "p=d/'artifact-manifest.json';"
            f"rec=json.loads(base64.b64decode({payload!r}));"
            "data=json.loads(p.read_text()) if p.exists() and p.stat().st_size "
            "else {'schema':'ark-artifact-manifest.v1','artifacts':[]};"
            "arts=[a for a in data.get('artifacts',[]) "
            "if not (a.get('sha256')==rec['sha256'] and a.get('destination')==rec['destination'])];"
            "arts.append(rec);data['artifacts']=arts;"
            f"data['updated_at']={stamp!r};"
            "p.write_text(json.dumps(data,ensure_ascii=False,indent=2)+chr(10));"
            "print('ARKMESH_OK')"
        )
        cmd = remote_shell(cfg, dest_node) + [f"python3 -c {shq(script)}"]
    rc, out, err = util.run_cmd(cmd, timeout)
    if rc != 0 or "ARKMESH_OK" not in out:
        clean = _clean_remote_err(err)
        return False, (clean or out)[:200]
    return True, ""


# --------------------------------------------------------------------- 命令


def _print_json(obj: Any) -> None:
    print(json.dumps(util.redact_obj(obj), ensure_ascii=False, indent=2))


def _write_failure(report: dict, dest_dir: str) -> None:
    try:
        out = Path(dest_dir)
        out.mkdir(parents=True, exist_ok=True)
        util.atomic_write_json(out / FAILURE_NAME, util.redact_obj(report), mode=0o644)
    except OSError as exc:
        util.info(f"ark-node: 警告：失败报告未能落盘: {exc}")


def build_failure_report(
    art: Optional[dict],
    name: str,
    attempts: list,
    reason: str,
    *,
    cfg: dict,
    dest_node: Optional[str],
    dest_dir: str,
) -> dict:
    art = art or {}
    next_steps = []
    if art.get("locations"):
        known = ", ".join(f"{loc['node']}:{loc['path']}" for loc in art["locations"])
        next_steps.append(f"确认这些路径上的文件是否被移动/删除: {known}")
    next_steps.append("在任一同网节点用 `ark-node fetch index --name ... --node ... --path ...` 登记新发现的位置")
    next_steps.append("若是新版本固件/工具，先按邮件或 NAS 取件流程落到任一节点，再登记台账")
    next_steps.append(f"全部来源失败后向派发方报 BLOCKED，并附本报告（{FAILURE_NAME}）")
    return {
        "schema": FAILURE_SCHEMA,
        "name": name,
        "project": art.get("project"),
        "version": art.get("version"),
        "reason": reason,
        "dest_node": dest_node,
        "dest_dir": dest_dir,
        "attempts": attempts,
        "next_steps": next_steps,
        "generated_at": util.now_display(),
        "config": cfg.get("_path"),
        "verdict": "BLOCKED",
    }


def _exit_for_attempts(attempts: list) -> int:
    """全部来源失败后的退出码：全连通性问题 → NETWORK，否则 NO_SOURCE。"""
    if not attempts:
        return util.EXIT_NO_SOURCE
    if all(attempt.get("result") in _NETWORK_RESULTS for attempt in attempts):
        return util.EXIT_NETWORK
    return util.EXIT_NO_SOURCE


def cmd_status(cfg: dict, args) -> int:
    timeout = args.timeout
    rows = []
    for name in sorted(cfg["nodes"]):
        ok, detail, elapsed = probe_node(cfg, name, timeout)
        rows.append(
            {
                "node": name,
                "reachable": ok,
                "detail": detail,
                "elapsed_s": round(elapsed, 2),
            }
        )

    arts = []
    for name, art in sorted(cfg["artifacts"].items()):
        cached_path = cache_path(cfg, dict(art, name=name))
        cached = cached_path.exists() and (
            not art.get("sha256") or util.sha256_file(cached_path) == art["sha256"]
        )
        locs = candidates(cfg, art, set())
        best = locs[:1]
        arts.append(
            {
                "name": name,
                "project": art.get("project"),
                "version": art.get("version"),
                "bytes": art.get("bytes"),
                "sha256": art.get("sha256"),
                "cached": cached,
                "sources": [f"{loc['node']}:{loc['path']}" for loc in locs],
                "best_source": f"{best[0]['node']}:{best[0]['path']}" if best else None,
            }
        )

    if args.json:
        _print_json(
            {
                "config": cfg["_path"],
                "nodes": rows,
                "artifacts": arts,
                "checked_at": util.now_display(),
            }
        )
        return util.EXIT_OK if all(row["reachable"] for row in rows) else util.EXIT_NETWORK

    print(f"来源索引: {cfg['_path']}")
    print(f"{'NODE':<12}{'STATUS':<10}{'SEC':>6}  DETAIL")
    for row in rows:
        status = "PASS" if row["reachable"] else "FAIL"
        print(f"{row['node']:<12}{status:<10}{row['elapsed_s']:>6}  {row['detail'][:70]}")
    print()
    print(f"{'ARTIFACT':<28}{'PROJECT':<12}{'VERSION':<12}{'SIZE':>10}  CACHE  BEST SOURCE")
    for art in arts:
        print(
            f"{art['name']:<28}{str(art['project'] or '-'):<12}{str(art['version'] or '-'):<12}"
            f"{util.human_bytes(art['bytes'] or 0):>10}  "
            f"{'yes' if art['cached'] else 'no':<6} {art['best_source'] or '-'}"
        )
    return util.EXIT_OK if all(row["reachable"] for row in rows) else util.EXIT_NETWORK


def cmd_index(cfg: dict, args) -> int:
    art = cfg["artifacts"].setdefault(args.name, {"name": args.name, "locations": []})
    for key in ("project", "version", "sha256", "bytes", "filename", "note"):
        value = getattr(args, key, None)
        if value is not None:
            art[key] = value
    if args.node and args.path:
        guard({"path": args.path})
        node = node_spec(cfg, args.node)
        node_dir = node.get("base_dir", "")
        path = args.path
        if node_dir and not path.startswith(("/", "D:", "C:", "E:")):
            sep = "\\" if is_windows(node) else "/"
            path = node_dir.rstrip("\\/") + sep + path.lstrip("\\/")
        art["locations"] = [
            loc
            for loc in art["locations"]
            if not (loc["node"] == args.node and loc["path"] == path)
        ]
        art["locations"].append(
            {
                "node": args.node,
                "path": path,
                "priority": args.priority
                if args.priority is not None
                else len(art["locations"]) + 1,
            }
        )
    if args.remove_node:
        art["locations"] = [
            loc for loc in art["locations"] if loc["node"] != args.remove_node
        ]
    if not art.get("locations") and not args.keep_empty:
        cfg["artifacts"].pop(args.name, None)
        save_config(cfg)
        print(f"已移除制品 {args.name}（无剩余来源）")
        return util.EXIT_OK
    save_config(cfg)
    _print_json(cfg["artifacts"][args.name])
    return util.EXIT_OK


def _single_attempt(cfg: dict, art: dict, loc: dict, timeout: float, staging: Path) -> tuple:
    node, path = loc["node"], loc["path"]
    started = time.time()
    attempt = {
        "source_node": node,
        "source_path": path,
        "priority": loc.get("priority"),
    }

    def _fail(result: str, error: str) -> tuple:
        attempt.update(
            result=result, error=error, elapsed_s=round(time.time() - started, 2)
        )
        return False, error, attempt

    ok, info, _probe_elapsed = probe_node(cfg, node, min(timeout, 10))
    if not ok:
        reason = "无口令环境变量" if info.startswith("SKIP:") else "不可达"
        return _fail("unreachable", f"节点{reason}: {info}")
    try:
        digest, err = remote_hash(cfg, node, path, timeout)
    except MissingPass as exc:
        return _fail("unreachable", str(exc))
    if digest is None:
        return _fail("source_unusable", err)
    expected = art.get("sha256")
    if expected and digest.lower() != str(expected).lower():
        return _fail(
            "hash_mismatch",
            f"源端 sha256={digest} 与台账 {expected} 不符",
        )

    stage = staging / (art.get("filename") or Path(path).name)
    try:
        ok, err = scp_fetch(cfg, node, path, stage, timeout)
    except MissingPass as exc:
        return _fail("unreachable", str(exc))
    if not ok:
        return _fail("copy_failed", err)

    local_digest = util.sha256_file(stage)
    if local_digest != digest:
        where = quarantine_file(
            stage,
            (str(expected).lower() if expected else digest),
            local_digest,
        )
        return _fail(
            "quarantined",
            f"回传后 sha256={local_digest} != 源端 {digest}，已隔离到 {where}",
        )
    attempt.update(
        result="ok",
        sha256=local_digest,
        bytes=stage.stat().st_size,
        elapsed_s=round(time.time() - started, 2),
    )
    return True, str(stage), attempt


def cmd_fetch(cfg: dict, args) -> int:
    dest_node = args.dest_node
    dest_dir = args.dest
    timeout = args.timeout
    staging = Path(tempfile.mkdtemp(prefix="ark-mesh-"))
    attempts: list = []
    try:
        if args.source and args.path:
            guard({"name": args.name or Path(args.path).name, "path": args.path})
            art = {
                "name": args.name or Path(args.path).name,
                "sha256": args.sha256,
                "filename": args.filename or Path(args.path).name,
            }
            locs = [{"node": args.source, "path": args.path, "priority": 1, "_index": 0}]
        else:
            if not args.name:
                util.die("fetch 需要 --name（台账）或 --source/--path（直连）", util.EXIT_USAGE)
            art = cfg["artifacts"].get(args.name)
            if not art:
                report = build_failure_report(
                    None,
                    args.name,
                    [],
                    "制品未登记在台账里",
                    cfg=cfg,
                    dest_node=dest_node,
                    dest_dir=dest_dir,
                )
                _print_json(report)
                _write_failure(report, dest_dir)
                return util.EXIT_NO_SOURCE
            art = dict(art, name=args.name)
            guard({"name": art["name"], "path": ""})
            for loc in art.get("locations") or []:
                guard({"name": art["name"], "path": loc.get("path", "")})
            locs = candidates(cfg, art, set(args.skip_node or []))
            cached = cache_path(cfg, art)
            cache_ok = cached.exists() and (
                not art.get("sha256") or util.sha256_file(cached) == art["sha256"]
            )
            if cache_ok and not args.force:
                locs = [
                    {"node": "__cache__", "path": str(cached), "priority": 0, "_index": -1}
                ] + locs
            if not locs:
                report = build_failure_report(
                    art,
                    args.name,
                    [],
                    "台账里没有任何可用来源",
                    cfg=cfg,
                    dest_node=dest_node,
                    dest_dir=dest_dir,
                )
                _print_json(report)
                _write_failure(report, dest_dir)
                return util.EXIT_NO_SOURCE

        staged: Optional[Path] = None
        used: Optional[dict] = None
        for loc in locs:
            if loc["node"] == "__cache__":
                staged = Path(loc["path"])
                attempts.append(
                    {
                        "source_node": "local-cache",
                        "source_path": str(staged),
                        "result": "ok",
                        "elapsed_s": 0.0,
                        "sha256": art.get("sha256"),
                    }
                )
                used = {"node": "local-cache", "path": str(staged)}
                break
            ok, info, attempt = _single_attempt(cfg, art, loc, timeout, staging)
            attempts.append(attempt)
            util.info(
                f"[{attempt['result']}] {loc['node']}:{loc['path']} "
                f"({attempt.get('elapsed_s')}s)"
                + ("" if ok else f" -> {attempt.get('error')}")
            )
            if ok:
                staged = Path(info)
                used = {"node": loc["node"], "path": loc["path"]}
                break

        if staged is None or used is None:
            report = build_failure_report(
                art,
                args.name or art.get("name"),
                attempts,
                "所有来源均失败",
                cfg=cfg,
                dest_node=dest_node,
                dest_dir=dest_dir,
            )
            _print_json(report)
            _write_failure(report, dest_dir)
            return _exit_for_attempts(attempts)

        # 下载后的最终校验：与台账/显式 --sha256 比对，不符即隔离
        if used["node"] != "local-cache":
            ok, err = verify_staged(staged, art.get("sha256"), name=art.get("name"))
            if not ok:
                report = build_failure_report(
                    art,
                    args.name or art.get("name"),
                    attempts,
                    err,
                    cfg=cfg,
                    dest_node=dest_node,
                    dest_dir=dest_dir,
                )
                _print_json(report)
                _write_failure(report, dest_dir)
                return util.EXIT_NO_SOURCE

        digest = util.sha256_file(staged)
        size = staged.stat().st_size
        art_sha = digest

        # 落到本地缓存（成功即复用）
        if used["node"] != "local-cache":
            cached = cache_path(cfg, dict(art, sha256=art_sha))
            cached.parent.mkdir(parents=True, exist_ok=True)
            if not cached.exists() or util.sha256_file(cached) != art_sha:
                shutil.copy2(staged, cached)

        filename = art.get("filename") or Path(used["path"]).name
        record = {
            "name": art.get("name"),
            "project": art.get("project"),
            "version": art.get("version"),
            "source_node": used["node"],
            "source_path": used["path"],
            "destination": (
                f"{dest_node}:{dest_dir}" if dest_node else str(Path(dest_dir) / filename)
            ),
            "bytes": size,
            "sha256": art_sha,
            "fetched_at": util.now_display(),
        }

        if dest_node:
            node = node_spec(cfg, dest_node)
            sep = "\\" if is_windows(node) else "/"
            remote_target = dest_dir.rstrip("\\/") + sep + filename
            ok, err = ensure_dir(cfg, dest_node, dest_dir, timeout)
            if not ok:
                util.die(f"目的节点目录创建失败: {err}", util.EXIT_NO_SOURCE)
            ok, err = scp_push(cfg, dest_node, staged, remote_target, timeout)
            if not ok:
                util.die(f"推送到目的节点失败: {err}", util.EXIT_NO_SOURCE)
            remote_digest, herr = remote_hash(cfg, dest_node, remote_target, timeout)
            if remote_digest != art_sha:
                util.die(
                    f"目的端校验失败: {herr or remote_digest} != {art_sha}",
                    util.EXIT_NO_SOURCE,
                )
            record["destination"] = f"{dest_node}:{remote_target}"
            record["destination_sha256_verified"] = True
            mok, merr = write_remote_manifest(cfg, dest_node, dest_dir, record, timeout)
            if not mok:
                util.info(f"ark-node: 警告：目的端 manifest 写入失败: {merr}")
            # 同时在本机留一份方便审计
            write_manifest(Path(tempfile.mkdtemp(prefix="ark-mesh-audit-")), record, local=True)
        else:
            target = Path(dest_dir)
            target.mkdir(parents=True, exist_ok=True)
            dest_file = target / filename
            if not dest_file.exists() or util.sha256_file(dest_file) != art_sha:
                shutil.copy2(staged, dest_file)
            if util.sha256_file(dest_file) != art_sha:
                util.die("本地目的文件校验失败", util.EXIT_NO_SOURCE)
            record["destination_sha256_verified"] = True
            mpath = write_manifest(target, record, local=True)
            util.info(f"manifest: {mpath}")

        if args.json:
            _print_json({"ok": True, "manifest_entry": record, "attempts": attempts})
        else:
            print(
                f"PASS {record['name']} {util.human_bytes(size)} "
                f"sha256={art_sha[:16]}… from {record['source_node']} -> {record['destination']}"
            )
        return util.EXIT_OK
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def cmd_archive(cfg: dict, args) -> int:
    art = cfg["artifacts"].get(args.name)
    if not art:
        util.die(f"制品未登记: {args.name}", util.EXIT_USAGE)
    target_node = args.to_node
    node = node_spec(cfg, target_node)
    version = args.version or art.get("version") or "unversioned"
    project = args.project or art.get("project") or "unassigned"
    locations = art.get("locations") or [{"path": art["name"]}]
    filename = art.get("filename") or Path(locations[0]["path"]).name
    base = str(node.get("base_dir", "/srv/ark/artifacts")).rstrip("\\/")
    sep = "\\" if is_windows(node) else "/"
    target_dir = sep.join([base, project, version])

    staging = Path(tempfile.mkdtemp(prefix="ark-mesh-archive-"))
    try:
        src = None
        attempts: list = []
        for loc in candidates(cfg, art, set(args.skip_node or [])):
            ok, info, attempt = _single_attempt(cfg, art, loc, args.timeout, staging)
            attempts.append(attempt)
            util.info(f"[{attempt['result']}] {loc['node']}:{loc['path']}")
            if ok:
                src = (Path(info), loc)
                break
        if src is None:
            util.die("归档失败：没有任何可用来源", _exit_for_attempts(attempts))
        staged, loc = src
        ok, err = ensure_dir(cfg, target_node, target_dir, args.timeout)
        if not ok:
            util.die(f"归档目录创建失败: {err}", util.EXIT_NO_SOURCE)
        remote_target = target_dir + sep + filename
        ok, err = scp_push(cfg, target_node, staged, remote_target, args.timeout)
        if not ok:
            util.die(f"归档上传失败: {err}", util.EXIT_NO_SOURCE)
        digest, herr = remote_hash(cfg, target_node, remote_target, args.timeout)
        if digest != util.sha256_file(staged):
            util.die(f"归档校验失败: {herr or digest}", util.EXIT_NO_SOURCE)
        # 登记归档节点为来源（优先级最低）
        priorities = [loc_item.get("priority", 0) for loc_item in art["locations"]] + [0]
        prio = max(priorities) + 1
        art["locations"] = [
            loc_item
            for loc_item in art["locations"]
            if not (loc_item["node"] == target_node and loc_item["path"] == remote_target)
        ]
        art["locations"].append(
            {"node": target_node, "path": remote_target, "priority": prio}
        )
        save_config(cfg)
        print(
            f"PASS archive {args.name} -> {target_node}:{remote_target} "
            f"sha256={digest[:16]}… (from {loc['node']})"
        )
        return util.EXIT_OK
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _dispatch(cfg: dict, args) -> int:
    if args.cmd == "status":
        return cmd_status(cfg, args)
    if args.cmd == "index":
        return cmd_index(cfg, args)
    if args.cmd == "fetch":
        return cmd_fetch(cfg, args)
    if args.cmd == "archive":
        return cmd_archive(cfg, args)
    util.die(f"未知子命令 {args.cmd}", util.EXIT_USAGE)
    return util.EXIT_USAGE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ark-node fetch",
        description="节点间制品取件/归档（只搬运与校验，不承载业务逻辑）",
    )
    parser.add_argument(
        "--config",
        help="来源索引路径（默认 $ARK_MESH_CONFIG 或 ~/.config/ark/mesh-sources.json）",
    )
    subs = parser.add_subparsers(dest="cmd", required=True)

    p_status = subs.add_parser("status", help="节点可达性 + 制品台账 + 缓存")
    p_status.add_argument("--json", action="store_true")
    p_status.add_argument("--timeout", type=float, default=8.0)

    p_index = subs.add_parser("index", help="登记/更新制品来源")
    p_index.add_argument("--name", required=True)
    p_index.add_argument("--project")
    p_index.add_argument("--version")
    p_index.add_argument("--sha256")
    p_index.add_argument("--bytes", type=int)
    p_index.add_argument("--filename")
    p_index.add_argument("--note")
    p_index.add_argument("--node")
    p_index.add_argument("--path")
    p_index.add_argument("--priority", type=int)
    p_index.add_argument("--remove-node")
    p_index.add_argument("--keep-empty", action="store_true")

    p_fetch = subs.add_parser("fetch", help="取件到目的目录/节点")
    p_fetch.add_argument("--name")
    p_fetch.add_argument("--source")
    p_fetch.add_argument("--path")
    p_fetch.add_argument("--sha256")
    p_fetch.add_argument("--filename")
    p_fetch.add_argument(
        "--dest", required=True, help="目的目录（配合 --dest-node 时是目标机路径）"
    )
    p_fetch.add_argument("--dest-node")
    p_fetch.add_argument("--timeout", type=float, default=30.0, help="单来源超时秒数，默认 30")
    p_fetch.add_argument(
        "--skip-node", action="append", default=[], help="临时跳过某来源（故障演练用）"
    )
    p_fetch.add_argument("--force", action="store_true", help="忽略缓存，强制回源")
    p_fetch.add_argument("--json", action="store_true")

    p_arc = subs.add_parser("archive", help="把制品归档到存储节点")
    p_arc.add_argument("--name", required=True)
    p_arc.add_argument("--version")
    p_arc.add_argument("--project")
    p_arc.add_argument("--to-node", required=True, help="归档目的节点名（配置里的键）")
    p_arc.add_argument("--timeout", type=float, default=120.0)
    p_arc.add_argument("--skip-node", action="append", default=[])

    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(list(argv))
    cfg = load_config(args.config)
    try:
        return _dispatch(cfg, args)
    except MissingPass as exc:
        util.die(str(exc), util.EXIT_USAGE)
        return util.EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(__import__("sys").argv[1:]))
