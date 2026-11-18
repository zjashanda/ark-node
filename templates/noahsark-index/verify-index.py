#!/usr/bin/env python3
"""校验已生成的总览三件套。

默认离线：只读本地文件，检查结构、计数、依赖、链接形态、双声明一致与校验值。
加 --online：逐个匿名读取对外地址，确认可访问且校验值一致。
加 --check-git：用 GitHub API 确认仓库存在（不抓取网页）。

门禁要点（任一不满足即失败）：
1. index.md 只允许两个一级区域，标题与 index.json 的 regions 一致；
2. index.json 必须有 system / onboarding 两个顶层导航域；
3. 文档与 JSON 双声明必须一致：每个条目、分组、服务、初始化步骤都要在对应区域出现；
4. 服务地址只允许可解析节点名，禁止 RFC1918 私网地址字面量；
5. secret_scan 与 time_lint 必须通过（--skip-tools 仅供本地调试）。

退出码：0 通过，1 不通过，2 用法错误，5 网络不可用。
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
TOOLS = REPO_ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

INDEX_SCHEMA = "ark.noahsark-index.v2"
ANON_PROPOSAL_SCHEMA = "ark.service-anonymous-proposal.v1"
ANON_PROPOSAL_GATES = ("lan_source", "evidence", "idempotency", "status", "quota", "audit")
ANON_PROPOSAL_REQUIRED_FIELDS = (
    "provenance.issue",
    "provenance.run",
    "provenance.agent",
    "provenance.node",
    "evidence[].bytes",
    "evidence[].sha256",
    "request_id",
    "idempotency_key",
)
ANON_PROPOSAL_FORBIDDEN = ("review", "close", "promote", "sync", "admin")
MANIFEST_SCHEMA = "ark.noahsark-index.manifest.v1"
REGION_ORDER = ("system", "onboarding")
SERVICE_KIND = "service"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_NETWORK = 5

HEX40 = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{40,}(?![0-9A-Za-z])")
RFC1918 = re.compile(
    r"10\.(?:\d{1,3}\.){2}\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.(?:\d{1,3}\.)\d{1,3}"
    r"|192\.168\.(?:\d{1,3}\.)\d{1,3}"
)
LEGACY_ENTRY = re.compile(r"http://bszT14:" + "8091")
ENDPOINT_REF = re.compile(r"^descriptor:templates/.*#discovery\.canonical\.fixed_entry$")
ENTRY_KINDS = ("node-name", "fixed-ip")
REQUIRED_ITEM_KEYS = (
    "id",
    "region",
    "title",
    "kind",
    "status",
    "version",
    "path",
    "url",
    "sha256",
    "source_repo",
    "source_commit",
    "roles",
    "requires",
    "summary",
)
REQUIRED_REGION_ITEM_KEYS = (
    "id",
    "region",
    "title",
    "kind",
    "status",
    "version",
    "url",
    "sha256",
    "source_repo",
    "source_commit",
)


class Fail(Exception):
    pass


def load(path):
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise Fail("缺少文件: %s" % path)
    except ValueError as exc:
        raise Fail("JSON 解析失败 %s: %s" % (path, exc))


def read_bytes(path):
    with open(str(path), "rb") as handle:
        return handle.read()


def check_manifest(out_dir, manifest, index, errors):
    if manifest.get("schema") != MANIFEST_SCHEMA:
        errors.append("manifest.schema 不是 %s" % MANIFEST_SCHEMA)
    for item in manifest.get("files") or []:
        local = out_dir / item["path"]
        if not local.exists():
            errors.append("manifest 指向的文件不存在: %s" % item["path"])
            continue
        data = read_bytes(local)
        if len(data) != item.get("bytes"):
            errors.append("字节数不一致: %s" % item["path"])
        digest = __import__("hashlib").sha256(data).hexdigest()
        if digest != item.get("sha256"):
            errors.append("校验值不一致: %s" % item["path"])
    if not isinstance((manifest.get("git") or {}).get("commit"), str):
        errors.append("manifest.git.commit 缺失")
    if manifest.get("version") != index.get("version"):
        errors.append("manifest.version 与 index.version 不一致")
    if (manifest.get("git") or {}).get("commit") != (index.get("git") or {}).get("commit"):
        errors.append("manifest.git.commit 与 index.git.commit 不一致")
    counts = manifest.get("counts") or {}
    index_counts = index.get("counts") or {}
    for key in ("items", "available", "planned", "roles", "groups", "regions", "services", "onboarding_steps"):
        if counts.get(key) != index_counts.get(key):
            errors.append("manifest.counts.%s 与 index.counts 不一致" % key)
    if len(manifest.get("regions") or []) != len(REGION_ORDER):
        errors.append("manifest.regions 不是两个区域")
    manifest_services = {s.get("id") for s in manifest.get("services") or []}
    index_services = {i.get("id") for i in index.get("items") or [] if i.get("kind") == SERVICE_KIND}
    if manifest_services != index_services:
        errors.append("manifest.services 与 index 服务条目不一致")


def check_index(index, errors):
    if index.get("schema") != INDEX_SCHEMA:
        errors.append("index.schema 不是 %s" % INDEX_SCHEMA)
    items = index.get("items") or []
    if not items:
        errors.append("index.items 为空")
        return items
    ids = [i.get("id") for i in items]
    if len(ids) != len(set(ids)):
        errors.append("index.items 存在重复 id")
    known = set(ids)
    role_ids = set(index.get("role_ids") or [])
    if not role_ids:
        errors.append("index.role_ids 为空")

    available = 0
    for item in items:
        iid = item.get("id")
        for key in REQUIRED_ITEM_KEYS:
            if key not in item:
                errors.append("条目 %s 缺少字段 %s" % (iid, key))
        if item.get("region") not in REGION_ORDER:
            errors.append("条目 %s 的 region 非法" % iid)
        if item.get("status") == "available":
            available += 1
            if item.get("kind") == SERVICE_KIND:
                if not item.get("url") and not item.get("endpoint_ref"):
                    errors.append("可用服务条目 %s 缺少地址或 endpoint_ref" % iid)
            elif not item.get("url"):
                errors.append("可用条目 %s 缺少地址" % iid)
            if not item.get("version"):
                errors.append("可用条目 %s 缺少版本" % iid)
        elif item.get("status") == "planned":
            if item.get("url"):
                errors.append("规划中条目 %s 不应带地址" % iid)
        else:
            errors.append("条目 %s 的 status 非法" % iid)
        for role in item.get("roles") or []:
            if role not in role_ids:
                errors.append("条目 %s 引用了未知角色 %s" % (iid, role))
        for dep in item.get("requires") or []:
            if dep not in known:
                errors.append("条目 %s 依赖未知条目 %s" % (iid, dep))
        sha = item.get("sha256")
        if sha is not None and not re.match(r"^[0-9a-f]{64}$", sha):
            errors.append("条目 %s 的 sha256 格式非法" % iid)
        if item.get("kind") == SERVICE_KIND:
            if not item.get("descriptor"):
                errors.append("服务条目 %s 缺少 descriptor" % iid)
            entry_kind = item.get("entry_kind")
            if entry_kind not in ENTRY_KINDS:
                errors.append("服务条目 %s 的 entry_kind 非法" % iid)
            has_endpoint = bool(item.get("endpoint"))
            has_ref = bool(item.get("endpoint_ref"))
            if has_endpoint == has_ref:
                errors.append("服务条目 %s 的 endpoint 与 endpoint_ref 必须恰有其一" % iid)
            if has_endpoint and RFC1918.search(item["endpoint"]):
                errors.append("服务条目 %s 的 endpoint 含私网地址字面量" % iid)
            if has_ref and not ENDPOINT_REF.match(item["endpoint_ref"]):
                errors.append("服务条目 %s 的 endpoint_ref 形态非法" % iid)
            if entry_kind == "fixed-ip" and has_endpoint:
                errors.append("fixed-ip 服务条目 %s 不得输出 endpoint" % iid)
        commit = item.get("source_commit")
        if commit is not None and not re.match(r"^[0-9a-f]{7,40}$", str(commit)):
            errors.append("条目 %s 的 source_commit 形态非法" % iid)

    counts = index.get("counts") or {}
    if counts.get("items") != len(items):
        errors.append("counts.items 与实际条目数不一致")
    if counts.get("available") != available:
        errors.append("counts.available 与实际可用条目数不一致")
    if counts.get("regions") != len(REGION_ORDER):
        errors.append("counts.regions 不是 2")
    if counts.get("services") != sum(1 for i in items if i.get("kind") == SERVICE_KIND):
        errors.append("counts.services 与实际服务条目数不一致")

    for role in index.get("roles") or []:
        if not role.get("must_read"):
            errors.append("角色 %s 的必读清单为空" % role.get("id"))
        for ref in list(role.get("must_read") or []) + list(role.get("optional") or []):
            if (ref or {}).get("id") not in known:
                errors.append("角色 %s 引用了未知条目 %s" % (role.get("id"), (ref or {}).get("id")))
    return items


def check_regions(index, items, errors):
    navigation = index.get("navigation") or []
    nav_ids = [n.get("id") for n in navigation]
    if nav_ids != list(REGION_ORDER):
        errors.append("index.navigation 必须是 %s" % "/".join(REGION_ORDER))

    groups = index.get("groups") or []
    group_region = {g.get("id"): g.get("region") for g in groups}
    item_by_id = {i["id"]: i for i in items}

    for region_id in REGION_ORDER:
        block = index.get(region_id)
        if not isinstance(block, dict):
            errors.append("index 缺少顶层导航域 %s" % region_id)
            continue
        if block.get("id") != region_id:
            errors.append("index.%s.id 不正确" % region_id)
        if not block.get("title") or not block.get("summary"):
            errors.append("index.%s 缺少 title/summary" % region_id)
        if not block.get("lead"):
            errors.append("index.%s 缺少 lead" % region_id)
        block_groups = block.get("groups") or []
        expected_groups = [g["id"] for g in groups if g.get("region") == region_id]
        if [g.get("id") for g in block_groups] != expected_groups:
            errors.append("index.%s.groups 与顶层 groups 的区域划分不一致" % region_id)
        expected_ids = [
            i["id"] for i in items if i.get("region") == region_id
        ]
        refs = block.get("items") or []
        if [r.get("id") for r in refs] != expected_ids:
            errors.append("index.%s.items 与顶层 items 的区域划分不一致" % region_id)
        for ref in refs:
            for key in REQUIRED_REGION_ITEM_KEYS:
                if key not in ref:
                    errors.append("区域 %s 的条目 %s 缺少字段 %s" % (region_id, ref.get("id"), key))
            if ref.get("id") not in item_by_id:
                errors.append("区域 %s 引用了未知条目 %s" % (region_id, ref.get("id")))
        for group in block_groups:
            for iid in group.get("items") or []:
                if iid not in item_by_id:
                    errors.append("区域 %s 的分组 %s 引用了未知条目 %s" % (region_id, group.get("id"), iid))
                elif group_region.get(group.get("id")) != region_id:
                    errors.append("分组 %s 的区域与父区域不一致" % group.get("id"))

    system = index.get("system") or {}
    for field in (
        "positioning",
        "layers",
        "lifecycle",
        "knowledge_loop",
        "gates",
        "security",
        "failure_taxonomy",
        "service_discovery",
        "services",
        "roles",
    ):
        if not system.get(field):
            errors.append("index.system.%s 不能为空" % field)
    check_failure_taxonomy(index, errors)
    service_ids = {s.get("id") for s in system.get("services") or []}
    item_service_ids = {i["id"] for i in items if i.get("kind") == SERVICE_KIND}
    if service_ids != item_service_ids:
        errors.append("index.system.services 与服务条目不一致")
    for svc in system.get("services") or []:
        for key in (
            "id",
            "title",
            "version",
            "entry_kind",
            "descriptor",
            "discovery",
            "token_bootstrap",
            "health",
            "auth",
            "anonymous_proposal",
            "verification",
        ):
            if key not in svc:
                errors.append("服务块 %s 缺少字段 %s" % (svc.get("id"), key))
        entry_kind = svc.get("entry_kind")
        if entry_kind not in ENTRY_KINDS:
            errors.append("服务块 %s 的 entry_kind 非法" % svc.get("id"))
        has_endpoint = "endpoint" in svc
        has_ref = "endpoint_ref" in svc
        if has_endpoint == has_ref:
            errors.append("服务块 %s 的 endpoint 与 endpoint_ref 必须恰有其一" % svc.get("id"))
        if has_endpoint and RFC1918.search(svc["endpoint"]):
            errors.append("服务块 %s 的 endpoint 含私网地址字面量" % svc.get("id"))
        if has_ref and not ENDPOINT_REF.match(str(svc["endpoint_ref"])):
            errors.append("服务块 %s 的 endpoint_ref 形态非法" % svc.get("id"))
        if entry_kind == "fixed-ip" and has_endpoint:
            errors.append("fixed-ip 服务块 %s 不得输出 endpoint" % svc.get("id"))
        check_service_discovery(svc, errors)
        health = svc.get("health") or {}
        for key in ("path", "required_fields", "status_rule", "write_path_rule"):
            if not health.get(key):
                errors.append("服务块 %s 的 health 缺少 %s" % (svc.get("id"), key))
        descriptor = REPO_ROOT / (svc.get("descriptor") or "")
        if not descriptor.exists():
            errors.append("服务块 %s 的 descriptor 不存在" % svc.get("id"))
        else:
            data = load(descriptor)
            if data.get("id") != svc.get("id"):
                errors.append("服务块 %s 与 descriptor.id 不一致" % svc.get("id"))
            if data.get("version") != svc.get("version"):
                errors.append("服务块 %s 与 descriptor.version 不一致" % svc.get("id"))
            descriptor_kind = (
                (data.get("discovery") or {}).get("canonical") or {}
            ).get("entry_kind") or "node-name"
            if descriptor_kind == "fixed-ip":
                expected_ref = "descriptor:%s#discovery.canonical.fixed_entry" % svc.get("descriptor")
                if svc.get("endpoint_ref") != expected_ref:
                    errors.append("服务块 %s 与 descriptor 固定入口引用不一致" % svc.get("id"))
            elif data.get("endpoint") != svc.get("endpoint"):
                errors.append("服务块 %s 与 descriptor.endpoint 不一致" % svc.get("id"))
            anon = (data.get("auth") or {}).get("anonymous_read") or {}
            if anon:
                block = svc.get("anonymous_read") or {}
                if not block:
                    errors.append("服务块 %s 缺少 anonymous_read（descriptor 已声明）" % svc.get("id"))
                else:
                    if block.get("status") not in ("pending_service_change", "verified"):
                        errors.append("服务块 %s 的 anonymous_read.status 非法" % svc.get("id"))
                    if block.get("status") != anon.get("status"):
                        errors.append("服务块 %s 的 anonymous_read.status 与 descriptor 不一致" % svc.get("id"))
                    if not block.get("public_api") or not block.get("protected_api"):
                        errors.append("服务块 %s 的 anonymous_read 公开/受保护接口不能为空" % svc.get("id"))
                    if set(block.get("public_api") or []) & set(block.get("protected_api") or []):
                        errors.append("服务块 %s 的 anonymous_read 公开与受保护接口重叠" % svc.get("id"))
                    if block.get("status") == "pending_service_change" and not (
                        (block.get("service_change") or {}).get("required")
                    ):
                        errors.append("服务块 %s 的 anonymous_read 待改状态缺少 service_change" % svc.get("id"))
            check_anonymous_proposal(svc, data, errors)
            check_anonymous_scope(data, svc.get("id"), errors)
            token = data.get("token_bootstrap") or {}
            if token.get("status") == "operational":
                evidence = token.get("verification") or {}
                if not evidence.get("observed"):
                    errors.append("服务块 %s 的 token_bootstrap 声称 operational 但无观测证据" % svc.get("id"))
                served = svc.get("token_bootstrap") or {}
                if not (served.get("verification") or {}).get("observed_count"):
                    errors.append("服务块 %s 未携带 token_bootstrap 观测证据" % svc.get("id"))

    onboarding = index.get("onboarding") or {}
    for field in ("automation_levels", "steps", "acceptance", "block_codes", "drift"):
        if not onboarding.get(field):
            errors.append("index.onboarding.%s 不能为空" % field)
    codes = {c.get("code") for c in onboarding.get("block_codes") or []}
    accepted = {a.get("id") for a in onboarding.get("acceptance") or []}
    step_ids = [s.get("id") for s in onboarding.get("steps") or []]
    if len(step_ids) != len(set(step_ids)):
        errors.append("onboarding.steps 存在重复 id")
    for step in onboarding.get("steps") or []:
        if step.get("automation") not in ("auto", "confirm", "admin"):
            errors.append("步骤 %s 的 automation 非法" % step.get("id"))
        for dep in step.get("requires") or []:
            if dep not in step_ids:
                errors.append("步骤 %s 依赖未知步骤 %s" % (step.get("id"), dep))
        for acc in step.get("acceptance") or []:
            if acc not in accepted:
                errors.append("步骤 %s 引用未知验收项 %s" % (step.get("id"), acc))
        for code in step.get("block_codes") or []:
            if code not in codes:
                errors.append("步骤 %s 引用未登记阻塞码 %s" % (step.get("id"), code))
    if counts := index.get("counts") or {}:
        if counts.get("onboarding_steps") != len(step_ids):
            errors.append("counts.onboarding_steps 与实际步骤数不一致")



def check_anonymous_proposal(svc, data, errors):
    sid = svc.get("id")
    proposal = (data.get("auth") or {}).get("anonymous_proposal") or {}
    block = svc.get("anonymous_proposal") or {}
    if not proposal:
        if block:
            errors.append("服务块 %s 输出 anonymous_proposal 但 descriptor 未声明" % sid)
        return
    if not block:
        errors.append("服务块 %s 缺少 anonymous_proposal（descriptor 已声明）" % sid)
        return
    if block.get("schema") != ANON_PROPOSAL_SCHEMA:
        errors.append("服务块 %s 的 anonymous_proposal.schema 非法" % sid)
    if block.get("status") not in ("pending_service_change", "verified"):
        errors.append("服务块 %s 的 anonymous_proposal.status 非法" % sid)
    if block.get("status") != proposal.get("status"):
        errors.append("服务块 %s 的 anonymous_proposal.status 与 descriptor 不一致" % sid)
    if block.get("endpoint") != "POST /v1/proposals":
        errors.append("服务块 %s 的 anonymous_proposal.endpoint 非法" % sid)
    if list(block.get("allowed_status") or []) != ["pending"]:
        errors.append("服务块 %s 的 anonymous_proposal.allowed_status 必须是 ['pending']" % sid)
    fields = set(block.get("required_fields") or [])
    for field in ANON_PROPOSAL_REQUIRED_FIELDS:
        if field not in fields:
            errors.append("服务块 %s 的 anonymous_proposal.required_fields 缺少 %s" % (sid, field))
    gates = block.get("gates") or {}
    for gate in ANON_PROPOSAL_GATES:
        if not gates.get(gate):
            errors.append("服务块 %s 的 anonymous_proposal.gates 缺少 %s" % (sid, gate))
    forbidden = set(block.get("forbidden") or [])
    for action in ANON_PROPOSAL_FORBIDDEN:
        if action not in forbidden:
            errors.append("服务块 %s 的 anonymous_proposal.forbidden 缺少 %s" % (sid, action))
    if not (block.get("service_change") or {}).get("required"):
        errors.append("服务块 %s 的 anonymous_proposal 缺少 service_change" % sid)
    if not (proposal.get("service_change") or {}).get("required"):
        errors.append("服务块 %s 的 descriptor anonymous_proposal 缺少 service_change" % sid)


def check_anonymous_scope(data, sid, errors):
    apis = data.get("api") or []
    anon = (data.get("auth") or {}).get("anonymous_read") or {}
    if anon:
        declared = {
            "%s %s" % (api.get("method"), api.get("path"))
            for api in apis
            if (api.get("scope") or "") == "anonymous"
            and (api.get("method") or "").upper() == "GET"
        }
        if declared != set(anon.get("public_api") or []):
            errors.append(
                "服务块 %s 的 anonymous_read.public_api 与 api[] 匿名 GET 条目不一致" % sid
            )
    posts = {
        "%s %s" % (api.get("method"), api.get("path"))
        for api in apis
        if (api.get("scope") or "") == "anonymous"
        and (api.get("method") or "").upper() == "POST"
    }
    proposal = (data.get("auth") or {}).get("anonymous_proposal")
    if proposal:
        if proposal.get("endpoint") not in posts:
            errors.append(
                "服务块 %s 的 anonymous_proposal.endpoint 未在 api[] 标记 scope=anonymous" % sid
            )
    elif posts:
        errors.append("服务块 %s 存在匿名 POST 接口但未声明 anonymous_proposal" % sid)

def check_service_discovery(svc, errors):
    sid = svc.get("id")
    discovery = svc.get("discovery") or {}
    for key in ("canonical", "resolution", "node_profiles", "redaction"):
        if key not in discovery:
            errors.append("服务块 %s 的 discovery 缺少 %s" % (sid, key))
    canonical = discovery.get("canonical") or {}
    for key in (
        "node_name",
        "port",
        "scheme",
        "entry_kind",
        "fixed_entry_present",
        "fixed_entry_ref",
    ):
        if key not in canonical:
            errors.append("服务块 %s 的 discovery.canonical 缺少 %s" % (sid, key))
    entry_kind = canonical.get("entry_kind")
    if entry_kind not in ENTRY_KINDS:
        errors.append("服务块 %s 的 discovery.canonical.entry_kind 非法" % sid)
    if entry_kind == "fixed-ip":
        if "endpoint" in canonical:
            errors.append("fixed-ip 服务块 %s 的 canonical 不得含 endpoint" % sid)
    elif "endpoint" not in canonical:
        errors.append("服务块 %s 的 discovery.canonical 缺少 endpoint" % sid)
    resolution = discovery.get("resolution") or {}
    if resolution.get("order") != ["node_profile", "canonical"]:
        errors.append("服务块 %s 的 discovery.resolution.order 非法" % sid)
    profiles = discovery.get("node_profiles") or []
    if not profiles:
        errors.append("服务块 %s 的 discovery.node_profiles 不能为空" % sid)
    for profile in profiles:
        for key in ("node", "verified", "receipt", "scope"):
            if key not in profile:
                errors.append("服务块 %s 的 node_profiles 缺少 %s" % (sid, key))
        if "verified_endpoint" in profile:
            errors.append("服务块 %s 的 node_profiles 不得含 verified_endpoint" % sid)
    internal = (discovery.get("redaction") or {}).get("internal_only_fields") or []
    for field in ("discovery.canonical.fixed_entry", "discovery.node_profiles[].verified_endpoint"):
        if field not in internal:
            errors.append("服务块 %s 的 discovery.redaction.internal_only_fields 缺少 %s" % (sid, field))


def check_failure_taxonomy(index, errors):
    rules = ((index.get("system") or {}).get("failure_taxonomy")) or []
    codes = [rule.get("code") for rule in rules]
    if len(codes) != len(set(codes)):
        errors.append("failure_taxonomy 存在重复 code")
    for rule in rules:
        for key in ("code", "when", "action"):
            if not rule.get(key):
                errors.append("failure_taxonomy 条目缺少 %s" % key)
        if not isinstance(rule.get("stop"), bool):
            errors.append("failure_taxonomy %s 的 stop 必须是布尔" % rule.get("code"))
    onboarding_codes = {
        code.get("code") for code in ((index.get("onboarding") or {}).get("block_codes") or [])
    }
    for code in codes:
        if code not in onboarding_codes:
            errors.append("failure_taxonomy 的 %s 未登记在 onboarding.block_codes" % code)


def check_json_literals(index, errors):
    blob = json.dumps(index, ensure_ascii=False)
    if RFC1918.search(blob):
        errors.append("index.json 含私网地址字面量")
    if LEGACY_ENTRY.search(blob):
        errors.append("index.json 含已失效旧入口（应为固定入口 endpoint_ref）")


def check_index_urls(index, errors):
    base = (index.get("entry") or {}).get("markdown") or ""
    if not base:
        errors.append("index.entry.markdown 缺失")
    for item in index.get("items") or []:
        url = item.get("url") or ""
        if not url:
            continue
        if item.get("kind") == SERVICE_KIND:
            if not url.startswith("http://") and not url.startswith("https://"):
                errors.append("服务条目 %s 的地址形态异常" % item.get("id"))
            if RFC1918.search(url):
                errors.append("服务条目 %s 的地址含私网字面量" % item.get("id"))
            continue
        if not url.startswith("https://"):
            errors.append("条目 %s 的地址不是 https" % item.get("id"))
        if item.get("kind") == "git" and not url.startswith("https://github.com/"):
            errors.append("条目 %s 的 Git 地址形态异常" % item.get("id"))
        if item.get("kind") in ("entry", "rule") and item.get("path", "").startswith("noahsark/"):
            if "/noahsark/" not in url:
                errors.append("条目 %s 的对外地址未落在 noahsark/ 前缀下" % item.get("id"))


def check_markdown(out_dir, index, errors):
    path = out_dir / "index.md"
    if not path.exists():
        errors.append("缺少 index.md")
        return
    text = path.read_text(encoding="utf-8")
    entry_url = (index.get("entry") or {}).get("markdown")
    if entry_url and entry_url not in text:
        errors.append("index.md 未包含唯一入口地址")
    hits = HEX40.findall(text)
    if hits:
        errors.append("index.md 含裸哈希串 %d 处（应只保留短前缀）" % len(hits))
    if RFC1918.search(text):
        errors.append("index.md 含私网地址字面量")
    if LEGACY_ENTRY.search(text):
        errors.append("index.md 含已失效旧入口（应为固定入口占位文本）")

    headings = re.findall(r"^# (.+)$", text, re.M)
    regions = index.get("navigation") or []
    expected = ["一、%s" % regions[0].get("title"), "二、%s" % regions[1].get("title")] if len(regions) == 2 else []
    if headings != expected:
        errors.append("index.md 一级标题必须且只能是 %s，实际 %s" % (expected, headings))

    first, _, second = text.partition("# 二、")
    groups = index.get("groups") or []
    for group in groups:
        marker = "### %s" % group.get("label")
        if marker not in text:
            errors.append("index.md 缺少分组表: %s" % group.get("label"))
            continue
        in_system = marker in first
        in_onboarding = marker in second
        if group.get("region") == "system" and not (in_system and not in_onboarding):
            errors.append("分组 %s 未落在区域一" % group.get("id"))
        if group.get("region") == "onboarding" and not (in_onboarding and not in_system):
            errors.append("分组 %s 未落在区域二" % group.get("id"))
        if not in_system and not in_onboarding:
            errors.append("分组 %s 的表格未出现在任一区域" % group.get("id"))

    for item in index.get("items") or []:
        iid = item.get("id")
        if iid not in text:
            errors.append("index.md 未收录条目 %s" % iid)
        if item.get("status") == "planned" and item.get("title") not in text:
            errors.append("index.md 未标注规划中条目: %s" % iid)
        if item.get("kind") == SERVICE_KIND:
            if item.get("url") and item["url"] not in text:
                errors.append("index.md 缺少服务地址: %s" % iid)
            if item.get("version") and item["version"] not in text:
                errors.append("index.md 缺少服务版本: %s" % iid)

    onboarding = index.get("onboarding") or {}
    for step in onboarding.get("steps") or []:
        if step.get("id") not in second:
            errors.append("index.md 区域二缺少初始化步骤 %s" % step.get("id"))
        if step.get("title") not in second:
            errors.append("index.md 区域二缺少步骤标题 %s" % step.get("id"))
    for code in onboarding.get("block_codes") or []:
        if code.get("code") not in text:
            errors.append("index.md 缺少阻塞码 %s" % code.get("code"))
    for acc in onboarding.get("acceptance") or []:
        if acc.get("id") not in second:
            errors.append("index.md 区域二缺少验收项 %s" % acc.get("id"))

    system = index.get("system") or {}
    for layer in system.get("layers") or []:
        if layer.get("title") not in first:
            errors.append("index.md 区域一缺少层职责 %s" % layer.get("id"))
    for gate in system.get("gates") or []:
        if gate.get("gate") not in first:
            errors.append("index.md 区域一缺少门禁 %s" % gate.get("gate"))


def run_tools(out_dir, errors):
    scan = REPO_ROOT / "tools" / "secret_scan.py"
    lint = REPO_ROOT / "tools" / "time_lint.py"
    targets = [str(out_dir / name) for name in ("index.md", "index.json", "manifest.json")]
    targets = [t for t in targets if Path(t).exists()]
    for script, label in ((scan, "secret_scan"), (lint, "time_lint")):
        if not script.exists():
            errors.append("缺少校验工具: %s" % script.name)
            continue
        proc = subprocess.run(
            [sys.executable, str(script)] + targets,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if proc.returncode != 0:
            output = proc.stdout.decode("utf-8", "replace").strip()
            errors.append("%s 未通过: %s" % (label, output))


def check_online(index, manifest, out_dir, errors):
    import oss_publish

    status, body = oss_publish.anon_get((index.get("entry") or {}).get("json", ""))
    if status != 200:
        errors.append("index.json 匿名读取失败: HTTP %s" % status)
        return
    try:
        live_index = json.loads(body.decode("utf-8"))
    except ValueError:
        errors.append("线上 index.json 不是合法 JSON")
        return
    if live_index.get("version") != index.get("version"):
        errors.append("线上 index.json 版本与本地产物不一致")
    if live_index.get("title") != index.get("title"):
        errors.append("线上 index.json 标题与本地产物不一致")
    if (live_index.get("git") or {}).get("commit") != (index.get("git") or {}).get("commit"):
        errors.append("线上 index.json 的 Git pin 与本地产物不一致")
    if [n.get("id") for n in live_index.get("navigation") or []] != list(REGION_ORDER):
        errors.append("线上 index.json 缺少两个导航域")

    for item in index.get("items") or []:
        url = item.get("url") or ""
        if not url.startswith("https://listenai-"):
            continue
        status, body = oss_publish.anon_get(url)
        if status != 200:
            errors.append("条目 %s 匿名读取失败: HTTP %s" % (item.get("id"), status))
            continue
        expected = item.get("sha256")
        if expected:
            actual = oss_publish.sha256_bytes(body)
            if actual != expected:
                errors.append("条目 %s 线上内容与记录校验值不一致" % item.get("id"))
        if url.endswith("/manifest.json"):
            try:
                nested = json.loads(body.decode("utf-8"))
            except ValueError:
                errors.append("条目 %s 指向的清单不是合法 JSON" % item.get("id"))
                continue
            if not nested.get("version"):
                errors.append("条目 %s 指向的清单缺少 version" % item.get("id"))

    # latest 指针必须与版本条目逐项一致
    latest_item = None
    for item in index.get("items") or []:
        if item.get("id") == "agent-template.latest":
            latest_item = item
    if latest_item and latest_item.get("url"):
        status, body = oss_publish.anon_get(latest_item["url"])
        if status != 200:
            errors.append("agent-template latest 匿名读取失败: HTTP %s" % status)
        else:
            try:
                latest = json.loads(body.decode("utf-8"))
            except ValueError:
                errors.append("agent-template latest 不是合法 JSON")
                latest = {}
            template_version = None
            for item in index.get("items") or []:
                if item.get("id") == "agent-template.template":
                    template_version = item.get("version")
            if latest.get("version") and template_version and latest["version"] != template_version:
                errors.append("latest 版本 %s 与索引记录 %s 不一致" % (latest["version"], template_version))

    for entry in manifest.get("files") or []:
        local = out_dir / entry["path"]
        if not local.exists():
            continue
        url = (index.get("entry") or {}).get("json", "").replace("/index.json", "/" + entry["path"])
        status, body = oss_publish.anon_get(url)
        if status != 200:
            errors.append("%s 匿名读取失败: HTTP %s" % (entry["object"], status))
            continue
        if oss_publish.sha256_bytes(body) != entry.get("sha256"):
            errors.append("%s 线上内容与 manifest 记录不一致" % entry["object"])
        if len(body) != entry.get("bytes"):
            errors.append("%s 线上字节数与 manifest 记录不一致" % entry["object"])


def check_git(index, errors, skipped=None):
    """用 GitHub API 确认仓库存在。

    * 未认证调用有每小时限额，命中限额时返回 403 —— 这既不是「仓库不存在」，
      也不该判失败，因此记为跳过并说明原因。
    * 环境里有 GITHUB_TOKEN 时自动带上（只读，不打印）。
    """
    import json as _json
    import os
    import urllib.error
    import urllib.request

    skipped = skipped if skipped is not None else []
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    headers = {"User-Agent": "ark-index-verify", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = "Bearer " + token

    repos = sorted({i["path"] for i in index.get("items") or [] if i.get("kind") == "git" and i.get("path")})
    for full in repos:
        url = "https://api.github.com/repos/%s" % full
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=20) as response:
                _json.load(response)
        except urllib.error.HTTPError as exc:
            remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
            if exc.code in (403, 429) and (remaining in (None, "0", 0) or not token):
                skipped.append("%s 未核对（GitHub API 限额或未授权，HTTP %s）" % (full, exc.code))
            else:
                errors.append("仓库不可达 %s: HTTP %s" % (full, exc.code))
        except Exception as exc:  # noqa: BLE001
            errors.append("仓库检查异常 %s: %s" % (full, exc))


def main(argv=None):
    parser = argparse.ArgumentParser(description="校验 NoahsArk 对外总览")
    parser.add_argument("--dir", default=str(REPO_ROOT / "build" / "noahsark-index"))
    parser.add_argument("--online", action="store_true", help="匿名读取线上地址")
    parser.add_argument("--check-git", action="store_true", help="用 GitHub API 确认仓库存在")
    parser.add_argument("--skip-tools", action="store_true", help="跳过 secret_scan 与 time_lint")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    out_dir = Path(args.dir)
    errors = []
    skipped = []
    summary = {"ok": False, "checks": {}, "errors": errors, "skipped": skipped}

    try:
        index = load(out_dir / "index.json")
        manifest = load(out_dir / "manifest.json")
        items = check_index(index, errors) or []
        check_json_literals(index, errors)
        check_regions(index, items, errors)
        check_index_urls(index, errors)
        check_markdown(out_dir, index, errors)
        check_manifest(out_dir, manifest, index, errors)
        if not args.skip_tools:
            run_tools(out_dir, errors)
        summary["checks"]["offline"] = not errors
        summary["counts"] = index.get("counts")

        if args.online:
            before = len(errors)
            try:
                check_online(index, manifest, out_dir, errors)
                summary["checks"]["online"] = len(errors) == before
            except Exception as exc:  # noqa: BLE001
                errors.append("线上检查异常: %s" % exc)
                summary["checks"]["online"] = False

        if args.check_git:
            before = len(errors)
            check_git(index, errors, skipped)
            summary["checks"]["git"] = len(errors) == before
    except Fail as exc:
        errors.append(str(exc))

    summary["ok"] = not errors
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        if errors:
            print("校验未通过，共 %d 项：" % len(errors))
            for err in errors:
                print("  - %s" % err)
            for note in skipped:
                print("  - %s" % note)
        else:
            print("校验通过：%d 个条目（可用 %d），%d 个角色，%d 个服务，%d 个初始化步骤" % (
                (index.get("counts") or {}).get("items", 0),
                (index.get("counts") or {}).get("available", 0),
                len(index.get("roles") or []),
                (index.get("counts") or {}).get("services", 0),
                (index.get("counts") or {}).get("onboarding_steps", 0),
            ))
    if errors:
        return EXIT_NETWORK if any("匿名读取失败" in e or "不可达" in e for e in errors) else EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
