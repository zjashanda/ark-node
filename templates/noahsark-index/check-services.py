#!/usr/bin/env python3
"""服务契约门禁：离线漂移检查、节点侧在线探测、回执校验。

三个用途：

1. ``--check``（默认）：核对 catalog 服务条目与 ``services/*.json`` 描述是否漂移
   （id / version / endpoint / 端口 / 契约摘要），并校验基线与已有回执；
   可选 ``--kb-repo`` 核对 ark-kb 基线提交。
2. ``--probe``：只在可达节点执行。带 Bearer token 请求健康检查与只读接口，
   把结果写成回执 JSON。token 只从 0600 文件或环境变量读，绝不回显、绝不写进回执。
3. ``--receipt``：把节点侧回执当发布门禁——endpoint、版本、结果必须与描述文件
   一致，否则不许发布。

退出码沿用跨契约约定：
0 OK / 1 FAILED / 2 USAGE / 3 NO_SOURCE / 4 FORBIDDEN / 5 NETWORK。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
CATALOG_PATH = HERE / "catalog.json"
BUILD_DIR = REPO_ROOT / "build" / "noahsark-index"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_NO_SOURCE = 3
EXIT_FORBIDDEN = 4
EXIT_NETWORK = 5

FMT_DISPLAY = "%Y-%m-%d %H:%M:%S"
TZ_CN = timezone(timedelta(hours=8))
SERVICE_KIND = "service"
USER_AGENT = "ark-service-check/2"

RFC1918 = re.compile(
    r"10\.(?:\d{1,3}\.){2}\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.(?:\d{1,3}\.)\d{1,3}"
    r"|192\.168\.(?:\d{1,3}\.)\d{1,3}"
)


class CheckError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


def now_display():
    return datetime.now(TZ_CN).strftime(FMT_DISPLAY)


def load_json(path):
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise CheckError(EXIT_NO_SOURCE, "文件不存在: %s" % path)
    except ValueError as exc:
        raise CheckError(EXIT_NO_SOURCE, "JSON 解析失败 %s: %s" % (path, exc))


def sha256_file(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def git(args, cwd):
    proc = subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace").strip(),
        proc.stderr.decode("utf-8", "replace").strip(),
    )


def describe_services(catalog_path=CATALOG_PATH):
    catalog = load_json(catalog_path)
    entries = []
    for group in catalog.get("groups") or []:
        for entry in group.get("entries") or []:
            if entry.get("kind") == SERVICE_KIND:
                entries.append(entry)
    if not entries:
        raise CheckError(EXIT_FAIL, "catalog 里没有 kind=service 的条目")
    pairs = []
    for entry in entries:
        rel = entry.get("descriptor") or ""
        if not rel:
            raise CheckError(EXIT_FAIL, "服务条目 %s 缺少 descriptor" % entry.get("id"))
        descriptor = load_json(REPO_ROOT / rel)
        pairs.append((entry, descriptor, rel))
    return catalog, pairs


def check_drift(entry, descriptor):
    errors = []
    if descriptor.get("id") != entry.get("id"):
        errors.append("id 漂移：catalog=%s descriptor=%s" % (entry.get("id"), descriptor.get("id")))
    if descriptor.get("version") != entry.get("version"):
        errors.append(
            "版本漂移：catalog=%s descriptor=%s" % (entry.get("version"), descriptor.get("version"))
        )
    endpoint = str(descriptor.get("endpoint") or "")
    host = descriptor.get("host") or {}
    if RFC1918.search(endpoint):
        errors.append("endpoint 含私网地址字面量：%s" % entry.get("id"))
    if host.get("name") and host["name"] not in endpoint:
        errors.append("endpoint 未使用可解析节点名：%s" % entry.get("id"))
    match = re.search(r":(\d{1,5})$", endpoint)
    if match and host.get("port") and int(match.group(1)) != int(host["port"]):
        errors.append("端口漂移：endpoint 端口与 host.port 不一致（%s）" % entry.get("id"))
    if not descriptor.get("auth") or not descriptor.get("api"):
        errors.append("契约缺少 auth 或 api：%s" % entry.get("id"))
    verification = descriptor.get("verification") or {}
    if verification.get("state") not in ("node-receipt", "probe", "offline-only"):
        errors.append("verification.state 非法：%s" % entry.get("id"))
    if verification.get("state") == "probe" and not verification.get("probed_at"):
        errors.append("声称已在线探测但缺少 probed_at：%s" % entry.get("id"))
    return errors


def check_baseline(descriptor, kb_repo):
    errors = []
    baseline = ((descriptor.get("baseline") or {}).get("source") or {})
    expected = baseline.get("commit")
    if not expected or not kb_repo:
        return errors
    code, out, _err = git(["rev-parse", "HEAD"], kb_repo)
    if code != 0:
        raise CheckError(EXIT_NO_SOURCE, "无法读取 ark-kb 仓库 HEAD: %s" % kb_repo)
    if out != expected:
        errors.append(
            "基线漂移：descriptor=%s 本地 ark-kb=%s（%s）" % (expected[:12], out[:12], descriptor.get("id"))
        )
    return errors


def check_rendered(build_dir, catalog, pairs, errors):
    index_path = Path(build_dir) / "index.json"
    if not index_path.exists():
        errors.append("缺少构建产物: %s" % index_path)
        return
    index = load_json(index_path)
    services = {s.get("id"): s for s in (index.get("system") or {}).get("services") or []}
    for entry, descriptor, rel in pairs:
        sid = entry["id"]
        if sid not in services:
            errors.append("index.json 的 system.services 缺少 %s" % sid)
            continue
        block = services[sid]
        if block.get("endpoint") != descriptor.get("endpoint"):
            errors.append("index.json 服务地址漂移: %s" % sid)
        if block.get("version") != descriptor.get("version"):
            errors.append("index.json 服务版本漂移: %s" % sid)
        if block.get("descriptor_sha256") != sha256_file(REPO_ROOT / rel):
            errors.append("index.json 服务契约摘要漂移: %s" % sid)
        if block.get("descriptor") != rel:
            errors.append("index.json 服务契约路径漂移: %s" % sid)

    markdown = (Path(build_dir) / "index.md")
    if markdown.exists():
        text = markdown.read_text(encoding="utf-8")
        for entry, descriptor, _rel in pairs:
            if descriptor.get("endpoint") and descriptor["endpoint"] not in text:
                errors.append("index.md 缺少服务地址: %s" % entry["id"])
            if descriptor.get("host", {}).get("port") and str(descriptor["host"]["port"]) not in text:
                errors.append("index.md 缺少服务端口: %s" % entry["id"])


def read_token(token_file, env_name="ARK_SERVICE_TOKEN"):
    """token 只来自 0600 文件或环境变量；绝不回显。"""
    if token_file:
        path = Path(token_file)
        if not path.exists():
            raise CheckError(EXIT_FORBIDDEN, "token 文件不存在: %s" % path)
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise CheckError(EXIT_FORBIDDEN, "token 文件权限过宽（需要 0600）: %s" % path)
        return path.read_text(encoding="utf-8").strip()
    token = os.environ.get(env_name, "").strip()
    if not token:
        raise CheckError(EXIT_FORBIDDEN, "缺少 token：用 --token-file 或环境变量 %s" % env_name)
    return token


def http_json(url, token, timeout=10):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return response.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001
        raise CheckError(EXIT_NETWORK, "请求失败 %s: %s" % (url, exc))


def probe(descriptor, token, timeout=10, descriptor_rel=None):
    """在线探测：只读健康与状态接口，返回可落盘的回执（不含 token）。"""
    endpoint = str(descriptor.get("endpoint") or "").rstrip("/")
    health_path = None
    for api in descriptor.get("api") or []:
        if api.get("name", "").startswith("健康检查"):
            health_path = api.get("path")
            break
    if not health_path:
        raise CheckError(EXIT_FAIL, "契约缺少健康检查路径: %s" % descriptor.get("id"))

    checks = []
    status, body = http_json(endpoint + health_path, token, timeout)
    checks.append(
        {
            "id": "health",
            "path": health_path,
            "http": status,
            "ok": status == 200,
            "bytes": len(body),
        }
    )
    health_payload = {}
    try:
        health_payload = json.loads(body.decode("utf-8"))
    except ValueError:
        checks[-1]["ok"] = False
        checks[-1]["error"] = "health 不是合法 JSON"

    snapshot = {
        "service": descriptor.get("id"),
        "version": health_payload.get("version"),
        "freshness": health_payload.get("freshness"),
        "commit": health_payload.get("commit"),
        "entries": health_payload.get("entries"),
        "degraded": health_payload.get("degraded") or [],
    }

    if descriptor.get("id") == "svc.knowledge-service":
        for name, path in (("sync_status", "/v1/sync/status"), ("search", "/v1/search?q=evidence_ref&top=1")):
            status, body = http_json(endpoint + path, token, timeout)
            item = {"id": name, "path": path, "http": status, "ok": status == 200, "bytes": len(body)}
            try:
                payload = json.loads(body.decode("utf-8"))
            except ValueError:
                payload = {}
            if name == "sync_status":
                item["source_commit"] = payload.get("source_commit")
                item["indexed_commit"] = payload.get("indexed_commit")
                item["freshness"] = payload.get("freshness")
                item["ok"] = status == 200 and payload.get("source_commit") == payload.get("indexed_commit")
            else:
                sources = payload.get("sources") or []
                item["returned"] = sum(len(s.get("entries") or []) for s in sources)
            checks.append(item)

    ok = all(c.get("ok") for c in checks)
    return {
        "schema": "ark.service-probe.v1",
        "service": descriptor.get("id"),
        "endpoint": endpoint,
        "contract_version": descriptor.get("version"),
        "descriptor_sha256": sha256_file(REPO_ROOT / descriptor_rel) if descriptor_rel else None,
        "probed_at": now_display(),
        "ok": ok,
        "checks": checks,
        "snapshot": snapshot,
        "token": "<redacted>",
        "note": "token 只用于本次请求，未写入回执；回执不含 Authorization 头。",
    }


def check_receipt(receipt_path, pairs, errors):
    receipt = load_json(receipt_path)
    if receipt.get("schema") != "ark.service-probe.v1":
        errors.append("回执 schema 不是 ark.service-probe.v1")
    by_id = {entry["id"]: (entry, descriptor) for entry, descriptor, _rel in pairs}
    sid = receipt.get("service")
    if sid not in by_id:
        errors.append("回执引用了未知服务: %s" % sid)
        return
    _entry, descriptor = by_id[sid]
    if receipt.get("endpoint") != descriptor.get("endpoint"):
        errors.append("回执地址与契约漂移: %s" % sid)
    if receipt.get("contract_version") != descriptor.get("version"):
        errors.append("回执版本与契约漂移: %s" % sid)
    if not receipt.get("ok"):
        errors.append("回执判定为失败: %s" % sid)
    if "Authorization" in json.dumps(receipt, ensure_ascii=False):
        errors.append("回执含 Authorization 头")
    checks = receipt.get("checks") or []
    if not checks:
        errors.append("回执没有探测项")
    for item in checks:
        if item.get("http") not in (200, 201, 202, 206):
            errors.append("回执探测项失败: %s HTTP %s" % (item.get("id"), item.get("http")))


def main(argv=None):
    parser = argparse.ArgumentParser(description="NoahsArk 服务契约门禁与在线探测")
    parser.add_argument("--check", action="store_true", help="离线漂移检查（默认）")
    parser.add_argument("--probe", action="store_true", help="在线探测（只在可达节点执行）")
    parser.add_argument("--receipt", default=None, help="校验节点侧回执")
    parser.add_argument("--service", default=None, help="只处理指定服务 id")
    parser.add_argument("--token-file", default=None, help="0600 token 文件（不回显）")
    parser.add_argument("--kb-repo", default=None, help="ark-kb 本地检出，用于核对基线提交")
    parser.add_argument("--dir", default=str(BUILD_DIR), help="构建产物目录")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    errors = []
    summary = {"ok": False, "checks": {}, "errors": errors, "services": []}
    try:
        _catalog, pairs = describe_services()
        if args.service:
            pairs = [p for p in pairs if p[0]["id"] == args.service]
            if not pairs:
                raise CheckError(EXIT_USAGE, "没有该服务: %s" % args.service)

        for entry, descriptor, _rel in pairs:
            summary["services"].append(
                {
                    "id": entry["id"],
                    "version": descriptor.get("version"),
                    "endpoint": descriptor.get("endpoint"),
                    "verification": (descriptor.get("verification") or {}).get("state"),
                }
            )

        if args.probe:
            receipt = None
            for entry, descriptor, rel in pairs:
                token = read_token(args.token_file)
                result = probe(descriptor, token, descriptor_rel=rel)
                summary["checks"].setdefault("probe", []).append(result)
                if not result.get("ok"):
                    errors.append("在线探测失败: %s" % entry["id"])
                receipt = result
            if receipt is not None:
                summary["receipt"] = receipt
        elif args.receipt:
            check_receipt(args.receipt, pairs, errors)
            summary["checks"]["receipt"] = not errors
        else:
            for entry, descriptor, _rel in pairs:
                errors.extend(check_drift(entry, descriptor))
                errors.extend(check_baseline(descriptor, args.kb_repo))
            check_rendered(args.dir, _catalog, pairs, errors)
            summary["checks"]["drift"] = not errors
    except CheckError as exc:
        errors.append(exc.message)
        summary["code"] = exc.code

    summary["ok"] = not errors
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        if errors:
            print("服务门禁未通过，共 %d 项：" % len(errors))
            for err in errors:
                print("  - %s" % err)
        else:
            print(
                "服务门禁通过：%s"
                % "；".join(
                    "%s %s @ %s（%s）"
                    % (s["id"], s["version"], s["endpoint"], s["verification"])
                    for s in summary["services"]
                )
            )
    if errors:
        return summary.get("code") or (
            EXIT_NETWORK if any("请求失败" in e for e in errors) else EXIT_FAIL
        )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
