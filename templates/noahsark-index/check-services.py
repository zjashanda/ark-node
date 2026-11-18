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


def check_discovery_drift(descriptor):
    sid = descriptor.get("id")
    discovery = descriptor.get("discovery") or {}
    canonical = discovery.get("canonical") or {}
    host = descriptor.get("host") or {}
    errors = []
    if canonical.get("endpoint") != descriptor.get("endpoint"):
        errors.append("SERVICE_DISCOVERY_DRIFT：%s 的 canonical.endpoint 与 endpoint 不一致" % sid)
    if canonical.get("node_name") != host.get("name"):
        errors.append("SERVICE_DISCOVERY_DRIFT：%s 的 canonical.node_name 与 host.name 不一致" % sid)
    if canonical.get("port") != host.get("port"):
        errors.append("SERVICE_DISCOVERY_DRIFT：%s 的 canonical.port 与 host.port 不一致" % sid)
    entry_kind = canonical.get("entry_kind") or "node-name"
    if entry_kind != "fixed-ip":
        for profile in discovery.get("node_profiles") or []:
            node = profile.get("node")
            verified = profile.get("verified_endpoint")
            if verified and verified == canonical.get("endpoint") and node != canonical.get("node_name"):
                errors.append(
                    "SERVICE_DISCOVERY_DRIFT：%s 的节点 %s profile 被复制成 canonical.endpoint" % (sid, node)
                )
    return errors


def check_anonymous_drift(descriptor):
    sid = descriptor.get("id")
    anon = (descriptor.get("auth") or {}).get("anonymous_read") or {}
    if not anon:
        return []
    errors = []
    if anon.get("schema") != "ark.service-anonymous-read.v1":
        errors.append("SERVICE_DISCOVERY_DRIFT：%s 的 anonymous_read.schema 非法" % sid)
    if anon.get("status") not in ("pending_service_change", "verified"):
        errors.append("SERVICE_DISCOVERY_DRIFT：%s 的 anonymous_read.status 非法" % sid)
    public = set(anon.get("public_api") or [])
    protected = set(anon.get("protected_api") or [])
    if not public or not protected:
        errors.append("SERVICE_DISCOVERY_DRIFT：%s 的 anonymous_read 公开/受保护接口不能为空" % sid)
    if public & protected:
        errors.append("SERVICE_DISCOVERY_DRIFT：%s 的 anonymous_read 公开与受保护接口重叠" % sid)
    declared = {
        "%s %s" % (api.get("method"), api.get("path"))
        for api in descriptor.get("api") or []
        if api.get("scope") == "anonymous"
    }
    if declared != public:
        errors.append(
            "SERVICE_DISCOVERY_DRIFT：%s 的 anonymous_read.public_api 与 api[].scope=anonymous 不一致" % sid
        )
    if anon.get("status") == "pending_service_change":
        change = anon.get("service_change") or {}
        if change.get("required") is not True or not change.get("doc"):
            errors.append("SERVICE_DISCOVERY_DRIFT：%s 的 anonymous_read 待改状态缺少 service_change" % sid)
    token = descriptor.get("token_bootstrap") or {}
    if token.get("status") == "operational":
        if not (token.get("verification") or {}).get("observed"):
            errors.append("SERVICE_DISCOVERY_DRIFT：%s 的 token_bootstrap operational 缺少观测证据" % sid)
        if token.get("bootstrap_api") is not True:
            errors.append("SERVICE_DISCOVERY_DRIFT：%s 的 token_bootstrap operational 但 bootstrap_api 非 true" % sid)
    return errors


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
    canonical = (descriptor.get("discovery") or {}).get("canonical") or {}
    entry_kind = canonical.get("entry_kind") or "node-name"
    if entry_kind == "fixed-ip":
        if endpoint != canonical.get("fixed_entry"):
            errors.append("fixed-ip endpoint 与 canonical.fixed_entry 不一致：%s" % entry.get("id"))
    else:
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
    errors.extend(check_discovery_drift(descriptor))
    errors.extend(check_anonymous_drift(descriptor))
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
        canonical = (descriptor.get("discovery") or {}).get("canonical") or {}
        entry_kind = canonical.get("entry_kind") or "node-name"
        if block.get("entry_kind") != entry_kind:
            errors.append("index.json 服务 entry_kind 漂移: %s" % sid)
        if entry_kind == "fixed-ip":
            expected_ref = "descriptor:%s#discovery.canonical.fixed_entry" % rel
            if block.get("endpoint_ref") != expected_ref:
                errors.append("index.json 固定入口 endpoint_ref 漂移: %s" % sid)
            if block.get("endpoint"):
                errors.append("index.json fixed-ip 服务不得输出 endpoint: %s" % sid)
        elif block.get("endpoint") != descriptor.get("endpoint"):
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
            canonical = (descriptor.get("discovery") or {}).get("canonical") or {}
            entry_kind = canonical.get("entry_kind") or "node-name"
            if entry_kind == "fixed-ip":
                if descriptor.get("endpoint") and descriptor["endpoint"] in text:
                    errors.append("index.md 不得出现固定入口地址字面量: %s" % entry["id"])
                if "固定入口（节点侧登记）" not in text:
                    errors.append("index.md 缺少固定入口占位文本: %s" % entry["id"])
            elif descriptor.get("endpoint") and descriptor["endpoint"] not in text:
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


