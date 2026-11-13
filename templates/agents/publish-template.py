#!/usr/bin/env python3
"""NoahsArk 智能体模板发布器：从 Git 内容生成发布包、扫描、算摘要、上传对象存储。

用法::

    python3 templates/agents/publish-template.py                # 默认 dry-run
    python3 templates/agents/publish-template.py --json         # 机器可读计划
    python3 templates/agents/publish-template.py --apply --yes  # 真正上传

行为
----
1. 读 Git 工作区里的模板三件套（模板正文、profile、校验器）；
2. 用仓库自带 ``tools/secret_scan.py`` 扫凭据、``tools/time_lint.py`` 卡时间口径，
   任一不干净直接拒绝发布（退出码 4）；
3. 逐文件算 sha256 与字节数，生成 ``manifest.json``；
4. **默认 dry-run**：只打印将要上传的对象路径、大小、摘要、可见性，不联网；
5. ``--apply`` 才上传，且必须先有 ``--yes`` 与对象存储凭据环境变量。

退出码沿用全平台跨契约口径::

    0 成功 / dry-run 完成
    1 失败（读写错误、摘要不一致）
    2 用法错误（参数非法、缺少发布所需的配置）
    3 NO_SOURCE（内容缺失、Git 状态不可用、工作区有未提交内容）
    4 FORBIDDEN（扫描命中、凭据缺失/被拒）
    5 NETWORK（上传网络错误）

凭据只从环境变量读取，永不写进产出的 manifest、日志或对象内容。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "1.7.0"
MANIFEST_SCHEMA = "ark.agent-template.manifest.v1"
TEMPLATE_SCHEMA = "ark.agent-template.v1"

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
DEFAULT_VERSION = "1.7.0"
DEFAULT_PREFIX = "noahsark/agent-template"
DEFAULT_BUCKET = "listenai-test-internal"
DEFAULT_VISIBILITY = "public-read"

# (文件名, 角色)：发布包只含这四件，多一个都不发。
ARTIFACTS: List[Tuple[str, str]] = [
    ("noahsark-agent-template.md", "template"),
    ("agent-profile.v1.json", "profile"),
    ("verify-template.py", "verifier"),
    ("run-state.v1.json", "schema"),
]

# 对象存储凭据的变量名（只有名字，值永远不落盘）。
OSS_ENV_KEYS = ("ACCESS_KEY_ID", "ACCESS_KEY_SECRET")
OBJECT_ACL_HEADER = "x-oss-object-acl"

BEIJING = timezone(timedelta(hours=8))
FMT_DISPLAY = "%Y-%m-%d %H:%M:%S"

DIGEST_FIELD = re.compile(  # noqa: secret-scan
    r'"(?:content_|evidence_)?sha256"\s*:\s*"[0-9a-f]{64}"'
    r'|"commit"\s*:\s*"[0-9a-f]{40}"'
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NO_SOURCE = 3
EXIT_FORBIDDEN = 4
EXIT_NETWORK = 5


class PublishError(Exception):
    """带退出码的失败。"""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------- 基础


def now_display() -> str:
    """北京时间、无时区标记。"""
    return datetime.now(BEIJING).strftime(FMT_DISPLAY)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PublishError(f"无法读取 {path}: {exc}", EXIT_NO_SOURCE) from exc


def load_json(path: Path) -> Any:
    raw = read_bytes(path)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublishError(f"{path} 不是合法 JSON: {exc}", EXIT_FAILED) from exc


def load_module(name: str, path: Path) -> Any:
    """按文件路径加载仓库内置工具模块（不依赖安装）。"""
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise PublishError(f"无法加载模块 {path}", EXIT_FAILED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git_ok(args: List[str]) -> bool:
    """只看 git 命令是否成功（不取输出）。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT)] + args, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def git(args: List[str]) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT)] + args,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def git_dirty() -> bool:
    """工作区是否改过**已跟踪**文件。

    生成器自己的产物（``templates/agents/manifest.json``）不计入：它是输出，不是源。
    未跟踪文件也不计入（首次发布前内容本来就是新文件），改由 ``tracked`` 把关。
    """
    text = git(["status", "--porcelain", "--untracked-files=no"]) or ""
    for line in text.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) < 2:
            continue
        if parts[1].strip().endswith("templates/agents/manifest.json"):
            continue
        return True
    return False


