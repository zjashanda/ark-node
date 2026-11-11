#!/usr/bin/env python3
"""校验已生成的总览三件套。

默认离线：只读本地文件，检查结构、计数、依赖、链接形态与校验值。
加 --online：逐个匿名读取对外地址，确认可访问且校验值一致。
加 --check-git：用 GitHub API 确认仓库存在（不抓取网页）。

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

INDEX_SCHEMA = "ark.noahsark-index.v1"
MANIFEST_SCHEMA = "ark.noahsark-index.manifest.v1"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_NETWORK = 5

HEX40 = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{40,}(?![0-9A-Za-z])")
REQUIRED_ITEM_KEYS = (
    "id",
    "title",
    "kind",
    "status",
    "version",
    "path",
    "url",
    "roles",
    "requires",
    "summary",
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


def check_manifest(out_dir, manifest, errors):
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
        if item.get("status") == "available":
            available += 1
            if not item.get("url"):
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

    counts = index.get("counts") or {}
    if counts.get("items") != len(items):
        errors.append("counts.items 与实际条目数不一致")
    if counts.get("available") != available:
        errors.append("counts.available 与实际可用条目数不一致")

    for role in index.get("roles") or []:
        if not role.get("must_read"):
            errors.append("角色 %s 的必读清单为空" % role.get("id"))
        for ref in list(role.get("must_read") or []) + list(role.get("optional") or []):
            if (ref or {}).get("id") not in known:
                errors.append("角色 %s 引用了未知条目 %s" % (role.get("id"), (ref or {}).get("id")))
    return items


def check_index_urls(index, errors):
    base = (index.get("entry") or {}).get("markdown") or ""
    if not base:
        errors.append("index.entry.markdown 缺失")
    for item in index.get("items") or []:
        url = item.get("url") or ""
        if not url:
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
    available_urls = {i["url"] for i in index.get("items") or [] if i.get("url")}
    for url in available_urls:
        if url not in text:
            errors.append("index.md 缺少可用条目地址: %s" % url)
    for item in index.get("items") or []:
        if item.get("status") == "planned" and item.get("title") not in text:
            errors.append("index.md 未标注规划中条目: %s" % item.get("id"))


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
        check_index_urls(index, errors)
        check_markdown(out_dir, index, errors)
        check_manifest(out_dir, manifest, errors)
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
            print("校验通过：%d 个条目（可用 %d），%d 个角色" % (
                (index.get("counts") or {}).get("items", 0),
                (index.get("counts") or {}).get("available", 0),
                len(index.get("roles") or []),
            ))
    if errors:
        return EXIT_NETWORK if any("匿名读取失败" in e or "不可达" in e for e in errors) else EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
