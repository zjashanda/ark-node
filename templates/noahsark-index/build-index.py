#!/usr/bin/env python3
"""从 catalog.json 生成对外总览三件套。

输入：templates/noahsark-index/catalog.json（唯一手工维护源）
输出：build/noahsark-index/index.md、index.json、manifest.json

设计约束：
- 只依赖标准库，Python 3.8 及以上可运行。
- 不访问网络；地址由 catalog 的 oss 与 git 配置拼出，避免多处写死主机名。
- 校验值与提交号必须真实：本地文件当场算，Git 提交号取 HEAD，禁止手填。
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

CATALOG_SCHEMA = "ark.noahsark-index.catalog.v1"
INDEX_SCHEMA = "ark.noahsark-index.v1"
MANIFEST_SCHEMA = "ark.noahsark-index.manifest.v1"

FMT_DISPLAY = "%Y-%m-%d %H:%M:%S"
TZ_CN = timezone(timedelta(hours=8))

ALLOWED_VISIBILITY = ("public-read", "private")
ALLOWED_KIND = ("entry", "git", "rule", "dir", "asset")
ALLOWED_STATUS = ("available", "planned")

DEFAULT_OUT = REPO_ROOT / "build" / "noahsark-index"
AGENT_MANIFEST = REPO_ROOT / "templates" / "agents" / "manifest.json"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NO_SOURCE = 3


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


def validate_catalog(cat):
    errors = []

    def need(cond, message):
        if not cond:
            errors.append(message)

    need(cat.get("schema") == CATALOG_SCHEMA, "catalog.schema 必须是 %s" % CATALOG_SCHEMA)
    need(bool(cat.get("version")), "catalog.version 不能为空")
    need(bool(cat.get("title")), "catalog.title 不能为空")

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
    need(bool(workdir.get("windows")), "catalog.workdir.windows 不能为空")
    need(bool(workdir.get("linux")), "catalog.workdir.linux 不能为空")

    roles = cat.get("roles") or []
    need(len(roles) >= 1, "至少需要一个角色")
    role_ids = [r.get("id") for r in roles]
    need(len(role_ids) == len(set(role_ids)), "角色 id 重复")
    for role in roles:
        need(bool(role.get("label")), "角色 %s 缺少 label" % role.get("id"))
        need(bool(role.get("summary")), "角色 %s 缺少 summary" % role.get("id"))
        need(bool(role.get("must_read")), "角色 %s 的必读清单不能为空" % role.get("id"))

    groups = cat.get("groups") or []
    need(len(groups) >= 1, "至少需要一个分组")
    group_ids = [g.get("id") for g in groups]
    need(len(group_ids) == len(set(group_ids)), "分组 id 重复")

    entries = []
    for group in groups:
        need(bool(group.get("label")), "分组 %s 缺少 label" % group.get("id"))
        for entry in group.get("entries") or []:
            entries.append((group, entry))

    entry_ids = [e.get("id") for _, e in entries]
    need(len(entry_ids) == len(set(entry_ids)), "条目 id 重复")
    known = set(entry_ids)

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
                bool(entry.get("oss")) or bool(entry.get("git")),
                "可用条目 %s 必须有 oss 或 git 地址" % eid,
            )
        if entry.get("status") == "planned":
            need(
                not entry.get("oss") and not entry.get("git"),
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
        for role in entry.get("roles") or []:
            need(role in role_ids, "条目 %s 引用了不存在的角色 %s" % (eid, role))
        for dep in entry.get("requires") or []:
            need(dep in known, "条目 %s 依赖了不存在的条目 %s" % (eid, dep))
            need(dep != eid, "条目 %s 不能依赖自身" % eid)

    for role in roles:
        for ref in list(role.get("must_read") or []) + list(role.get("optional") or []):
            need(ref in known, "角色 %s 引用了不存在的条目 %s" % (role.get("id"), ref))

    errors.extend(find_cycles(entries))
    return errors


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
            item["_url"] = None
            item["_url_file"] = None
            item["_path"] = None
            item["_sha"] = entry.get("sha256")

            if entry.get("oss"):
                item["_path"] = entry["oss"]
                item["_url"] = object_url(oss, entry["oss"])
            elif entry.get("git"):
                repo = entry["git"].split("/")[-1]
                item["_path"] = entry["git"]
                item["_url"] = git_repo_url(git_conf["owner"], repo)

            if entry.get("local"):
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
            elif entry.get("sha256"):
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


def build_index_json(cat, items, generated_at, content_commit):
    role_index = {r["id"]: r for r in cat["roles"]}
    entry_by_id = {i["id"]: i for i in items}

    def ref(entry_id):
        target = entry_by_id[entry_id]
        return {
            "id": target["id"],
            "title": target["title"],
            "status": target["status"],
            "url": target["_url"],
            "section": target.get("section"),
        }

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
        "positioning": list(cat.get("positioning") or []),
        "workdir": dict(cat["workdir"]),
        "entry": {
            "markdown": object_url(cat["oss"], cat["oss"]["prefix"].strip("/") + "/index.md"),
            "json": object_url(cat["oss"], cat["oss"]["prefix"].strip("/") + "/index.json"),
            "manifest": object_url(cat["oss"], cat["oss"]["prefix"].strip("/") + "/manifest.json"),
            "stable": True,
            "note": "入口地址永久固定；更新只重新指向上述三个文件，已发布版本目录不改动。",
        },
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
        "groups": [
            {
                "id": group["id"],
                "label": group["label"],
                "summary": group.get("summary") or "",
                "items": [e["id"] for e in group["entries"]],
            }
            for group in cat["groups"]
        ],
        "items": [
            {
                "id": item["id"],
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
                "roles": list(item.get("roles") or []),
                "requires": list(item.get("requires") or []),
                "summary": item["summary"],
            }
            for item in items
        ],
        "counts": count_items(items),
        "role_ids": sorted(role_index),
    }
    return payload


def count_items(items):
    available = sum(1 for i in items if i["status"] == "available")
    return {
        "items": len(items),
        "available": available,
        "planned": len(items) - available,
    }


def status_label(status):
    return "可用" if status == "available" else "规划中"


def md_link(text, url, extra=None):
    if not url:
        return text
    return "[%s](%s)" % (text, url)


def short_sha(value):
    if not value:
        return ""
    return value[:12]


def build_index_md(cat, items, generated_at, content_commit):
    oss = cat["oss"]
    prefix = oss["prefix"].strip("/")
    entry_url = object_url(oss, prefix + "/index.md")
    entry_by_id = {i["id"]: i for i in items}
    lines = []

    def add(text=""):
        lines.append(text)

    add("# %s" % cat["title"])
    add()
    add("本页是 NoahsArk 唯一的对外地址，长期不变：")
    add()
    add("    %s" % entry_url)
    add()
    add("外部工作区只需要记住这一个链接。读完本页，按下方角色清单进入对应条目即可，")
    add("不需要提前了解内部仓库、分支或节点。")
    add()

    add("## 平台定位")
    add()
    for line in cat.get("positioning") or []:
        add("- %s" % line)
    add()

    add("## 三步接入")
    add()
    add("1. 打开本页，读「平台定位」和下面与自己角色对应的一节。")
    add("2. 按「唯一任务目录」规则建好本次任务的目录，目录内先写 plan.md。")
    add("3. 读智能体模板正文，按其中的硬约束开始执行；需要清单、仓储、规则时回到本页对应分组。")
    add()

    add("## 只记一个地址")
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

    add("## 按角色读")
    add()
    add("按自己的角色照顺序读，读完必读项再开工。")
    add()
    for role in cat["roles"]:
        add("### %s（%s）" % (role["label"], role["id"]))
        add()
        add(role["summary"])
        add()
        for idx, ref in enumerate(role["must_read"], 1):
            target = entry_by_id[ref]
            label = target["title"]
            if target.get("section"):
                label += "（见 %s）" % target["section"]
            add("%d. %s —— %s" % (idx, md_link(label, target["_url"]), target["summary"]))
        if role.get("optional"):
            add()
            names = [entry_by_id[x]["title"] for x in role["optional"]]
            add("按需参考：%s。" % "、".join(names))
        add()

    add("## 条目总览")
    add()
    for group in cat["groups"]:
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
                if item.get("section"):
                    label += "（见 %s）" % item["section"]
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

    add("## 工作目录")
    add()
    add("每个任务一个独立目录，路径固定：")
    add()
    add("| 系统 | 路径 |")
    add("| --- | --- |")
    add("| Windows | `%s` |" % cat["workdir"]["windows"])
    add("| Linux | `%s` |" % cat["workdir"]["linux"])
    add()
    add("`<tasks-id>` 用任务编号，不加时间戳后缀；不要用 C 盘、系统临时目录或运行时工作目录代替。")
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

    add("## 更新与回滚")
    add()
    add("- 源文件在 Git 仓库 `%s` 的 `%s` 目录，本页由 `build-index.py` 生成，不手工修改。" % (cat["git"]["repo"], cat["git"]["source"]))
    add("- 更新只重新发布 index.md、index.json、manifest.json 三个文件；已发布的内容按版本目录留存，不改动。")
    add("- 回滚同样只还原这三个文件，不删除任何版本目录。")
    add("- 上传后逐个匿名读取校验，校验值与 manifest.json 不一致即视为发布失败。")
    add()
    add("## 本页版本")
    add()
    add("| 项 | 值 |")
    add("| --- | --- |")
    add("| 版本 | %s |" % cat["version"])
    add("| 生成时间 | %s |" % generated_at)
    add("| 内容所在提交 | %s |" % short_sha(content_commit))
    add()
    return "\n".join(lines).rstrip() + "\n"


def build_manifest(cat, items, generated_at, content_commit, files, extra=None):
    extra = extra or {}
    counts = count_items(items)
    template_versions = sorted(
        {i.get("version") for i in items if i["id"].startswith("agent-template.") and i.get("version")}
    )
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
        },
        "counts": dict(counts, roles=len(cat["roles"]), groups=len(cat["groups"])),
        "files": files,
        "checks": {
            "catalog": "valid",
            "secret_scan": "pending",
            "time_lint": "pending",
            "links": "offline-only",
        },
        "notes": [
            "入口地址永久固定；重发只覆盖 index.md、index.json、manifest.json。",
            "回滚只还原这三个文件，不删除任何版本目录。",
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
                "  条目 %d（可用 %d / 规划 %d），角色 %d，分组 %d"
                % (
                    manifest["counts"]["items"],
                    manifest["counts"]["available"],
                    manifest["counts"]["planned"],
                    manifest["counts"]["roles"],
                    manifest["counts"]["groups"],
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