def git_tracked() -> bool:
    """发布包三个文件是否都已被 git 跟踪。"""
    return all(git_ok(["ls-files", "--error-unmatch", str(HERE / name)]) for name, _ in ARTIFACTS)


# --------------------------------------------------------------------- 扫描


def scan_secrets(paths: List[Path]) -> List[str]:
    """凭据扫描：复用仓库工具的规则，但对内容摘要字段放行。

    manifest 里的 ``sha256`` 是内容摘要，不是凭据；扫描前把这类字段掩掉，
    其余内容按原规则逐行检查。
    """
    scanner_path = REPO_ROOT / "tools" / "secret_scan.py"
    if not scanner_path.exists():
        raise PublishError("缺少 tools/secret_scan.py，无法做凭据扫描", EXIT_NO_SOURCE)
    scanner = load_module("ark_secret_scan", scanner_path)
    findings: List[str] = []
    for path in paths:
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            raise PublishError(f"无法读取 {path}: {exc}", EXIT_NO_SOURCE) from exc
        masked = DIGEST_FIELD.sub('"sha256": "<digest>"', text)
        findings.extend(scanner.scan_text(str(path), masked))
    return findings


def run_time_lint(paths: List[Path]) -> List[str]:
    """时间口径检查：复用仓库工具的规则，返回违规行。"""
    linter_path = REPO_ROOT / "tools" / "time_lint.py"
    if not linter_path.exists():
        raise PublishError("缺少 tools/time_lint.py，无法做时间口径检查", EXIT_NO_SOURCE)
    proc = subprocess.run(
        [sys.executable, str(linter_path), *[str(p) for p in paths]],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode == 0:
        return []
    if proc.returncode == 2:
        raise PublishError("time_lint 用法错误", EXIT_USAGE)
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    return lines or ["time_lint 报告违规但未给出明细"]


# --------------------------------------------------------------------- 组装


def build_release(
    version: str,
    prefix: str,
    bucket: str,
    visibility: str,
    commit: Optional[str],
) -> Dict[str, Any]:
    files: List[Dict[str, Any]] = []
    blobs: Dict[str, bytes] = {}

    for name, role in ARTIFACTS:
        path = HERE / name
        if not path.exists():
            raise PublishError(f"发布包缺少文件: {path}", EXIT_NO_SOURCE)
        data = read_bytes(path)
        blobs[name] = data
        files.append(
            {
                "path": name,
                "object": f"{prefix}/v{version}/{name}",
                "role": role,
                "sha256": sha256_bytes(data),
                "bytes": len(data),
            }
        )

    profile_path = HERE / "agent-profile.v1.json"
    profile = load_json(profile_path)
    if not isinstance(profile, dict) or profile.get("schema") != "ark.agent-profile.v1":
        raise PublishError("agent-profile.v1.json 的 schema 不是 ark.agent-profile.v1", EXIT_FAILED)

    dirty = git_dirty()
    tracked = git_tracked()
    resolved = commit or git(["rev-parse", "HEAD"])
    if not resolved:
        raise PublishError("无法解析 Git 提交号（不是 Git 仓库？）", EXIT_NO_SOURCE)

    manifest: Dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "name": "noahsark-agent-template",
        "version": version,
        "channel": "immutable",
        "source_version": version,
        "published_at": now_display(),
        "template_schema": TEMPLATE_SCHEMA,
        "git": {
            "repo": "zjashanda/ark-node",
            "commit": resolved,
            "path": "templates/agents",
            "dirty": dirty,
            "tracked": tracked,
        },
        "oss": {
            "bucket": bucket,
            "prefix": prefix,
            "visibility": visibility,
        },
        "compatible": {
            "profile": "agent-profile.v1",
            "template": TEMPLATE_SCHEMA,
            "ark_node": profile.get("default", {}).get("compatibility", {}).get("ark_node", ">=0.1.0"),
        },
        "files": files,
        "checks": {"secret_scan": "pass", "time_lint": "pass"},
        "notes": (
            "对象路径不可变；latest 是稳定别名，内容与 v{version} 完全一致。"
            "校验下载内容用同目录 verify-template.py。"
        ).format(version=version),
    }
    return {"manifest": manifest, "blobs": blobs}