def resolve_probe_base(descriptor, node=None):
    if not node:
        canonical = (descriptor.get("discovery") or {}).get("canonical") or {}
        entry_kind = canonical.get("entry_kind") or "node-name"
        base = canonical.get("endpoint") if entry_kind == "fixed-ip" else descriptor.get("endpoint")
        return str(base or "").rstrip("/")
    profiles = (descriptor.get("discovery") or {}).get("node_profiles") or []
    for profile in profiles:
        if profile.get("node") == node:
            endpoint = str(profile.get("verified_endpoint") or "").rstrip("/")
            if not endpoint:
                raise CheckError(
                    EXIT_FAIL,
                    "节点 %s 的 profile 缺少 verified_endpoint: %s" % (node, descriptor.get("id")),
                )
            return endpoint
    raise CheckError(
        EXIT_FAIL,
        "节点 %s 不在服务 %s 的 discovery.node_profiles 中（不回退 canonical）"
        % (node, descriptor.get("id")),
    )


def probe(descriptor, token, timeout=10, descriptor_rel=None, node=None):
    """在线探测：按 health_contract 只读健康与同步状态，返回可落盘回执（不含 token）。"""
    health_contract = descriptor.get("health_contract") or {}
    probe_conf = health_contract.get("probe") or {}
    health_path = probe_conf.get("path")
    if not health_path:
        raise CheckError(EXIT_FAIL, "契约缺少 health_contract.probe.path: %s" % descriptor.get("id"))
    base = resolve_probe_base(descriptor, node)
    query = probe_conf.get("strict_query") or ""

    status, body = http_json(base + health_path + query, token, timeout)
    payload = None
    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError:
        payload = None
    required = list(health_contract.get("required_fields") or [])
    missing = [field for field in required if not isinstance(payload, dict) or field not in payload]
    health_fields_ok = bool(isinstance(payload, dict) and not missing)

    checks = [
        {
            "id": "health",
            "path": health_path + query,
            "http": status,
            "ok": status == 200 and health_fields_ok,
            "bytes": len(body),
            "fields_ok": health_fields_ok,
            "missing_fields": missing,
        }
    ]
    if payload is None:
        checks[-1]["error"] = "health 不是合法 JSON"

    version_field = health_contract.get("version_field") or "version"
    version = payload.get(version_field) if isinstance(payload, dict) else None
    source_commit = payload.get("source_commit") if isinstance(payload, dict) else None
    indexed_commit = payload.get("indexed_commit") if isinstance(payload, dict) else None
    freshness = payload.get("freshness") if isinstance(payload, dict) else None
    degraded = payload.get("degraded") if isinstance(payload, dict) else None

    if health_contract.get("commits"):
        status, body = http_json(base + "/v1/sync/status", token, timeout)
        item = {
            "id": "sync_status",
            "path": "/v1/sync/status",
            "http": status,
            "ok": False,
            "bytes": len(body),
        }
        sync_payload = {}
        try:
            sync_payload = json.loads(body.decode("utf-8"))
        except ValueError:
            sync_payload = {}
        item["source_commit"] = sync_payload.get("source_commit")
        item["indexed_commit"] = sync_payload.get("indexed_commit")
        item["freshness"] = sync_payload.get("freshness")
        item["ok"] = status == 200 and sync_payload.get("source_commit") == sync_payload.get("indexed_commit")
        checks.append(item)
        source_commit = sync_payload.get("source_commit") or source_commit
        indexed_commit = sync_payload.get("indexed_commit") or indexed_commit
        freshness = sync_payload.get("freshness") or freshness

    anon = (descriptor.get("auth") or {}).get("anonymous_read") or {}
    anonymous_read = None
    if anon:
        observed = {}
        for entry in anon.get("public_api") or []:
            method, _, route = entry.partition(" ")
            if method != "GET":
                continue
            if route == "/v1/search":
                target = "/v1/search?q=probe&top=1"
            else:
                target = route.replace("{entry_id}", "probe-missing").replace("{doc_id}", "probe-missing")
            code, _body = http_json(base + target, None, timeout)
            observed[entry] = code
        anonymous_ok = bool(observed) and all(code == 200 for code in observed.values())
        anonymous_read = {
            "contract": anon.get("status"),
            "verified": anonymous_ok,
            "observed": observed,
            "note": "匿名探测不带任何 token；pending_service_change 阶段 401 属预期，不判失败。",
        }
        checks.append(
            {
                "id": "anonymous_read",
                "contract": anon.get("status"),
                "ok": anonymous_ok if anon.get("status") == "verified" else True,
                "verified": anonymous_ok,
            }
        )

    snapshot = {
        "service": descriptor.get("id"),
        "version": version,
        "freshness": freshness,
        "commit": payload.get("commit") if isinstance(payload, dict) else None,
        "entries": payload.get("entries") if isinstance(payload, dict) else None,
        "degraded": degraded or [],
    }

    ok = all(check.get("ok") for check in checks)
    return {
        "schema": "ark.service-probe.v1",
        "service": descriptor.get("id"),
        "endpoint": base,
        "contract_version": descriptor.get("version"),
        "descriptor_sha256": sha256_file(REPO_ROOT / descriptor_rel) if descriptor_rel else None,
        "probed_at": now_display(),
        "ok": ok,
        "health_fields_ok": health_fields_ok,
        "version": version,
        "source_commit": source_commit,
        "indexed_commit": indexed_commit,
        "freshness": freshness,
        "degraded": degraded,
        "write_path_proven": False,
        "write_path_rule": health_contract.get("write_path_rule"),
        "checks": checks,
        "anonymous_read": anonymous_read,
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
    allowed_endpoints = {descriptor.get("endpoint")}
    for profile in (descriptor.get("discovery") or {}).get("node_profiles") or []:
        if profile.get("verified_endpoint"):
            allowed_endpoints.add(profile["verified_endpoint"])
    if receipt.get("endpoint") not in allowed_endpoints:
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
    parser = argparse.ArgumentParser(
        description=(
            "NoahsArk 服务契约门禁与在线探测（--check 漂移 / --probe 在线 / --receipt 回执）；"
            "--json 输出含 entry_kind（node-name / fixed-ip）"
        ),
        epilog=(
            "--node <name> 只用于 --probe：按 discovery.node_profiles 取该节点 verified_endpoint，"
            "不存在的节点直接失败，不回退 canonical。"
            "entry_kind=fixed-ip 时无 --node 的探测基址取 discovery.canonical.endpoint（固定入口）；"
            "entry_kind=node-name 时取 descriptor.endpoint。失败码：SERVICE_DISCOVERY_DRIFT、"
            "BLOCKED_STORAGE_AUTH、BLOCKED_KB_AUTH、STALE_INDEX、EVIDENCE_GAP。"
        ),
    )
    parser.add_argument("--check", action="store_true", help="离线漂移检查（默认）")
    parser.add_argument("--probe", action="store_true", help="在线探测（只在可达节点执行）")
    parser.add_argument("--node", default=None, help="探测基址取该节点的 discovery profile（仅 --probe）")
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
            discovery = descriptor.get("discovery") or {}
            canonical = discovery.get("canonical") or {}
            token = descriptor.get("token_bootstrap") or {}
            summary["services"].append(
                {
                    "id": entry["id"],
                    "version": descriptor.get("version"),
                    "entry_kind": canonical.get("entry_kind") or "node-name",
                    "endpoint": descriptor.get("endpoint"),
                    "verification": (descriptor.get("verification") or {}).get("state"),
                    "discovery": {
                        "canonical_endpoint": canonical.get("endpoint"),
                        "profiles": [p.get("node") for p in discovery.get("node_profiles") or []],
                        "vm_boundary": list(
                            (discovery.get("vm_boundary") or {}).get("nodes") or []
                        ),
                    },
                    "token_bootstrap": {
                        "mode": token.get("mode"),
                        "bootstrap_api": token.get("bootstrap_api"),
                        "status": token.get("status"),
                    },
                }
            )

        if args.node and not args.probe:
            raise CheckError(EXIT_USAGE, "--node 只能与 --probe 一起使用")

        if args.probe:
            receipt = None
            for entry, descriptor, rel in pairs:
                token = read_token(args.token_file)
                result = probe(descriptor, token, descriptor_rel=rel, node=args.node)
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
