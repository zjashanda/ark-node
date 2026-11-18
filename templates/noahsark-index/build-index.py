#!/usr/bin/env python3
"""从 catalog.json 生成对外总览三件套。

输入：templates/noahsark-index/catalog.json（唯一手工维护源）
输出：build/noahsark-index/index.md、index.json、manifest.json

设计约束：
- 只依赖标准库，Python 3.8 及以上可运行。
- 不访问网络；地址由 catalog 的 oss 与 git 配置拼出，避免多处写死主机名。
- 校验值与提交号必须真实：本地文件当场算，Git 提交号取 HEAD，禁止手填。
- 任何人读入口只允许两个一级区域（体系介绍、体系环境初始化）；
  机器读入口提供 system / onboarding 两个顶层导航域，页面与 JSON 双声明不一致即失败。
- 服务地址只接受可解析节点名，禁止 RFC1918 私网地址字面量。
- 任何结构问题直接非零退出，不生成半成品。
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent

CATALOG_SCHEMA = "ark.noahsark-index.catalog.v2"
INDEX_SCHEMA = "ark.noahsark-index.v2"
MANIFEST_SCHEMA = "ark.noahsark-index.manifest.v1"

FMT_DISPLAY = "%Y-%m-%d %H:%M:%S"
TZ_CN = timezone(timedelta(hours=8))

ALLOWED_VISIBILITY = ("public-read", "private")
ALLOWED_KIND = ("entry", "git", "rule", "dir", "asset", "service")
ALLOWED_STATUS = ("available", "planned")
ALLOWED_REGION = ("system", "onboarding")
ALLOWED_AUTOMATION = ("auto", "confirm", "admin")
REGION_ORDER = ("system", "onboarding")
SERVICE_KIND = "service"

RFC1918 = re.compile(
    r"10\.(?:\d{1,3}\.){2}\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.(?:\d{1,3}\.)\d{1,3}"
    r"|192\.168\.(?:\d{1,3}\.)\d{1,3}"
)

# 已失效的旧节点名入口：只允许出现在迁移记录里，生成物命中即失败。
# 字面量拆分拼接，避免扫描器把检测器自身当成命中。
LEGACY_ENTRY = re.compile(r"http://bszT14:" + "8091")
ENTRY_KINDS = ("node-name", "fixed-ip")

DEFAULT_OUT = REPO_ROOT / "build" / "noahsark-index"
AGENT_MANIFEST = REPO_ROOT / "templates" / "agents" / "manifest.json"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NO_SOURCE = 3

# 已批准的固定入口：只能作为 discovery.canonical.fixed_entry 或节点 profile 的
# verified_endpoint 出现在 descriptor 里，生成物必须剥离。
APPROVED_INTERNAL_ENDPOINTS = (
    "http://10.11.63.24:8090",  # noqa: secret-scan
    "http://10.11.63.42:8091",  # noqa: secret-scan
    "http://10.210.8.205:8090",  # noqa: secret-scan
)
APPROVED_INTERNAL_HOSTS = (
    "10.11.63.24",  # noqa: secret-scan
    "10.11.63.42",  # noqa: secret-scan
    "10.210.8.205",  # noqa: secret-scan
)
DISCOVERY_SCHEMA = "ark.service-discovery.v1"
TOKEN_BOOTSTRAP_SCHEMA = "ark.token-bootstrap.v1"
HEALTH_SCHEMA = "ark.service-health.v1"
ANONYMOUS_READ_SCHEMA = "ark.service-anonymous-read.v1"
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
TOKEN_VERIFICATION_SCHEMA = "ark.token-bootstrap.verification.v1"
TOKEN_MODES = ("one-time-secret", "self-service", "admin-provisioned")
TOKEN_STATUSES = ("operational", "implemented", "partial")
API_SCOPES = ("anonymous", "read", "write")
INTERNAL_PATH_SUFFIXES = (".fixed_entry", ".verified_endpoint")


class BuildError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


def now_display():
    return datetime.now(TZ_CN).strftime(FMT_DISPLAY)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def git(args, cwd=None):
    proc = subprocess.run(
        ["git"] + args,
        cwd=str(cwd or REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace").strip()


def git_head():
    code, out = git(["rev-parse", "HEAD"])
    if code != 0 or not re.match(r"^[0-9a-f]{40}$", out):
        raise BuildError(EXIT_NO_SOURCE, "无法取得 Git 提交号（git rev-parse HEAD 失败）")
    return out


def git_tracked(path):
    code, out = git(["ls-files", "--error-unmatch", path])
    return code == 0


def git_dirty(paths):
    code, out = git(["status", "--porcelain", "--untracked-files=no", "--"] + list(paths))
    if code != 0:
        raise BuildError(EXIT_NO_SOURCE, "无法读取 Git 工作区状态")
    return bool(out.strip())


def load_json(path):
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise BuildError(EXIT_NO_SOURCE, "文件不存在: %s" % path)
    except ValueError as exc:
        raise BuildError(EXIT_NO_SOURCE, "JSON 解析失败 %s: %s" % (path, exc))


def object_url(oss, key):
    host = oss["endpoint"]
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
    host = host.rstrip("/")
    return "https://%s.%s/%s" % (oss["bucket"], host, key)


def git_repo_url(owner, repo):
    return "https://github.com/%s/%s" % (owner, repo)


def git_file_url(owner, repo, ref, path):
    return "https://github.com/%s/%s/blob/%s/%s" % (owner, repo, ref, path)


# ---------------------------------------------------------------- 校验


def iter_internal_literals(node, path=""):
    """递归返回 (json_path, ip)，只收集 RFC1918 命中。"""
    hits = []
    if isinstance(node, dict):
        for key, value in node.items():
            child = "%s.%s" % (path, key) if path else str(key)
            hits.extend(iter_internal_literals(value, child))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            child = "%s[%d]" % (path, index) if path else "[%d]" % index
            hits.extend(iter_internal_literals(value, child))
    elif isinstance(node, str):
        for match in RFC1918.finditer(node):
            hits.append((path, match.group(0)))
    return hits


def validate_discovery(descriptor, eid, need):
    discovery = descriptor.get("discovery") or {}
    host = descriptor.get("host") or {}
    canonical = discovery.get("canonical") or {}
    resolution = discovery.get("resolution") or {}
    profiles = discovery.get("node_profiles")
    vm_boundary = discovery.get("vm_boundary") or {}
    redaction = discovery.get("redaction") or {}

    need(bool(descriptor.get("discovery")), "服务条目 %s 缺少 discovery 契约" % eid)
    need(
        discovery.get("schema") == DISCOVERY_SCHEMA,
        "服务条目 %s 的 discovery.schema 必须是 %s" % (eid, DISCOVERY_SCHEMA),
    )

    need(
        canonical.get("node_name") == host.get("name"),
        "服务条目 %s 的 canonical.node_name 必须等于 host.name" % eid,
    )
    need(
        canonical.get("endpoint") == descriptor.get("endpoint"),
        "服务条目 %s 的 canonical.endpoint 必须等于 endpoint" % eid,
    )
    need(
        canonical.get("port") == host.get("port"),
        "服务条目 %s 的 canonical.port 必须等于 host.port" % eid,
    )
    need(
        canonical.get("scheme") == host.get("scheme"),
        "服务条目 %s 的 canonical.scheme 必须等于 host.scheme" % eid,
    )
    fixed_entry = str(canonical.get("fixed_entry") or "")
    need(
        fixed_entry.startswith("http://") or fixed_entry.startswith("https://"),
        "服务条目 %s 的 canonical.fixed_entry 必须是 http(s) 地址" % eid,
    )
    need(bool(canonical.get("fixed_entry_since")), "服务条目 %s 的 fixed_entry_since 不能为空" % eid)
    need(
        bool(canonical.get("fixed_entry_approval")),
        "服务条目 %s 的 fixed_entry_approval 不能为空" % eid,
    )
    need(
        fixed_entry in APPROVED_INTERNAL_ENDPOINTS,
        "服务条目 %s 的 canonical.fixed_entry 是未批准的固定入口" % eid,
    )

    need(
        resolution.get("order") == ["node_profile", "canonical"],
        "服务条目 %s 的 resolution.order 必须是 node_profile → canonical" % eid,
    )
    need(bool(resolution.get("rule")), "服务条目 %s 的 resolution.rule 不能为空" % eid)
    need(
        bool(resolution.get("fixed_entry_rule")),
        "服务条目 %s 的 resolution.fixed_entry_rule 不能为空" % eid,
    )

    need(isinstance(profiles, list) and bool(profiles), "服务条目 %s 的 node_profiles 必须是非空列表" % eid)
    node_names = []
    for index, profile in enumerate(profiles if isinstance(profiles, list) else []):
        if not isinstance(profile, dict):
            need(False, "服务条目 %s 的 node_profiles[%d] 必须是对象" % (eid, index))
            continue
        for field in ("node", "os", "verified_endpoint", "verified_at", "receipt", "scope", "note"):
            need(
                bool(profile.get(field)),
                "服务条目 %s 的 node_profiles[%d] 缺少 %s" % (eid, index, field),
            )
        node = profile.get("node")
        if node:
            need(node not in node_names, "服务条目 %s 的 node_profiles 节点 %s 重复" % (eid, node))
            node_names.append(node)
        verified = str(profile.get("verified_endpoint") or "")
        need(
            verified.startswith("http://") or verified.startswith("https://"),
            "服务条目 %s 的 node_profiles[%d].verified_endpoint 必须是 http(s) 地址" % (eid, index),
        )
        need(
            profile.get("scope") == "node-local",
            "服务条目 %s 的 node_profiles[%d].scope 必须是 node-local" % (eid, index),
        )

    need(bool(vm_boundary.get("rule")), "服务条目 %s 的 vm_boundary.rule 不能为空" % eid)
    for node in vm_boundary.get("nodes") or []:
        need(
            node in node_names,
            "服务条目 %s 的 vm_boundary.nodes 引用了不存在的节点 %s" % (eid, node),
        )

    need(bool(redaction.get("rule")), "服务条目 %s 的 redaction.rule 不能为空" % eid)
    internal_fields = redaction.get("internal_only_fields") or []
    need(
        "discovery.canonical.fixed_entry" in internal_fields,
        "服务条目 %s 的 redaction.internal_only_fields 必须包含 fixed_entry" % eid,
    )
    need(
        "discovery.node_profiles[].verified_endpoint" in internal_fields,
        "服务条目 %s 的 redaction.internal_only_fields 必须包含 verified_endpoint" % eid,
    )

    entry_kind = canonical.get("entry_kind") or "node-name"
    fixed_ip_paths = ("endpoint", "discovery.canonical.endpoint") if entry_kind == "fixed-ip" else ()
    for json_path, ip in iter_internal_literals(descriptor):
        allowed = ip in APPROVED_INTERNAL_HOSTS and (
            json_path.endswith(INTERNAL_PATH_SUFFIXES) or json_path in fixed_ip_paths
        )
        need(
            allowed,
            "服务条目 %s 的 %s 出现未批准的私网字面量 %s" % (eid, json_path, ip),
        )


def validate_token_bootstrap(descriptor, eid, need):
    token = descriptor.get("token_bootstrap") or {}
    need(bool(descriptor.get("token_bootstrap")), "服务条目 %s 缺少 token_bootstrap 契约" % eid)
    need(
        token.get("schema") == TOKEN_BOOTSTRAP_SCHEMA,
        "服务条目 %s 的 token_bootstrap.schema 必须是 %s" % (eid, TOKEN_BOOTSTRAP_SCHEMA),
    )
    need(
        token.get("mode") in TOKEN_MODES,
        "服务条目 %s 的 token_bootstrap.mode 非法（允许 %s）" % (eid, "/".join(TOKEN_MODES)),
    )
    need(
        token.get("status") in TOKEN_STATUSES,
        "服务条目 %s 的 token_bootstrap.status 非法（允许 %s）" % (eid, "/".join(TOKEN_STATUSES)),
    )
    need(token.get("per_node") is True, "服务条目 %s 的 token_bootstrap.per_node 必须为 true" % eid)

    if token.get("status") == "operational":
        verification = token.get("verification") or {}
        need(
            verification.get("schema") == TOKEN_VERIFICATION_SCHEMA,
            "服务条目 %s 声称 token_bootstrap operational，必须带 %s 证据块"
            % (eid, TOKEN_VERIFICATION_SCHEMA),
        )
        need(
            bool(verification.get("observed")),
            "服务条目 %s 声称 token_bootstrap operational，verification.observed 不能为空" % eid,
        )
        need(
            bool(verification.get("rule")),
            "服务条目 %s 的 token_bootstrap.verification.rule 不能为空" % eid,
        )
    if token.get("status") == "operational":
        need(
            token.get("bootstrap_api") is True,
            "服务条目 %s 的 token_bootstrap.status=operational 时 bootstrap_api 必须为 true" % eid,
        )

    minimum = token.get("minimum_scopes") or []
    allowed = token.get("allowed_scopes") or []
    need(bool(minimum), "服务条目 %s 的 token_bootstrap.minimum_scopes 不能为空" % eid)
    for scope in minimum:
        need(
            scope in allowed,
            "服务条目 %s 的 token_bootstrap.minimum_scopes 含未授权 scope %s" % (eid, scope),
        )

    storage = token.get("storage") or {}
    for field in ("linux", "windows", "env", "rule"):
        need(bool(storage.get(field)), "服务条目 %s 的 token_bootstrap.storage 缺少 %s" % (eid, field))

    lifecycle = token.get("lifecycle") or {}
    for field in ("bootstrap", "status", "rotate", "revoke"):
        need(
            bool(lifecycle.get(field)),
            "服务条目 %s 的 token_bootstrap.lifecycle 缺少 %s" % (eid, field),
        )

    need(bool(token.get("rules")), "服务条目 %s 的 token_bootstrap.rules 不能为空" % eid)

    parity = token.get("parity") or {}
    need(
        isinstance(parity.get("bootstrap_api"), bool),
        "服务条目 %s 的 token_bootstrap.parity.bootstrap_api 必须是布尔值" % eid,
    )
    need(bool(parity.get("note")), "服务条目 %s 的 token_bootstrap.parity.note 不能为空" % eid)
    if parity.get("bootstrap_api") is False:
        need(
            bool(parity.get("gap")),
            "服务条目 %s 的 token_bootstrap.parity.bootstrap_api 为 false 时 gap 不能为空" % eid,
        )


def validate_anonymous_read(descriptor, eid, need):
    auth = descriptor.get("auth") or {}
    anon = auth.get("anonymous_read")
    if not anon:
        return
    need(
        anon.get("schema") == ANONYMOUS_READ_SCHEMA,
        "服务条目 %s 的 auth.anonymous_read.schema 必须是 %s" % (eid, ANONYMOUS_READ_SCHEMA),
    )
    need(
        anon.get("status") in ("pending_service_change", "verified"),
        "服务条目 %s 的 auth.anonymous_read.status 非法" % eid,
    )
    public = anon.get("public_api") or []
    protected = anon.get("protected_api") or []
    need(bool(public), "服务条目 %s 的 auth.anonymous_read.public_api 不能为空" % eid)
    need(bool(protected), "服务条目 %s 的 auth.anonymous_read.protected_api 不能为空" % eid)
    need(bool(anon.get("redaction")), "服务条目 %s 的 auth.anonymous_read.redaction 不能为空" % eid)
    limits = anon.get("limits") or {}
    for field in ("rate_limit", "body_max_bytes", "page_max", "audit"):
        need(bool(limits.get(field)), "服务条目 %s 的 auth.anonymous_read.limits 缺少 %s" % (eid, field))
    need(
        not (set(public) & set(protected)),
        "服务条目 %s 的 anonymous_read 公开与受保护接口重叠" % eid,
    )
    change = anon.get("service_change") or {}
    if anon.get("status") == "pending_service_change":
        need(
            change.get("required") is True,
            "服务条目 %s 的 anonymous_read 仍是 pending_service_change，必须声明 service_change.required=true" % eid,
        )
        need(
            bool(change.get("doc")),
            "服务条目 %s 的 anonymous_read.service_change 缺少 doc" % eid,
        )

    declared = set()
    for api in descriptor.get("api") or []:
        if (api.get("scope") or "") == "anonymous" and (api.get("method") or "").upper() == "GET":
            declared.add("%s %s" % (api.get("method"), api.get("path")))
    need(
        declared == set(public),
        "服务条目 %s 的 anonymous_read.public_api 与 api[].scope=anonymous 不一致（声明 %s / 实际 %s）"
        % (eid, sorted(set(public)), sorted(declared)),
    )



def validate_anonymous_proposal(descriptor, eid, need):
    proposal = (descriptor.get("auth") or {}).get("anonymous_proposal")
    apis = descriptor.get("api") or []
    anonymous_posts = {
        "%s %s" % (api.get("method"), api.get("path"))
        for api in apis
        if (api.get("scope") or "") == "anonymous" and (api.get("method") or "").upper() == "POST"
    }
    if not proposal:
        need(
            not anonymous_posts,
            "服务条目 %s 有匿名 POST 接口 %s，必须声明 auth.anonymous_proposal"
            % (eid, sorted(anonymous_posts)),
        )
        return
    need(
        proposal.get("schema") == ANON_PROPOSAL_SCHEMA,
        "服务条目 %s 的 auth.anonymous_proposal.schema 必须是 %s" % (eid, ANON_PROPOSAL_SCHEMA),
    )
    need(
        proposal.get("status") in ("pending_service_change", "verified"),
        "服务条目 %s 的 auth.anonymous_proposal.status 非法" % eid,
    )
    endpoint = str(proposal.get("endpoint") or "")
    need(
        endpoint == "POST /v1/proposals",
        "服务条目 %s 的 auth.anonymous_proposal.endpoint 必须是 POST /v1/proposals" % eid,
    )
    need(
        list(proposal.get("allowed_status") or []) == ["pending"],
        "服务条目 %s 的 auth.anonymous_proposal.allowed_status 必须是 ['pending']" % eid,
    )
    fields = set(proposal.get("required_fields") or [])
    for field in ANON_PROPOSAL_REQUIRED_FIELDS:
        need(
            field in fields,
            "服务条目 %s 的 anonymous_proposal.required_fields 缺少 %s" % (eid, field),
        )
    gates = proposal.get("gates") or {}
    for gate in ANON_PROPOSAL_GATES:
        need(bool(gates.get(gate)), "服务条目 %s 的 anonymous_proposal.gates 缺少 %s" % (eid, gate))
    forbidden = set(proposal.get("forbidden") or [])
    for action in ANON_PROPOSAL_FORBIDDEN:
        need(
            action in forbidden,
            "服务条目 %s 的 anonymous_proposal.forbidden 缺少 %s" % (eid, action),
        )
    change = proposal.get("service_change") or {}
    need(
        change.get("required") is True,
        "服务条目 %s 的 anonymous_proposal 必须声明 service_change.required=true（含 verified）" % eid,
    )
    doc = change.get("doc")
    need(
        bool(doc),
        "服务条目 %s 的 anonymous_proposal.service_change 缺少 doc" % eid,
    )
    if doc:
        need(
            (REPO_ROOT / doc).exists(),
            "服务条目 %s 的 anonymous_proposal.service_change.doc 不存在: %s" % (eid, doc),
        )
    declared = {
        "%s %s" % (api.get("method"), api.get("path"))
        for api in apis
        if (api.get("scope") or "") == "anonymous"
    }
    need(
        endpoint in declared,
        "服务条目 %s 的 %s 必须在 api[] 中标记 scope=anonymous（实际匿名接口 %s）"
        % (eid, endpoint, sorted(declared)),
    )

def validate_health_contract(descriptor, eid, need):
    health = descriptor.get("health_contract") or {}
    need(bool(descriptor.get("health_contract")), "服务条目 %s 缺少 health_contract 契约" % eid)
    need(
        health.get("schema") == HEALTH_SCHEMA,
        "服务条目 %s 的 health_contract.schema 必须是 %s" % (eid, HEALTH_SCHEMA),
    )
    probe = health.get("probe") or {}
    path = str(probe.get("path") or "")
    need(path.startswith("/"), "服务条目 %s 的 health_contract.probe.path 必须以 / 开头" % eid)
    need(
        probe.get("method") == "GET",
        "服务条目 %s 的 health_contract.probe.method 必须是 GET" % eid,
    )
    need(
        bool(health.get("required_fields")),
        "服务条目 %s 的 health_contract.required_fields 不能为空" % eid,
    )
    need(bool(health.get("status_rule")), "服务条目 %s 的 health_contract.status_rule 不能为空" % eid)
    need(
        bool(health.get("write_path_rule")),
        "服务条目 %s 的 health_contract.write_path_rule 不能为空" % eid,
    )
    if descriptor.get("freshness"):
        commits = health.get("commits") or {}
        for field in ("source_field", "indexed_field", "consistency"):
            need(
                bool(commits.get(field)),
                "服务条目 %s 的 health_contract.commits 缺少 %s" % (eid, field),
            )
        need(
            bool((health.get("degraded") or {}).get("field")),
            "服务条目 %s 的 health_contract.degraded.field 不能为空" % eid,
        )


def validate_catalog(cat):
    errors = []

    def need(cond, message):
        if not cond:
            errors.append(message)

    need(cat.get("schema") == CATALOG_SCHEMA, "catalog.schema 必须是 %s" % CATALOG_SCHEMA)
    need(bool(cat.get("version")), "catalog.version 不能为空")
    need(bool(cat.get("title")), "catalog.title 不能为空")

    for json_path, ip in iter_internal_literals(cat):
        need(False, "catalog 的 %s 出现私网地址字面量 %s（catalog 不允许任何私网地址）" % (json_path, ip))

    oss = cat.get("oss") or {}
    for field in ("bucket", "endpoint", "prefix", "visibility"):
        need(bool(oss.get(field)), "catalog.oss.%s 不能为空" % field)
    need(
        oss.get("visibility") in ALLOWED_VISIBILITY,
        "catalog.oss.visibility 只能是 %s" % "/".join(ALLOWED_VISIBILITY),
    )
    prefix = (oss.get("prefix") or "").strip("/")

    git_conf = cat.get("git") or {}
    for field in ("owner", "repo", "source"):
        need(bool(git_conf.get(field)), "catalog.git.%s 不能为空" % field)

    workdir = cat.get("workdir") or {}
    for field in ("windows", "linux", "macos"):
        need(bool(workdir.get(field)), "catalog.workdir.%s 不能为空" % field)

    # 两个一级区域，顺序固定
    regions = cat.get("regions") or []
    region_ids = [r.get("id") for r in regions]
    need(region_ids == list(REGION_ORDER), "catalog.regions 必须是且只能是 %s" % "/".join(REGION_ORDER))
    for region in regions:
        need(bool(region.get("title")), "区域 %s 缺少 title" % region.get("id"))
        need(bool(region.get("summary")), "区域 %s 缺少 summary" % region.get("id"))

    # 体系介绍的结构化事实
    for field in ("positioning", "layers", "lifecycle", "knowledge_loop", "gates", "security"):
        need(bool(cat.get(field)), "catalog.%s 不能为空" % field)
    for layer in cat.get("layers") or []:
        for field in ("id", "title", "responsibility", "not_responsible"):
            need(bool(layer.get(field)), "layers 条目缺少 %s" % field)
    for step in cat.get("lifecycle") or []:
        for field in ("step", "actor", "artifact", "gate"):
            need(bool(step.get(field)), "lifecycle 条目缺少 %s" % field)
    for step in cat.get("knowledge_loop") or []:
        for field in ("step", "actor", "detail", "artifact"):
            need(bool(step.get(field)), "knowledge_loop 条目缺少 %s" % field)
    for gate in cat.get("gates") or []:
        for field in ("gate", "rule", "failure"):
            need(bool(gate.get(field)), "gates 条目缺少 %s" % field)

    # 角色
    roles = cat.get("roles") or []
    need(len(roles) >= 1, "至少需要一个角色")
    role_ids = [r.get("id") for r in roles]
    need(len(role_ids) == len(set(role_ids)), "角色 id 重复")
    for role in roles:
        need(bool(role.get("label")), "角色 %s 缺少 label" % role.get("id"))
        need(bool(role.get("summary")), "角色 %s 缺少 summary" % role.get("id"))
        need(bool(role.get("must_read")), "角色 %s 的必读清单不能为空" % role.get("id"))

    # 分组与条目
    groups = cat.get("groups") or []
    need(len(groups) >= 1, "至少需要一个分组")
    group_ids = [g.get("id") for g in groups]
    need(len(group_ids) == len(set(group_ids)), "分组 id 重复")

    entries = []
    for group in groups:
        need(bool(group.get("label")), "分组 %s 缺少 label" % group.get("id"))
        need(
            group.get("region") in ALLOWED_REGION,
            "分组 %s 的 region 必须是 %s" % (group.get("id"), "/".join(ALLOWED_REGION)),
        )
        for entry in group.get("entries") or []:
            entries.append((group, entry))

    entry_ids = [e.get("id") for _, e in entries]
    need(len(entry_ids) == len(set(entry_ids)), "条目 id 重复")
    known = set(entry_ids)

    descriptors = {}
    for group, entry in entries:
        eid = entry.get("id")
        need(bool(eid), "存在缺少 id 的条目（分组 %s）" % group.get("id"))
        need(bool(entry.get("title")), "条目 %s 缺少 title" % eid)
        need(bool(entry.get("summary")), "条目 %s 缺少 summary" % eid)
        need(entry.get("kind") in ALLOWED_KIND, "条目 %s 的 kind 非法" % eid)
        need(entry.get("status") in ALLOWED_STATUS, "条目 %s 的 status 非法" % eid)
        if entry.get("status") == "available":
            need(bool(entry.get("version")), "可用条目 %s 必须有 version" % eid)
            need(
                bool(entry.get("oss")) or bool(entry.get("git")) or bool(entry.get("descriptor")),
                "可用条目 %s 必须有 oss、git 或服务契约" % eid,
            )
        if entry.get("status") == "planned":
            need(
                not entry.get("oss") and not entry.get("git") and not entry.get("descriptor"),
                "规划中条目 %s 不能带地址（避免死链）" % eid,
            )
            need(entry.get("version") is None, "规划中条目 %s 的 version 必须为空" % eid)
        if entry.get("oss"):
            need(
                entry["oss"].startswith(prefix + "/"),
                "条目 %s 的 oss 路径必须以 %s/ 开头" % (eid, prefix),
            )
        if entry.get("kind") == "git":
            need(bool(entry.get("git")), "git 类条目 %s 缺少 git 仓库" % eid)
        if entry.get("kind") == "dir":
            need(entry.get("status") == "planned", "目录条目 %s 只能是规划中" % eid)
        if entry.get("kind") == SERVICE_KIND:
            need(entry.get("status") == "available", "服务条目 %s 必须可用" % eid)
            rel = entry.get("descriptor") or ""
            need(bool(rel), "服务条目 %s 缺少 descriptor" % eid)
            need(rel.startswith("templates/"), "服务条目 %s 的 descriptor 必须在仓库内" % eid)
            if rel:
                path = REPO_ROOT / rel
                need(path.exists(), "服务条目 %s 的 descriptor 不存在: %s" % (eid, rel))
                if path.exists():
                    try:
                        descriptor = load_json(path)
                    except BuildError as exc:
                        errors.append("服务条目 %s 的 descriptor 无法解析: %s" % (eid, exc.message))
                        descriptor = None
                    if descriptor is not None:
                        descriptors[eid] = descriptor
                        need(
                            descriptor.get("id") == eid,
                            "服务条目 %s 与 descriptor.id=%s 不一致" % (eid, descriptor.get("id")),
                        )
                        need(
                            descriptor.get("version") == entry.get("version"),
                            "服务条目 %s 的版本与 descriptor 不一致" % eid,
                        )
                        endpoint = str(descriptor.get("endpoint") or "")
                        host_conf = descriptor.get("host") or {}
                        host = host_conf.get("name") or ""
                        canonical = (descriptor.get("discovery") or {}).get("canonical") or {}
                        entry_kind = canonical.get("entry_kind") or "node-name"
                        need(
                            entry_kind in ENTRY_KINDS,
                            "服务条目 %s 的 entry_kind 非法：%s" % (eid, entry_kind),
                        )
                        need(
                            host_conf.get("entry_kind", entry_kind) == entry_kind,
                            "服务条目 %s 的 host.entry_kind 与 canonical.entry_kind 不一致" % eid,
                        )
                        need(
                            endpoint.startswith("http://") or endpoint.startswith("https://"),
                            "服务条目 %s 的 endpoint 必须是 http(s) 地址" % eid,
                        )
                        need(bool(host), "服务条目 %s 的 host.name 不能为空" % eid)
                        if entry_kind == "fixed-ip":
                            need(
                                endpoint in APPROVED_INTERNAL_ENDPOINTS,
                                "服务条目 %s 的 fixed-ip endpoint 不在批准清单内" % eid,
                            )
                            need(
                                host == canonical.get("node_name"),
                                "服务条目 %s 的 fixed-ip host.name 必须等于 canonical.node_name" % eid,
                            )
                        else:
                            need(
                                not RFC1918.search(endpoint),
                                "服务条目 %s 的 endpoint 含私网地址字面量" % eid,
                            )
                            need(
                                bool(host) and host in endpoint,
                                "服务条目 %s 的 endpoint 必须使用可解析节点名 %s" % (eid, host),
                            )
                        need(
                            host_conf.get("port") == extract_port(endpoint),
                            "服务条目 %s 的 endpoint 端口与 host.port 不一致" % eid,
                        )
                        need(bool(descriptor.get("auth")), "服务条目 %s 缺少 auth 契约" % eid)
                        need(bool(descriptor.get("api")), "服务条目 %s 缺少 api 列表" % eid)
                        need(bool(descriptor.get("verification")), "服务条目 %s 缺少 verification" % eid)
                        for api in descriptor.get("api") or []:
                            need(
                                api.get("scope") in API_SCOPES
                                or api.get("scope") in (None, "legacy-basic"),
                                "服务条目 %s 的 api %s 的 scope 非法"
                                % (eid, api.get("name") or api.get("path")),
                            )
                        validate_discovery(descriptor, eid, need)
                        validate_token_bootstrap(descriptor, eid, need)
                        validate_anonymous_read(descriptor, eid, need)
                        validate_anonymous_proposal(descriptor, eid, need)
                        validate_health_contract(descriptor, eid, need)
        for role in entry.get("roles") or []:
            need(role in role_ids, "条目 %s 引用了不存在的角色 %s" % (eid, role))
        for dep in entry.get("requires") or []:
            need(dep in known, "条目 %s 依赖了不存在的条目 %s" % (eid, dep))
            need(dep != eid, "条目 %s 不能依赖自身" % eid)

    # services 声明与分组一致
    services = cat.get("services") or []
    need(bool(services), "catalog.services 不能为空")
    svc_ids = [s.get("id") for s in services]
    need(len(svc_ids) == len(set(svc_ids)), "services id 重复")
    for svc in services:
        sid = svc.get("id")
        need(sid in descriptors, "services 里的 %s 没有对应的服务条目" % sid)
        need(bool(svc.get("summary")), "服务 %s 缺少 summary" % sid)
        need(svc.get("descriptor") == (descriptors.get(sid) and _entry_descriptor(entries, sid)),
             "服务 %s 的 services.descriptor 与分组条目不一致" % sid)

    # onboarding
    onboarding = cat.get("onboarding") or {}
    levels = onboarding.get("automation_levels") or {}
    need(set(levels) == set(ALLOWED_AUTOMATION),
         "onboarding.automation_levels 必须覆盖 %s" % "/".join(ALLOWED_AUTOMATION))
    steps = onboarding.get("steps") or []
    need(bool(steps), "onboarding.steps 不能为空")
    step_ids = [s.get("id") for s in steps]
    need(len(step_ids) == len(set(step_ids)), "onboarding step id 重复")
    accepted = [a.get("id") for a in onboarding.get("acceptance") or []]
    need(bool(accepted), "onboarding.acceptance 不能为空")
    need(len(accepted) == len(set(accepted)), "onboarding acceptance id 重复")
    codes = [c.get("code") for c in onboarding.get("block_codes") or []]
    need(bool(codes), "onboarding.block_codes 不能为空")
    need(len(codes) == len(set(codes)), "onboarding block code 重复")
    for step in steps:
        sid = step.get("id")
        for field in ("id", "scope", "title", "automation", "detail"):
            need(bool(step.get(field)), "onboarding step %s 缺少 %s" % (sid, field))
        need(
            step.get("automation") in ALLOWED_AUTOMATION,
            "onboarding step %s 的 automation 非法" % sid,
        )
        need(bool(step.get("acceptance")), "onboarding step %s 缺少 acceptance" % sid)
        need(bool(step.get("block_codes")), "onboarding step %s 缺少 block_codes" % sid)
        for dep in step.get("requires") or []:
            need(dep in step_ids, "onboarding step %s 依赖不存在的步骤 %s" % (sid, dep))
        for item in step.get("acceptance") or []:
            need(item in accepted, "onboarding step %s 引用了未知验收项 %s" % (sid, item))
        for code in step.get("block_codes") or []:
            need(code in codes, "onboarding step %s 引用了未登记阻塞码 %s" % (sid, code))
    drift = onboarding.get("drift") or {}
    need(bool(drift.get("states")), "onboarding.drift.states 不能为空")
    need(bool(drift.get("rule")), "onboarding.drift.rule 不能为空")

    # 角色必读清单
    for role in roles:
        for ref in list(role.get("must_read") or []) + list(role.get("optional") or []):
            need(ref in known, "角色 %s 引用了不存在的条目 %s" % (role.get("id"), ref))

    errors.extend(find_cycles(entries))
    return errors


def _entry_descriptor(entries, entry_id):
    for _group, entry in entries:
        if entry.get("id") == entry_id:
            return entry.get("descriptor")
    return None


def extract_port(endpoint):
    match = re.search(r":(\d{1,5})$", endpoint)
    if not match:
        return None
    return int(match.group(1))


def find_cycles(entries):
    graph = {}
    for _, entry in entries:
        graph[entry.get("id")] = list(entry.get("requires") or [])
    state = {}
    errors = []
    stack = []

    def visit(node):
        if state.get(node) == "done":
            return
        if state.get(node) == "visiting":
            cycle = " -> ".join(stack[stack.index(node):] + [node])
            errors.append("依赖成环: %s" % cycle)
            return
        state[node] = "visiting"
        stack.append(node)
        for dep in graph.get(node) or []:
            if dep in graph:
                visit(dep)
        stack.pop()
        state[node] = "done"

    for node in graph:
        visit(node)
    return errors


# ---------------------------------------------------------------- 解析


def cross_check_agent_manifest(local_path, digest):
    """本地文件若同时被 Agent 模板清单收录，两份校验值必须一致。"""
    if not AGENT_MANIFEST.exists():
        return None
    manifest = load_json(AGENT_MANIFEST)
    for item in manifest.get("files") or []:
        recorded = item.get("path") or ""
        if Path(recorded).name == Path(local_path).name:
            if item.get("sha256") != digest:
                raise BuildError(
                    EXIT_NO_SOURCE,
                    "校验值冲突：%s 与 templates/agents/manifest.json 记录不一致" % local_path,
                )
            return recorded
    return None


def resolve_entries(cat):
    oss = cat["oss"]
    git_conf = cat["git"]
    resolved = []
    for group in cat["groups"]:
        for entry in group["entries"]:
            item = dict(entry)
            item["_group"] = group["id"]
            item["_group_label"] = group["label"]
            item["_region"] = group["region"]
            item["_url"] = None
            item["_url_file"] = None
            item["_path"] = None
            item["_sha"] = entry.get("sha256")
            item["_descriptor"] = None
            item["_source_repo"] = entry.get("git") or git_conf["repo"]
            item["_source_commit"] = entry.get("commit")

            if entry.get("kind") == SERVICE_KIND:
                descriptor_rel = entry["descriptor"]
                descriptor = load_json(REPO_ROOT / descriptor_rel)
                canonical = (descriptor.get("discovery") or {}).get("canonical") or {}
                entry_kind = canonical.get("entry_kind") or "node-name"
                item["_descriptor"] = descriptor
                item["_entry_kind"] = entry_kind
                item["_url"] = None if entry_kind == "fixed-ip" else descriptor.get("endpoint")
                item["_path"] = descriptor_rel
                runtime = descriptor.get("runtime") or {}
                item["_sha"] = runtime.get("artifact_sha256")
                item["_source_repo"] = runtime.get("source_repo")
                item["_source_commit"] = runtime.get("source_commit")
            elif entry.get("oss"):
                item["_path"] = entry["oss"]
                item["_url"] = object_url(oss, entry["oss"])
            elif entry.get("git"):
                repo = entry["git"].split("/")[-1]
                item["_path"] = entry["git"]
                item["_url"] = git_repo_url(git_conf["owner"], repo)

            if entry.get("kind") != SERVICE_KIND and entry.get("local"):
                local = REPO_ROOT / entry["local"]
                if not local.exists():
                    raise BuildError(EXIT_NO_SOURCE, "本地文件不存在: %s" % local)
                digest = sha256_file(local)
                if entry.get("sha256") and entry["sha256"] != digest:
                    raise BuildError(
                        EXIT_NO_SOURCE,
                        "条目 %s 的固定校验值与本地文件不一致" % entry["id"],
                    )
                item["_sha"] = digest
                cross_check_agent_manifest(entry["local"], digest)
            elif entry.get("sha256") and entry.get("kind") != SERVICE_KIND:
                if not re.match(r"^[0-9a-f]{64}$", entry["sha256"]):
                    raise BuildError(EXIT_NO_SOURCE, "条目 %s 的 sha256 格式非法" % entry["id"])

            if entry.get("file"):
                item["_url_file"] = git_file_url(
                    git_conf["owner"],
                    entry["git"].split("/")[-1],
                    "main",
                    entry["file"],
                )

            resolved.append(item)
    return resolved


# ---------------------------------------------------------------- 生成


def count_items(items):
    available = sum(1 for i in items if i["status"] == "available")
    return {
        "items": len(items),
        "available": available,
        "planned": len(items) - available,
    }


def status_label(status):
    return "可用" if status == "available" else "规划中"


def block_code_meaning(code):
    return code.get("meaning") or code.get("title") or code.get("when") or "-"


def block_code_action(code):
    return code.get("action") or code.get("rule") or "-"


def clause(text):
    value = (text or "-").strip()
    return value[:-1] if value.endswith("。") else value


def md_link(text, url, extra=None):
    if not url:
        return text
    return "[%s](%s)" % (text, url)


def short_sha(value):
    if not value:
        return ""
    return value[:12]


def item_payload(item, content_commit, cat):
    source_commit = item.get("_source_commit") or content_commit
    payload = {
        "id": item["id"],
        "region": item["_region"],
        "group": item["_group"],
        "group_label": item["_group_label"],
        "kind": item["kind"],
        "title": item["title"],
        "status": item["status"],
        "version": item.get("version"),
        "path": item["_path"],
        "url": item["_url"],
        "file": item.get("file"),
        "file_url": item["_url_file"],
        "section": item.get("section"),
        "sha256": item["_sha"],
        "size": item.get("size"),
        "source_repo": item["_source_repo"] or cat["git"]["repo"],
        "source_commit": source_commit,
        "roles": list(item.get("roles") or []),
        "requires": list(item.get("requires") or []),
        "summary": item["summary"],
    }
    if item["kind"] == SERVICE_KIND:
        payload["descriptor"] = item["_path"]
        payload["entry_kind"] = item.get("_entry_kind") or "node-name"
        if payload["entry_kind"] == "fixed-ip":
            payload["endpoint_ref"] = "descriptor:%s#discovery.canonical.fixed_entry" % item["_path"]
        else:
            payload["endpoint"] = item["_url"]
    return payload


def compact_item(item, content_commit, cat):
    """区域视图只保留导航必需字段；完整字段在顶层 items。"""
    return {
        "id": item["id"],
        "region": item["_region"],
        "group": item["_group"],
        "kind": item["kind"],
        "title": item["title"],
        "status": item["status"],
        "version": item.get("version"),
        "url": item["_url"],
        "sha256": item["_sha"],
        "source_repo": item["_source_repo"] or cat["git"]["repo"],
        "source_commit": item.get("_source_commit") or content_commit,
        "summary": item["summary"],
    }


def service_block(item):
    descriptor = item["_descriptor"]
    host = descriptor.get("host") or {}
    auth = descriptor.get("auth") or {}
    verification = descriptor.get("verification") or {}
    fallback = descriptor.get("fallback") or {}
    discovery = descriptor.get("discovery") or {}
    canonical = discovery.get("canonical") or {}
    resolution = discovery.get("resolution") or {}
    vm_boundary = discovery.get("vm_boundary") or {}
    redaction = discovery.get("redaction") or {}
    token = descriptor.get("token_bootstrap") or {}
    token_storage = token.get("storage") or {}
    token_parity = token.get("parity") or {}
    health = descriptor.get("health_contract") or {}
    probe = health.get("probe") or {}
    degraded = health.get("degraded") or {}

    node_profiles = []
    for profile in discovery.get("node_profiles") or []:
        node_profiles.append(
            {
                "node": profile.get("node"),
                "os": profile.get("os"),
                "verified": True,
                "verified_at": profile.get("verified_at"),
                "receipt": profile.get("receipt"),
                "scope": profile.get("scope"),
                "note": profile.get("note"),
            }
        )

    entry_kind = canonical.get("entry_kind") or "node-name"
    endpoint_ref = "descriptor:%s#discovery.canonical.fixed_entry" % item["_path"]

    block = {
        "id": descriptor.get("id"),
        "title": descriptor.get("title"),
        "version": descriptor.get("version"),
        "kind": SERVICE_KIND,
        "status": "available",
        "entry_kind": entry_kind,
        "descriptor": item["_path"],
        "descriptor_sha256": sha256_file(REPO_ROOT / item["_path"]),
        "discovery": {
            "host": host.get("name"),
            "port": host.get("port"),
            "scheme": host.get("scheme"),
            "canonical": {
                "node_name": canonical.get("node_name"),
                "entry_kind": entry_kind,
                "port": canonical.get("port"),
                "scheme": canonical.get("scheme"),
                "public_display": canonical.get("public_display"),
                "fixed_entry_present": bool(canonical.get("fixed_entry")),
                "fixed_entry_ref": "descriptor:discovery.canonical.fixed_entry",
                "fixed_entry_since": canonical.get("fixed_entry_since"),
                "fixed_entry_approval": canonical.get("fixed_entry_approval"),
                "fixed_entry_usage": canonical.get("fixed_entry_usage"),
            },
            "resolution": {
                "schema": resolution.get("schema"),
                "order": list(resolution.get("order") or []),
                "rule": resolution.get("rule"),
                "fixed_entry_rule": resolution.get("fixed_entry_rule"),
            },
            "node_profiles": node_profiles,
            "vm_boundary": {
                "rule": vm_boundary.get("rule"),
                "nodes": list(vm_boundary.get("nodes") or []),
                "note": vm_boundary.get("note"),
            },
            "redaction": {
                "rule": redaction.get("rule"),
                "internal_only_fields": list(redaction.get("internal_only_fields") or []),
                "public_fields": list(redaction.get("public_fields") or []),
            },
            "rule": "先节点 profile 后 canonical；profile 是节点局部事实，不构成全局替代。",
        },
        "token_bootstrap": {
            "schema": token.get("schema"),
            "mode": token.get("mode"),
            "status": token.get("status"),
            "per_node": token.get("per_node"),
            "bootstrap_api": token.get("bootstrap_api"),
            "minimum_scopes": list(token.get("minimum_scopes") or []),
            "allowed_scopes": list(token.get("allowed_scopes") or []),
            "storage": {
                "linux": token_storage.get("linux"),
                "windows": token_storage.get("windows"),
                "env": list(token_storage.get("env") or []),
                "rule": token_storage.get("rule"),
            },
            "lifecycle": dict(token.get("lifecycle") or {}),
            "server_store": token.get("server_store"),
            "rules": list(token.get("rules") or []),
            "parity": {
                "bootstrap_api": token_parity.get("bootstrap_api"),
                "gap": token_parity.get("gap"),
                "note": token_parity.get("note"),
            },
            "fallback": dict(token.get("fallback") or {}),
            "verification": {
                "state": (token.get("verification") or {}).get("state"),
                "verified_at": (token.get("verification") or {}).get("verified_at"),
                "observed_count": len((token.get("verification") or {}).get("observed") or []),
            },
        },
        "auth": {
            "mode": auth.get("mode"),
            "scopes": list(auth.get("scopes") or []),
            "token_rule": auth.get("token_store") or auth.get("token_source"),
        },
        "anonymous_read": (
            {
                "schema": (auth.get("anonymous_read") or {}).get("schema"),
                "status": (auth.get("anonymous_read") or {}).get("status"),
                "public_api": list((auth.get("anonymous_read") or {}).get("public_api") or []),
                "protected_api": list((auth.get("anonymous_read") or {}).get("protected_api") or []),
                "rule": (auth.get("anonymous_read") or {}).get("rule"),
                "limits": dict((auth.get("anonymous_read") or {}).get("limits") or {}),
                "service_change": dict((auth.get("anonymous_read") or {}).get("service_change") or {}),
            }
            if auth.get("anonymous_read")
            else None
        ),
        "anonymous_proposal": (
            {
                "schema": (auth.get("anonymous_proposal") or {}).get("schema"),
                "status": (auth.get("anonymous_proposal") or {}).get("status"),
                "endpoint": (auth.get("anonymous_proposal") or {}).get("endpoint"),
                "allowed_status": list(
                    (auth.get("anonymous_proposal") or {}).get("allowed_status") or []
                ),
                "required_fields": list(
                    (auth.get("anonymous_proposal") or {}).get("required_fields") or []
                ),
                "gates": dict((auth.get("anonymous_proposal") or {}).get("gates") or {}),
                "forbidden": list((auth.get("anonymous_proposal") or {}).get("forbidden") or []),
                "service_change": dict(
                    (auth.get("anonymous_proposal") or {}).get("service_change") or {}
                ),
            }
            if auth.get("anonymous_proposal")
            else None
        ),
        "health": {
            "path": probe.get("path"),
            "public": probe.get("public"),
            "schema": health.get("schema"),
            "method": probe.get("method"),
            "strict_query": probe.get("strict_query"),
            "strict_rule": probe.get("strict_rule"),
            "required_fields": list(health.get("required_fields") or []),
            "status_rule": health.get("status_rule"),
            "version_field": health.get("version_field"),
            "degraded": {
                "field": degraded.get("field"),
                "values": list(degraded.get("values") or []),
                "pass_rule": degraded.get("pass_rule"),
            },
            "commits": health.get("commits"),
            "write_path_rule": health.get("write_path_rule"),
            "stop_conditions": list(health.get("stop_conditions") or []),
        },
        "verification": {
            "state": verification.get("state"),
            "confirmed": list(verification.get("confirmed") or []),
            "pending": list(verification.get("pending") or []),
            "receipt": verification.get("receipt"),
        },
        "fallback": fallback,
        "source_repo": item["_source_repo"],
        "source_commit": item["_source_commit"],
    }
    if entry_kind == "fixed-ip":
        block["endpoint_ref"] = endpoint_ref
        block["host"] = {
            "name": host.get("name"),
            "port": host.get("port"),
            "scheme": host.get("scheme"),
        }
        block["discovery"]["canonical"]["endpoint_ref"] = endpoint_ref
    else:
        block["endpoint"] = descriptor.get("endpoint")
        block["discovery"]["canonical"]["endpoint"] = canonical.get("endpoint")
    return block


FAILURE_CODE = re.compile(
    r"\b(?:BLOCKED_[A-Z0-9_]+|STALE_INDEX|EVIDENCE_GAP|SOURCE_MISSING|SERVICE_DISCOVERY_DRIFT|PUBLISH_METADATA_DRIFT)\b"
)


def service_failure_codes(descriptor):
    codes = []

    def absorb(value):
        if isinstance(value, str):
            for code in FAILURE_CODE.findall(value):
                if code not in codes:
                    codes.append(code)
        elif isinstance(value, dict):
            for sub in value.values():
                absorb(sub)
        elif isinstance(value, list):
            for sub in value:
                absorb(sub)

    absorb(descriptor.get("fallback") or {})
    absorb((descriptor.get("health_contract") or {}).get("stop_conditions") or [])
    return codes


def group_payload(group, items_by_group):
    return {
        "id": group["id"],
        "label": group["label"],
        "summary": group.get("summary") or "",
        "region": group["region"],
        "items": [i["id"] for i in items_by_group.get(group["id"], [])],
    }


def build_index_json(cat, items, generated_at, content_commit):
    role_index = {r["id"]: r for r in cat["roles"]}
    entry_by_id = {i["id"]: i for i in items}
    by_group = {}
    for item in items:
        by_group.setdefault(item["_group"], []).append(item)

    def ref(entry_id):
        target = entry_by_id[entry_id]
        return {
            "id": target["id"],
            "title": target["title"],
            "status": target["status"],
            "url": target["_url"],
            "section": target.get("section"),
            "region": target["_region"],
        }

    def region_block(region_id, extra):
        region = next(r for r in cat["regions"] if r["id"] == region_id)
        block = {
            "id": region["id"],
            "title": region["title"],
            "summary": region.get("summary") or "",
            "lead": list(region.get("lead") or []),
        }
        block.update(extra)
        block["groups"] = [
            group_payload(g, by_group) for g in cat["groups"] if g["region"] == region_id
        ]
        block["items"] = [
            compact_item(i, content_commit, cat) for i in items if i["_region"] == region_id
        ]
        return block

    services = [service_block(i) for i in items if i["kind"] == SERVICE_KIND]

    system_block = region_block(
        "system",
        {
            "positioning": list(cat.get("positioning") or []),
            "layers": list(cat.get("layers") or []),
            "lifecycle": list(cat.get("lifecycle") or []),
            "knowledge_loop": list(cat.get("knowledge_loop") or []),
            "gates": list(cat.get("gates") or []),
            "security": list(cat.get("security") or []),
            "failure_taxonomy": list(cat.get("failure_taxonomy") or []),
            "service_discovery": {
                "schema": DISCOVERY_SCHEMA,
                "resolution": ["node_profile", "canonical"],
                "rule": "索引只渲染公开字段；固定入口与节点 profile 的内网地址只存在于 descriptor 与节点回执。",
            },
            "services": services,
            "roles": [
                {
                    "id": role["id"],
                    "label": role["label"],
                    "summary": role["summary"],
                    "must_read": [ref(x) for x in role["must_read"]],
                    "optional": [ref(x) for x in role.get("optional") or []],
                }
                for role in cat["roles"]
            ],
        },
    )

    onboarding = cat.get("onboarding") or {}
    onboarding_block = region_block(
        "onboarding",
        {
            "automation_levels": dict(onboarding.get("automation_levels") or {}),
            "steps": list(onboarding.get("steps") or []),
            "acceptance": list(onboarding.get("acceptance") or []),
            "block_codes": list(onboarding.get("block_codes") or []),
            "drift": dict(onboarding.get("drift") or {}),
        },
    )

    payload = {
        "schema": INDEX_SCHEMA,
        "name": cat["name"],
        "version": cat["version"],
        "title": cat["title"],
        "generated_at": generated_at,
        "git": {
            "repo": cat["git"]["repo"],
            "commit": content_commit,
            "path": cat["git"]["source"],
        },
        "workdir": dict(cat["workdir"]),
        "entry": {
            "markdown": object_url(cat["oss"], cat["oss"]["prefix"].strip("/") + "/index.md"),
            "json": object_url(cat["oss"], cat["oss"]["prefix"].strip("/") + "/index.json"),
            "manifest": object_url(cat["oss"], cat["oss"]["prefix"].strip("/") + "/manifest.json"),
            "stable": True,
            "note": "入口地址永久固定；更新只重新指向上述三个文件，已发布版本目录不改动。",
        },
        "navigation": [
            {"id": "system", "title": cat["regions"][0]["title"], "summary": cat["regions"][0]["summary"]},
            {"id": "onboarding", "title": cat["regions"][1]["title"], "summary": cat["regions"][1]["summary"]},
        ],
        "system": system_block,
        "onboarding": onboarding_block,
        "roles": system_block["roles"],
        "groups": [group_payload(g, by_group) for g in cat["groups"]],
        "items": [item_payload(i, content_commit, cat) for i in items],
        "counts": dict(
            count_items(items),
            roles=len(cat["roles"]),
            groups=len(cat["groups"]),
            regions=len(cat["regions"]),
            services=len(services),
            onboarding_steps=len(onboarding.get("steps") or []),
        ),
        "role_ids": sorted(role_index),
        "compatible_with": ["ark.noahsark-index.v1"],
    }
    return payload


def build_index_md(cat, items, generated_at, content_commit):
    oss = cat["oss"]
    prefix = oss["prefix"].strip("/")
    entry_url = object_url(oss, prefix + "/index.md")
    entry_by_id = {i["id"]: i for i in items}
    by_group = {}
    for item in items:
        by_group.setdefault(item["_group"], []).append(item)
    lines = []

    def add(text=""):
        lines.append(text)

    def group_table(region_id):
        for group in cat["groups"]:
            if group["region"] != region_id:
                continue
            add("### %s" % group["label"])
            add()
            if group.get("summary"):
                add(group["summary"])
                add()
            add("| id | 条目 | 版本 | 状态 | 校验值前 12 位 |")
            add("| --- | --- | --- | --- | --- |")
            for entry in group["entries"]:
                item = entry_by_id[entry["id"]]
                label = item["title"]
                if item["_url"]:
                    label = md_link(label, item["_url"])
                    if item.get("file"):
                        label += "（文件 %s）" % item["file"]
                    if item.get("section") and item["kind"] == "rule":
                        label += "（见 %s）" % item["section"]
                elif item["kind"] == SERVICE_KIND and item.get("_entry_kind") == "fixed-ip":
                    label += "（固定入口，节点侧登记）"
                else:
                    label += "（规划中，未发布）"
                add(
                    "| %s | %s | %s | %s | %s |"
                    % (
                        item["id"],
                        label,
                        item.get("version") or "-",
                        status_label(item["status"]),
                        short_sha(item["_sha"]) or "-",
                    )
                )
            add()

    system_region, onboarding_region = cat["regions"]

    # ============================================================ 区域一
    add("# 一、%s" % system_region["title"])
    add()
    for line in system_region.get("lead") or []:
        add(line)
        add()

    add("## 唯一对外地址")
    add()
    add("本页是 NoahsArk 唯一的对外地址，长期不变：")
    add()
    add("    %s" % entry_url)
    add()
    add("| 文件 | 用途 | 地址 |")
    add("| --- | --- | --- |")
    for item in items:
        if item["_group"] != "meta":
            continue
        add(
            "| %s | %s | %s |"
            % (item["id"], item["summary"], md_link(item["_path"], item["_url"]))
        )
    add()
    add("index.md 给人看，index.json 给程序读，manifest.json 记录版本与校验值。")
    add("三者同批发布；判断本目录是否被改动，比对 index.md 与 index.json 的校验值即可。")
    add()

    add("## 平台定位与边界")
    add()
    for line in cat.get("positioning") or []:
        add("- %s" % line)
    add()

    add("## 三层职责")
    add()
    add("| 层 | 负责 | 不负责 | 门禁 |")
    add("| --- | --- | --- | --- |")
    for layer in cat.get("layers") or []:
        add(
            "| %s | %s | %s | %s |"
            % (layer["title"], layer["responsibility"], layer["not_responsible"], layer["gate"])
        )
    add()

    add("## 执行闭环")
    add()
    add("| 阶段 | 负责方 | 产物 | 门禁 |")
    add("| --- | --- | --- | --- |")
    for step in cat.get("lifecycle") or []:
        add("| %s | %s | %s | %s |" % (step["step"], step["actor"], step["artifact"], step["gate"]))
    add()

    add("## 角色体系")
    add()
    add("按自己的角色照顺序读，读完必读项再开工；角色与节点绑定，运行中的 Run 不换模型。")
    add()
    for role in cat["roles"]:
        add("### %s（%s）" % (role["label"], role["id"]))
        add()
        add(role["summary"])
        add()
        for idx, ref in enumerate(role["must_read"], 1):
            target = entry_by_id[ref]
            label = target["title"]
            if target.get("section") and target["kind"] == "rule":
                label += "（见 %s）" % target["section"]
            add("%d. %s —— %s" % (idx, md_link(label, target["_url"]), target["summary"]))
        if role.get("optional"):
            add()
            names = [entry_by_id[x]["title"] for x in role["optional"]]
            add("按需参考：%s。" % "、".join(names))
        add()

    add("## 服务与版本")
    add()
    add("| 服务 | 版本 | 可解析地址 | 鉴权 | 健康/新鲜度 | 降级 | 固定入口 |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for item in items:
        if item["kind"] != SERVICE_KIND:
            continue
        descriptor = item["_descriptor"]
        auth = descriptor.get("auth") or {}
        verification = descriptor.get("verification") or {}
        fallback = descriptor.get("fallback") or {}
        health = (descriptor.get("health_contract") or {}).get("probe", {}).get("path") or "-"
        fixed_entry = (descriptor.get("discovery") or {}).get("canonical", {}).get("fixed_entry")
        fixed_ip = item.get("_entry_kind") == "fixed-ip"
        service_cell = item["title"] if fixed_ip else md_link(item["title"], item["_url"])
        address_cell = "固定入口（节点侧登记）" if fixed_ip else item["_url"]
        degrade = []
        if descriptor.get("id") == "svc.knowledge-service":
            degrade = ["BLOCKED_KB_STALE", "BLOCKED_KB_UNAVAILABLE"]
        else:
            degrade = ["BLOCKED_STORAGE_UNAVAILABLE", "BLOCKED_STORAGE_QUARANTINE"]
        add(
            "| %s | %s | %s | %s（%s） | %s；%s | %s | %s |"
            % (
                service_cell,
                item.get("version") or "-",
                address_cell,
                auth.get("mode") or "-",
                "、".join(auth.get("scopes") or []) or "-",
                health,
                "已确认 %d 项 / 待确认 %d 项"
                % (len(verification.get("confirmed") or []), len(verification.get("pending") or [])),
                "、".join(degrade),
                "已登记（仅节点侧）" if fixed_entry else "-",
            )
        )
    add()
    add(
        "服务地址按 descriptor 的 entry_kind 渲染：node-name 服务写可解析节点名，"
        "fixed-ip 服务只写「固定入口（节点侧登记）」；固定入口与节点 profile 的内网地址"
        "只登记在 descriptor 与节点回执，生成物不含私网地址。"
    )
    add()

    for item in items:
        if item["kind"] != SERVICE_KIND:
            continue
        descriptor = item["_descriptor"]
        add("### 服务契约：%s" % item["title"])
        add()
        add(item["summary"])
        add()
        if item.get("_entry_kind") == "fixed-ip":
            descriptor_name = item["_path"].rsplit("/", 1)[-1]
            add(
                "入口：固定入口（节点侧登记，见 descriptor:%s#discovery.canonical.fixed_entry）"
                % descriptor_name
            )
            add()
        add("| 接口 | 方法 | 路径 | scope | 说明 |")
        add("| --- | --- | --- | --- | --- |")
        for api in descriptor.get("api") or []:
            add(
                "| %s | %s | %s | %s | %s |"
                % (
                    api.get("name"),
                    api.get("method") or "-",
                    api.get("path") or "待确认路由",
                    api.get("scope") or "-",
                    api.get("note") or "-",
                )
            )
        add()
        if descriptor.get("categories"):
            add("分类与保留：%s" % descriptor["categories"].get("rule"))
            add()
        if descriptor.get("integrity"):
            integrity = descriptor["integrity"]
            add("完整性：")
            add()
            add("- 服务端复算：%s" % integrity.get("server_sha"))
            add("- 幂等：%s" % integrity.get("idempotent"))
            add("- 冲突：%s" % integrity.get("conflict"))
            add("- 回读：%s" % integrity.get("readback"))
            add("- 审计：%s" % integrity.get("audit"))
            add()
        if descriptor.get("freshness"):
            fresh = descriptor["freshness"]
            add(
                "新鲜度：取值 %s；增量间隔 %s 秒；一致性规则：%s"
                % (
                    "/".join(fresh.get("values") or []),
                    fresh.get("interval_seconds"),
                    fresh.get("consistency_rule"),
                )
            )
            add()
        if descriptor.get("snapshot"):
            add("查询快照：%s" % descriptor["snapshot"].get("rule"))
            add()
        discovery = descriptor.get("discovery") or {}
        canonical = discovery.get("canonical") or {}
        resolution = discovery.get("resolution") or {}
        profiles = discovery.get("node_profiles") or []
        vm_boundary = discovery.get("vm_boundary") or {}
        canonical_display = (
            "固定入口（节点侧登记）"
            if (canonical.get("entry_kind") or "node-name") == "fixed-ip"
            else (canonical.get("endpoint") or "-")
        )
        add(
            "服务发现：canonical %s，端口 %s，解析顺序 %s；节点 profile：%s；VM 边界：%s。"
            "固定入口仅登记于 descriptor:discovery.canonical.fixed_entry，不进入索引与 OSS。"
            % (
                canonical_display,
                canonical.get("port") or "-",
                " → ".join(resolution.get("order") or []),
                "、".join(p.get("node") or "-" for p in profiles) or "-",
                "、".join(vm_boundary.get("nodes") or []) or "-",
            )
        )
        add()
        health_contract = descriptor.get("health_contract") or {}
        add(
            "健康契约：探针 %s，所需字段 %s，判定规则：%s；写链路不看健康码：%s"
            % (
                (health_contract.get("probe") or {}).get("path") or "-",
                "、".join(health_contract.get("required_fields") or []) or "-",
                clause(health_contract.get("status_rule")),
                clause(health_contract.get("write_path_rule")),
            )
        )
        add()
        token = descriptor.get("token_bootstrap") or {}
        token_storage = token.get("storage") or {}
        token_lifecycle = token.get("lifecycle") or {}
        add(
            "Token bootstrap：mode=%s，status=%s，自助 API=%s，最小 scope=%s，存储位置=%s / %s；"
            "rotate：%s；revoke：%s。"
            % (
                token.get("mode") or "-",
                token.get("status") or "-",
                "是" if token.get("bootstrap_api") else "否",
                "、".join(token.get("minimum_scopes") or []) or "-",
                token_storage.get("linux") or "-",
                token_storage.get("windows") or "-",
                clause(token_lifecycle.get("rotate")),
                clause(token_lifecycle.get("revoke")),
            )
        )
        add()
        anon = (descriptor.get("auth") or {}).get("anonymous_read") or {}
        proposal = (descriptor.get("auth") or {}).get("anonymous_proposal") or {}
        if anon:
            add(
                "只读公开边界：状态 %s；匿名可读 %s；仍需鉴权 %s；限流、分页与脱敏见 `%s#auth.anonymous_read`。"
                % (
                    anon.get("status") or "-",
                    "、".join(anon.get("public_api") or []) or "-",
                    "、".join(anon.get("protected_api") or []) or "-",
                    item["_path"],
                )
            )
            add()
            if proposal:
                add(
                    "匿名提案：状态 %s；入口 %s；只允许 %s；"
                    "必填 provenance(issue/run/agent/node)、evidence(bytes/sha256)、request_id、idempotency_key；"
                    "审核/入库/sync/admin 仍需 write token（见 descriptor:auth.anonymous_proposal）。"
                    % (
                        proposal.get("status") or "-",
                        proposal.get("endpoint") or "-",
                        "、".join(proposal.get("allowed_status") or []) or "-",
                    )
                )
                add()
            change = anon.get("service_change") or {}
            proposal_change = proposal.get("service_change") or {}
            scope_source = proposal_change if proposal_change.get("scope") else change
            if scope_source.get("scope"):
                docs = "、".join(
                    dict.fromkeys(
                        doc
                        for doc in (change.get("doc"), proposal_change.get("doc"))
                        if doc
                    )
                )
                scope_text = str(scope_source.get("scope")).rstrip("。")
                add("服务改动：%s（见 %s）。" % (clause(scope_text), docs or "-"))
                add()
        add("失败码：%s。" % ("、".join(service_failure_codes(descriptor)) or "-"))
        add()
        add("完整契约：`%s`（机器可读，字段含 auth、api、errors、fallback、verification）。" % item["_path"])
        add()

    add("## 知识时序（沉淀与复用）")
    add()
    add("| 步骤 | 负责方 | 说明 | 产物 |")
    add("| --- | --- | --- | --- |")
    for step in cat.get("knowledge_loop") or []:
        add("| %s | %s | %s | %s |" % (step["step"], step["actor"], step["detail"], step["artifact"]))
    add()

    add("## 门禁与降级")
    add()
    add("| 门禁 | 规则 | 失败码 |")
    add("| --- | --- | --- |")
    for gate in cat.get("gates") or []:
        add("| %s | %s | %s |" % (gate["gate"], gate["rule"], gate["failure"]))
    add()

    add("## 失败分类与停止条件")
    add()
    add("| 失败码 | 触发条件 | 处理 | 停止 |")
    add("| --- | --- | --- | --- |")
    for rule in cat.get("failure_taxonomy") or []:
        add(
            "| %s | %s | %s | %s |"
            % (
                rule.get("code") or "-",
                rule.get("when") or "-",
                rule.get("action") or "-",
                "停止" if rule.get("stop") else "降级继续",
            )
        )
    add()

    add("## 安全边界")
    add()
    for line in cat.get("security") or []:
        add("- %s" % line)
    add()

    add("## 硬规则")
    add()
    add("以下规则对所有角色生效，正文在模板对应章节；两边冲突时以模板正文为准。")
    add()
    add("| 规则 | 要点 |")
    add("| --- | --- |")
    for item in items:
        if item["kind"] != "rule":
            continue
        cell = item["summary"]
        if item["_url"]:
            cell += "（%s）" % md_link(item.get("section") or "模板正文", item["_url"])
        add("| %s | %s |" % (item["title"], cell))
    add()

    planned = [i for i in items if i["status"] == "planned"]
    if planned:
        add("## 未发布条目")
        add()
        add("下列条目已规划但尚未发布，暂不提供链接；在它们变为可用之前不要引用。")
        add()
        for item in planned:
            add("- %s（%s）：%s" % (item["title"], item["id"], item["summary"]))
        add()

    add("## 体系条目")
    add()
    group_table("system")

    # ============================================================ 区域二
    add("# 二、%s" % onboarding_region["title"])
    add()
    for line in onboarding_region.get("lead") or []:
        add(line)
        add()

    onboarding = cat.get("onboarding") or {}
    levels = onboarding.get("automation_levels") or {}
    steps = onboarding.get("steps") or []
    acceptance = {a["id"]: a for a in onboarding.get("acceptance") or []}
    codes = {c["code"]: c for c in onboarding.get("block_codes") or []}

    add("## 初始化总览")
    add()
    add("| # | 步骤 | 范围 | 自动化 | 依赖 | 验收 | 阻塞码 |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for idx, step in enumerate(steps, 1):
        add(
            "| %d | %s（%s） | %s | %s | %s | %s | %s |"
            % (
                idx,
                step["title"],
                step["id"],
                step["scope"],
                step["automation"],
                "、".join(step.get("requires") or []) or "-",
                "、".join(step.get("acceptance") or []),
                "、".join(step.get("block_codes") or []),
            )
        )
    add()

    add("## 自动化级别")
    add()
    add("| 级别 | 含义 |")
    add("| --- | --- |")
    for level in ALLOWED_AUTOMATION:
        add("| %s | %s |" % (level, levels.get(level) or "-"))
    add()

    add("## 工作目录")
    add()
    add("每个任务一个独立目录，路径固定：")
    add()
    add("| 系统 | 路径 |")
    add("| --- | --- |")
    add("| Windows | `%s` |" % cat["workdir"]["windows"])
    add("| Linux | `%s` |" % cat["workdir"]["linux"])
    add("| macOS | `%s` |" % cat["workdir"]["macos"])
    add()
    add("`<tasks-id>` 用任务编号，不加时间戳后缀；不要用 C 盘、系统临时目录或运行时工作目录代替。")
    add()

    add("## 步骤详解")
    add()
    for idx, step in enumerate(steps, 1):
        add("### %d. %s（%s）" % (idx, step["title"], step["id"]))
        add()
        add("- 范围：%s；自动化级别：%s。" % (step["scope"], step["automation"]))
        if step.get("requires"):
            add("- 依赖：%s。" % "、".join(step["requires"]))
        add("- 说明：%s" % step["detail"])
        for command in step.get("commands") or []:
            add("- 命令：`%s`" % command)
        add(
            "- 验收：%s。"
            % "；".join("%s（%s）" % (x, (acceptance.get(x) or {}).get("title", "")) for x in step["acceptance"])
        )
        add(
            "- 失败：%s。"
            % "；".join("%s（%s）" % (c, block_code_meaning(codes.get(c) or {})) for c in step["block_codes"])
        )
        add()

    add("## 初始化验收")
    add()
    add("| 验收 id | 项目 | 判定规则 |")
    add("| --- | --- | --- |")
    for item in onboarding.get("acceptance") or []:
        add("| %s | %s | %s |" % (item["id"], item["title"], item["rule"]))
    add()

    add("## 阻塞码")
    add()
    add("| 码 | 领域 | 含义 | 处理 |")
    add("| --- | --- | --- | --- |")
    for code in onboarding.get("block_codes") or []:
        add(
            "| %s | %s | %s | %s |"
            % (code["code"], code.get("area") or "knowledge", block_code_meaning(code), block_code_action(code))
        )
    add()

    drift = onboarding.get("drift") or {}
    add("## 漂移分类")
    add()
    add("状态取值：%s。" % "、".join(drift.get("states") or []))
    add()
    add(drift.get("rule") or "")
    add()
    add(drift.get("receipt_rule") or "")
    add()

    add("## 凭据边界")
    add()
    for line in cat.get("security") or []:
        add("- %s" % line)
    add()

    add("## 初始化条目")
    add()
    group_table("onboarding")

    add("## 更新与回滚")
    add()
    add("- 源文件在 Git 仓库 `%s` 的 `%s` 目录，本页由 `build-index.py` 生成，不手工修改。" % (cat["git"]["repo"], cat["git"]["source"]))
    add("- 更新只重新发布 index.md、index.json、manifest.json 三个文件；已发布的内容按版本目录留存，不改动。")
    add("- 回滚同样只还原这三个文件，不删除任何版本目录。")
    add("- 上传后逐个匿名读取校验，校验值与 manifest.json 不一致即视为发布失败。")
    add("- 服务契约改动必须同时改 services/*.json 与 catalog 条目；双声明漂移会让构建失败。")
    add()

    add("## 本页版本")
    add()
    add("| 项 | 值 |")
    add("| --- | --- |")
    add("| 版本 | %s |" % cat["version"])
    add("| 生成时间 | %s |" % generated_at)
    add("| 内容所在提交 | %s |" % short_sha(content_commit))
    add("| 区域 | %s |" % "、".join(r["title"] for r in cat["regions"]))
    add("| 服务 | %s |" % "、".join(i["title"] for i in items if i["kind"] == SERVICE_KIND))
    add()
    return "\n".join(lines).rstrip() + "\n"


def build_manifest(cat, items, generated_at, content_commit, files, extra=None):
    extra = extra or {}
    counts = count_items(items)
    template_versions = sorted(
        {i.get("version") for i in items if i["id"].startswith("agent-template.") and i.get("version")}
    )
    services = []
    for i in items:
        if i["kind"] != SERVICE_KIND:
            continue
        entry = {
            "id": i["id"],
            "version": i.get("version"),
            "entry_kind": i.get("_entry_kind") or "node-name",
            "descriptor": i["_path"],
        }
        if entry["entry_kind"] == "fixed-ip":
            entry["endpoint_ref"] = "descriptor:%s#discovery.canonical.fixed_entry" % i["_path"]
        else:
            entry["endpoint"] = i["_url"]
        services.append(entry)
    regions = [
        {
            "id": region["id"],
            "title": region["title"],
            "groups": sum(1 for g in cat["groups"] if g["region"] == region["id"]),
            "items": sum(1 for i in items if i["_region"] == region["id"]),
        }
        for region in cat["regions"]
    ]
    return {
        "schema": MANIFEST_SCHEMA,
        "name": cat["name"],
        "version": cat["version"],
        "channel": "immutable",
        "source_version": cat["version"],
        "generated_at": generated_at,
        "git": {
            "repo": cat["git"]["repo"],
            "commit": content_commit,
            "path": cat["git"]["source"],
            "dirty": bool(extra.get("dirty")),
            "tracked": bool(extra.get("tracked")),
        },
        "oss": {
            "bucket": cat["oss"]["bucket"],
            "endpoint": cat["oss"]["endpoint"],
            "prefix": cat["oss"]["prefix"].strip("/"),
            "visibility": cat["oss"]["visibility"],
        },
        "compatible": {
            "catalog": CATALOG_SCHEMA,
            "index": INDEX_SCHEMA,
            "agent_template": template_versions[-1] if template_versions else None,
            "previous_index": "ark.noahsark-index.v1",
        },
        "regions": regions,
        "services": services,
        "counts": dict(
            counts,
            roles=len(cat["roles"]),
            groups=len(cat["groups"]),
            regions=len(cat["regions"]),
            services=len(services),
            onboarding_steps=len((cat.get("onboarding") or {}).get("steps") or []),
        ),
        "files": files,
        "checks": {
            "catalog": "valid",
            "secret_scan": "pending",
            "time_lint": "pending",
            "links": "offline-only",
            "service_contract": "valid",
            "md_json": "consistent",
        },
        "notes": [
            "入口地址永久固定；重发只覆盖 index.md、index.json、manifest.json。",
            "回滚只还原这三个文件，不删除任何版本目录。",
            "服务地址只写可解析节点名；真实内网地址只存在于节点侧回执。",
            "固定入口与节点 profile 只存在于节点侧 descriptor 与回执，生成物不含私网地址。",
        ],
    }


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as handle:
        handle.write(text)


def render(cat, content_commit, generated_at, out_dir, extra=None):
    resolved = resolve_entries(cat)
    index_md = build_index_md(cat, resolved, generated_at, content_commit)
    index_json = build_index_json(cat, resolved, generated_at, content_commit)

    md_bytes = index_md.encode("utf-8")
    json_text = json.dumps(index_json, ensure_ascii=False, indent=2) + "\n"
    json_bytes = json_text.encode("utf-8")

    prefix = cat["oss"]["prefix"].strip("/")
    files = [
        {
            "path": "index.md",
            "object": prefix + "/index.md",
            "role": "entry",
            "sha256": sha256_bytes(md_bytes),
            "bytes": len(md_bytes),
        },
        {
            "path": "index.json",
            "object": prefix + "/index.json",
            "role": "machine",
            "sha256": sha256_bytes(json_bytes),
            "bytes": len(json_bytes),
        },
    ]
    manifest = build_manifest(cat, resolved, generated_at, content_commit, files, extra)
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"

    for name, blob in (
        ("index.json", json_text),
        ("index.md", index_md),
        ("manifest.json", manifest_text),
    ):
        hit = RFC1918.search(blob)
        if hit:
            raise BuildError(
                EXIT_FAILED,
                "生成物 %s 含私网地址字面量 %s（固定入口未剥离）" % (name, hit.group(0)),
            )
        legacy = LEGACY_ENTRY.search(blob)
        if legacy:
            raise BuildError(
                EXIT_FAILED,
                "生成物 %s 含已失效旧入口 %s（必须使用固定入口 endpoint_ref）" % (name, legacy.group(0)),
            )

    write_text(out_dir / "index.md", index_md)
    write_text(out_dir / "index.json", json_text)
    write_json(out_dir / "manifest.json", manifest)
    return manifest, files


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成 NoahsArk 对外总览三件套")
    parser.add_argument("--catalog", default=str(HERE / "catalog.json"), help="catalog.json 路径")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT), help="输出目录")
    parser.add_argument("--commit", default=None, help="覆盖内容提交号（默认取 HEAD）")
    parser.add_argument("--allow-dirty", action="store_true", help="允许源目录有未提交改动")
    parser.add_argument("--check-only", action="store_true", help="只校验 catalog，不生成文件")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    try:
        cat = load_json(Path(args.catalog))
        errors = validate_catalog(cat)
        if errors:
            for err in errors:
                sys.stderr.write("校验失败: %s\n" % err)
            return EXIT_FAILED

        if args.check_only:
            summary = {"ok": True, "version": cat["version"], "errors": []}
            print(json.dumps(summary, ensure_ascii=False))
            return EXIT_OK

        dirty = git_dirty([cat["git"]["source"]])
        if dirty and not args.allow_dirty:
            raise BuildError(
                EXIT_NO_SOURCE,
                "源目录 %s 有未提交改动；先提交，或显式加 --allow-dirty" % cat["git"]["source"],
            )
        content_commit = args.commit or git_head()
        if not re.match(r"^[0-9a-f]{40}$", content_commit):
            raise BuildError(EXIT_NO_SOURCE, "提交号格式非法")

        tracked = git_tracked(cat["git"]["source"] + "/catalog.json")
        if not tracked and not args.allow_dirty:
            raise BuildError(
                EXIT_NO_SOURCE,
                "源文件 %s/catalog.json 未纳入版本库；先 git add 再生成" % cat["git"]["source"],
            )

        out_dir = Path(args.out_dir)
        generated_at = now_display()
        manifest, files = render(
            cat,
            content_commit,
            generated_at,
            out_dir,
            {"dirty": dirty, "tracked": tracked},
        )

        result = {
            "ok": True,
            "version": manifest["version"],
            "out_dir": str(out_dir),
            "generated_at": generated_at,
            "commit": content_commit,
            "dirty": dirty,
            "counts": manifest["counts"],
            "files": files,
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("生成完成：%s" % out_dir)
            print("  版本 %s，生成时间 %s" % (manifest["version"], generated_at))
            print(
                "  条目 %d（可用 %d / 规划 %d），角色 %d，分组 %d，区域 %d"
                % (
                    manifest["counts"]["items"],
                    manifest["counts"]["available"],
                    manifest["counts"]["planned"],
                    manifest["counts"]["roles"],
                    manifest["counts"]["groups"],
                    manifest["counts"]["regions"],
                )
            )
            for item in files:
                print("  %s  %d 字节  %s" % (item["object"], item["bytes"], item["sha256"][:12]))
        return EXIT_OK
    except BuildError as exc:
        sys.stderr.write("%s\n" % exc.message)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