def latest_manifest(manifest: Dict[str, Any], prefix: str) -> Dict[str, Any]:
    """latest 别名清单：内容一致，路径换成 latest，并指回不可变版本。"""
    alias = json.loads(json.dumps(manifest))
    alias["channel"] = "latest"
    alias["source_version"] = manifest["version"]
    alias["immutable_manifest"] = f"{prefix}/v{manifest['version']}/manifest.json"
    for entry in alias["files"]:
        entry["object"] = f"{prefix}/latest/{entry['path']}"
    return alias


def upload_plan(manifest: Dict[str, Any], blobs: Dict[str, bytes], bucket: str) -> List[Dict[str, Any]]:
    """上传顺序：先不可变内容，再不可变清单，最后 latest 别名。"""
    prefix = manifest["oss"]["prefix"]
    version = manifest["version"]
    alias = latest_manifest(manifest, prefix)
    steps: List[Dict[str, Any]] = []
    for entry in manifest["files"]:
        steps.append(
            {
                "object": entry["object"],
                "bytes": entry["bytes"],
                "sha256": entry["sha256"],
                "source": entry["path"],
                "data": blobs[entry["path"]],
            }
        )
    immutable_manifest = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    steps.append(
        {
            "object": f"{prefix}/v{version}/manifest.json",
            "bytes": len(immutable_manifest.encode("utf-8")),
            "sha256": sha256_bytes(immutable_manifest.encode("utf-8")),
            "source": "manifest.json",
            "data": immutable_manifest.encode("utf-8"),
        }
    )
    for entry in alias["files"]:
        steps.append(
            {
                "object": entry["object"],
                "bytes": entry["bytes"],
                "sha256": entry["sha256"],
                "source": entry["path"],
                "data": blobs[entry["path"]],
            }
        )
    latest_text = json.dumps(alias, ensure_ascii=False, indent=2) + "\n"
    steps.append(
        {
            "object": f"{prefix}/latest/manifest.json",
            "bytes": len(latest_text.encode("utf-8")),
            "sha256": sha256_bytes(latest_text.encode("utf-8")),
            "source": "manifest.json",
            "data": latest_text.encode("utf-8"),
        }
    )
    plan: List[Dict[str, Any]] = []
    for step in steps:
        item = {key: value for key, value in step.items() if key != "data"}
        item["_data"] = step["data"]
        plan.append(item)
    return plan


# --------------------------------------------------------------------- 上传


def normalize_endpoint(raw: str) -> str:
    """把 endpoint 归一成纯主机名：去掉协议头与尾部斜杠。"""
    host = raw.strip()
    for scheme in ("https://", "http://"):
        if host.startswith(scheme):
            host = host[len(scheme) :]
    return host.rstrip("/")


def oss_put(
    endpoint_host: str,
    bucket: str,
    key: str,
    data: bytes,
    ak: str,
    sk: str,
    visibility: str,
) -> None:
    """对象存储 PUT（签名 V1，零第三方依赖，显式带 x-oss-object-acl）。"""
    endpoint_host = normalize_endpoint(endpoint_host)
    content_type = "application/octet-stream"
    date_header = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    content_md5 = base64.b64encode(hashlib.md5(data).digest()).decode("ascii")
    resource = f"/{bucket}/{key}"
    # 显式对象 ACL：不依赖桶默认权限，匿名可读才算发出去。
    canonical_headers = f"{OBJECT_ACL_HEADER}:{visibility}"
    to_sign = "\n".join(
        ["PUT", content_md5, content_type, date_header, canonical_headers, resource]
    )
    signature = base64.b64encode(
        hmac.new(sk.encode("utf-8"), to_sign.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")

    url = f"https://{bucket}.{endpoint_host}/{key}"
    request = urllib.request.Request(url, data=data, method="PUT")
    request.add_header("Date", date_header)
    request.add_header("Content-Type", content_type)
    request.add_header("Content-MD5", content_md5)
    request.add_header(OBJECT_ACL_HEADER, visibility)
    request.add_header("Authorization", f"OSS {ak}:{signature}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status not in (200, 201):
                raise PublishError(f"上传 {key} 返回 {response.status}", EXIT_FAILED)
    except urllib.error.HTTPError as exc:
        code = EXIT_FORBIDDEN if exc.code in (401, 403) else EXIT_FAILED
        raise PublishError(f"上传 {key} 失败: HTTP {exc.code}", code) from exc
    except urllib.error.URLError as exc:
        raise PublishError(f"上传 {key} 网络错误: {exc.reason}", EXIT_NETWORK) from exc


def anon_readback(
    steps: List[Dict[str, Any]], endpoint_host: str, bucket: str
) -> int:
    host = normalize_endpoint(endpoint_host)
    for step in steps:
        url = f"https://{bucket}.{host}/{step['object']}"
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, method="GET"), timeout=30
            ) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise PublishError(
                f"匿名回读 {step['object']} 失败: HTTP {exc.code}，对象没真正公开可读",
                EXIT_FORBIDDEN,
            ) from exc
        except urllib.error.URLError as exc:
            raise PublishError(f"匿名回读 {step['object']} 网络错误: {exc.reason}", EXIT_NETWORK) from exc
        actual = hashlib.sha256(body).hexdigest()
        if actual != step["sha256"]:
            raise PublishError(
                f"匿名回读 {step['object']} 内容不符: sha256 {actual} != {step['sha256']}",
                EXIT_FAILED,
            )
    return len(steps)


ALLOWED_VISIBILITY = ("public-read", "private")


def upload_all(
    steps: List[Dict[str, Any]], endpoint_host: str, bucket: str, visibility: str
) -> int:
    ak = os.environ.get("ACCESS_KEY_ID", "")
    sk = os.environ.get("ACCESS_KEY_SECRET", "")
    if not ak or not sk:
        raise PublishError(
            "缺少对象存储凭据：请设置 " + " / ".join(OSS_ENV_KEYS) + "（值不要出现在命令行）",
            EXIT_FORBIDDEN,
        )
    if visibility not in ALLOWED_VISIBILITY:
        raise PublishError(
            f"不支持的可见性: {visibility}（可选 " + " / ".join(ALLOWED_VISIBILITY) + "）",
            EXIT_USAGE,
        )
    for step in steps:
        oss_put(endpoint_host, bucket, step["object"], step["_data"], ak, sk, visibility)
    return anon_readback(steps, endpoint_host, bucket)


# --------------------------------------------------------------------- 输出


def render_table(steps: List[Dict[str, Any]], manifest: Dict[str, Any]) -> str:
    lines = [
        f"模板版本: {manifest['version']}    渠道: immutable + latest    可见性: {manifest['oss']['visibility']}",
        "Git 提交: {}    dirty: {}    tracked: {}".format(
            manifest["git"]["commit"],
            str(manifest["git"]["dirty"]).lower(),
            str(manifest["git"]["tracked"]).lower(),
        ),
        "",
        f"{'对象路径':<66} {'字节':>10}  sha256",
        "-" * 118,
    ]
    for step in steps:
        lines.append(f"{step['object']:<66} {step['bytes']:>10}  {step['sha256'][:16]}...")
    return "\n".join(lines)


def write_manifest(manifest: Dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "manifest.json"
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    target.write_text(text, encoding="utf-8")
    return target


# --------------------------------------------------------------------- 入口


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="publish-template.py",
        description="生成 NoahsArk 智能体模板发布包（默认 dry-run，不联网）。",
    )
    parser.add_argument("--version", default=DEFAULT_VERSION, help=f"发布版本，默认 {DEFAULT_VERSION}")
    parser.add_argument("--prefix", default=os.environ.get("ARK_TEMPLATE_PREFIX", DEFAULT_PREFIX))
    parser.add_argument("--bucket", default=os.environ.get("ARK_OSS_BUCKET", DEFAULT_BUCKET))
    parser.add_argument("--endpoint", default=os.environ.get("ARK_OSS_ENDPOINT", ""))
    parser.add_argument("--visibility", default=DEFAULT_VISIBILITY)
    parser.add_argument("--commit", default=None, help="覆盖 manifest 里的 Git 提交号")
    parser.add_argument("--allow-dirty", action="store_true", help="允许从未提交内容发布（不推荐）")
    parser.add_argument("--out-dir", default=None, help="manifest 落地目录，默认 build/agent-template/<版本>")
    parser.add_argument("--apply", action="store_true", help="真正上传（默认 dry-run）")
    parser.add_argument("--yes", action="store_true", help="--apply 必须同时给出，代表已获批准")
    parser.add_argument("--json", action="store_true", help="输出机器可读计划")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    version = args.version.lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.stderr.write(f"版本号必须是三段数字: {args.version}\n")
        return EXIT_USAGE

    try:
        scan_targets = [HERE / name for name, _ in ARTIFACTS]
        findings = scan_secrets(scan_targets)
        if findings:
            for line in findings:
                sys.stderr.write(f"secret_scan: {line}\n")
            raise PublishError("凭据扫描未通过，拒绝发布", EXIT_FORBIDDEN)
        lint_findings = run_time_lint(scan_targets)
        if lint_findings:
            for line in lint_findings:
                sys.stderr.write(f"time_lint: {line}\n")
            raise PublishError("时间口径检查未通过，拒绝发布", EXIT_FORBIDDEN)

        built = build_release(
            version=version,
            prefix=args.prefix.rstrip("/"),
            bucket=args.bucket,
            visibility=args.visibility,
            commit=args.commit,
        )
        manifest = built["manifest"]
        steps = upload_plan(manifest, built["blobs"], args.bucket)

        out_dir = Path(args.out_dir) if args.out_dir else REPO_ROOT / "build" / "agent-template" / version
        manifest_path = write_manifest(manifest, out_dir)

        plan = {
            "mode": "apply" if args.apply else "dry-run",
            "manifest": str(manifest_path),
            "manifest_sha256": sha256_bytes(manifest_path.read_bytes()),
            "objects": [{key: value for key, value in step.items() if key != "_data"} for step in steps],
        }

        if args.json:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
        else:
            print(render_table(steps, manifest))
            print()
            print(f"manifest: {manifest_path}")
            print(f"manifest sha256: {plan['manifest_sha256']}")

        if not args.apply:
            print()
            print("DRY-RUN：未上传任何对象。加 --apply --yes 才会真正发布（需对象存储凭据）。")
            return EXIT_OK

        if not args.yes:
            raise PublishError("--apply 必须同时给出 --yes（对外发布需明确批准）", EXIT_USAGE)
        if (manifest["git"]["dirty"] or not manifest["git"]["tracked"]) and not args.allow_dirty:
            reason = "工作区有未提交改动" if manifest["git"]["dirty"] else "发布包里有未入库文件"
            raise PublishError(
                f"{reason}，拒绝上传：发布出去的内容将无法用 commit 追溯"
                "（先提交内容再发布；确实要这样发就加 --allow-dirty）",
                EXIT_NO_SOURCE,
            )
        if not args.endpoint:
            raise PublishError("--apply 需要 --endpoint 或 ARK_OSS_ENDPOINT", EXIT_USAGE)

        uploaded = upload_all(steps, args.endpoint, args.bucket, args.visibility)
        print(
            "已上传 {} 个对象到 {}.{}（{}）".format(
                uploaded, args.bucket, normalize_endpoint(args.endpoint), args.visibility
            )
        )
        return EXIT_OK
    except PublishError as exc:
        sys.stderr.write(f"publish-template: {exc}\n")
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
