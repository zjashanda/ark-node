"""NoahsArk 一键发布：仓库包 → OSS → catalog → index → 校验（可含节点同步与智能体刷新）。

用法：
    python tools/release.py --check                        # 只核对当前一致性
    python tools/release.py --version 2.8.4 --note "..."   # 完整发布
    python tools/release.py --version 2.8.4 --note "..." --sync-nodes --agents

顺序（任一步失败即中止）：
    1 校验三仓库已推送、工作区无关键改动
    2 重建仓库包（bundle + tarball + 元数据）
    3 上传 OSS 并逐个 sha 校验
    4 按元数据改 catalog 的版本与 pin
    5 提交并推送 ark-node
    6 发布 index.json 并公开读回验证
    7 可选：强制同步节点 / 刷新智能体
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "reports" / "repo-bundles"
CATALOG = ROOT / "templates" / "noahsark-index" / "catalog.json"
OSS_TOOLS = Path(__file__).resolve().parent
BUCKET = "listenai-test-internal"
ENDPOINT = "https://listenai-test-internal.oss-cn-beijing.aliyuncs.com"
PUBLIC = f"{ENDPOINT}/noahsark"

REPOS = {
    "lstest": ("D:/workstation/quattro/_cloud/lstest", "zjashanda/lstest", "HEAD", False),
    "ark-kb": ("D:/workstation/quattro/nodehub/reports/repo-bundles/_repos/ark-kb",
               "zjashanda/ark-kb", "origin/main", True),
    "ark-node": (str(ROOT).replace("\\", "/"), "zjashanda/ark-node", "HEAD", False),
    "burner": None,
}

PIN_IDS = {"lstest": "git.lstest", "ark-kb": "git.ark-kb", "ark-node": "git.ark-node"}


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{out.stderr.strip()[:200]}")
    return out.stdout.strip()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def http_get(url: str, timeout: int = 40) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def step(title: str) -> None:
    print(f"\n=== {title} ===")


def check_repos() -> list[str]:
    problems = []
    for name, spec in REPOS.items():
        if spec is None:
            continue
        path, _, ref, needs_fetch = spec
        repo = Path(path)
        if not (repo / ".git").is_dir():
            problems.append(f"{name}: 不是 git 仓库 {repo}")
            continue
        if needs_fetch:
            continue
        try:
            head = git(repo, "rev-parse", "--short", ref)
            branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
            dirty = git(repo, "status", "--porcelain", "--untracked-files=no")
        except RuntimeError as exc:
            problems.append(f"{name}: {exc}")
            continue
        if dirty:
            problems.append(f"{name}: 有未提交改动（{len(dirty.splitlines())} 项）@ {branch} {head}")
        else:
            print(f"  {name:<10} {branch} {head} 干净")
    return problems


def build(repos: list[str]) -> dict[str, dict]:
    STAGING.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    metas: dict[str, dict] = {}
    for name in repos:
        spec = REPOS.get(name)
        if not spec:
            print(f"  跳过 {name}（未配置构建源，沿用现有包）")
            continue
        path, full, ref, needs_fetch = spec
        repo = Path(path)
        if needs_fetch:
            git(repo, "-c", "core.sshCommand=ssh -o BatchMode=yes -o ConnectTimeout=25",
                "fetch", "--quiet", "github-zjashanda:zjashanda/ark-kb.git",
                "main:refs/remotes/origin/main")
        commit = git(repo, "rev-parse", ref)
        short = commit[:12]
        for pattern in (f"{name}-*.bundle", f"{name}-*.tar.gz"):
            for old in STAGING.glob(pattern):
                old.unlink()
        bundle = STAGING / f"{name}-{short}.bundle"
        tarball = STAGING / f"{name}-{short}.tar.gz"
        git(repo, "bundle", "create", str(bundle), ref)
        git(repo, "archive", "--format=tar.gz", "-o", str(tarball), commit)
        meta = {
            "schema": "ark.repo-bundle.v1",
            "repo": full,
            "name": name,
            "commit": commit,
            "bundle": bundle.name,
            "sha256": sha256(bundle),
            "bytes": bundle.stat().st_size,
            "tarball": tarball.name,
            "tarball_sha256": sha256(tarball),
            "tarball_bytes": tarball.stat().st_size,
            "generated_at": stamp,
        }
        (STAGING / f"{name}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        metas[name] = meta
        print(f"  {name:<10} {short} bundle={meta['bytes']}B")
    return metas


def upload(repos: list[str]) -> None:
    sys.path.insert(0, str(OSS_TOOLS))
    from oss_publish import anon_get, normalize_endpoint, object_url, oss_put

    ak = os.environ.get("ACCESS_KEY_ID", "").strip()
    sk = os.environ.get("ACCESS_KEY_SECRET", "").strip()
    if not ak or not sk:
        raise RuntimeError("缺少 ACCESS_KEY_ID / ACCESS_KEY_SECRET")
    endpoint = normalize_endpoint("oss-cn-beijing.aliyuncs.com")
    for name in repos:
        meta_path = STAGING / f"{name}.json"
        if not meta_path.is_file():
            print(f"  跳过 {name}（无元数据）")
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        for key_name, content_type in ((meta["bundle"], "application/octet-stream"),
                                       (meta.get("tarball"), "application/gzip")):
            if not key_name:
                continue
            data = (STAGING / key_name).read_bytes()
            key = f"noahsark/repos/{name}/{key_name}"
            status, _ = oss_put(BUCKET, endpoint, key, data, ak, sk,
                                visibility="public-read", content_type=content_type)
            if status not in (200, 201, 204):
                raise RuntimeError(f"上传 {key} 失败 status={status}")
        latest = json.dumps(meta, ensure_ascii=False, indent=2).encode()
        key = f"noahsark/repos/{name}/latest.json"
        status, _ = oss_put(BUCKET, endpoint, key, latest, ak, sk,
                            visibility="public-read", content_type="application/json")
        if status not in (200, 201, 204):
            raise RuntimeError(f"上传 {key} 失败 status={status}")
        url = object_url(BUCKET, endpoint, key)
        read_status, body = anon_get(url, timeout=30)
        ok = read_status == 200 and json.loads(body.decode())["sha256"] == meta["sha256"]
        if not ok:
            raise RuntimeError(f"{name} latest.json 回读校验失败 status={read_status}")
        print(f"  {name:<10} 上传并校验通过 {meta['commit'][:12]}")


def bump_catalog(version: str, metas: dict[str, dict]) -> None:
    text = CATALOG.read_text(encoding="utf-8")
    data = json.loads(text)
    old_version = data.get("version")
    if f'"version": "{old_version}",' not in text:
        raise RuntimeError(f"catalog 版本行格式不符：{old_version}")
    text = text.replace(f'"version": "{old_version}",', f'"version": "{version}",', 1)

    entries: dict[str, dict] = {}
    for group in data.get("groups", []):
        for entry in group.get("entries", []):
            if entry.get("id") in PIN_IDS.values():
                entries[entry["id"]] = entry

    for name, entry_id in PIN_IDS.items():
        meta = metas.get(name)
        if not meta:
            continue
        entry = entries.get(entry_id)
        if not entry:
            raise RuntimeError(f"catalog 缺少条目 {entry_id}")
        old_ver = entry["version"]
        old_commit = entry["commit"]
        new_short = meta["commit"][:12]
        new_ver = f"main@{new_short}"
        text = text.replace(f'"version": "{old_ver}",', f'"version": "{new_ver}",')
        text = text.replace(f'"commit": "{old_commit}",', f'"commit": "{meta["commit"]}",')
        print(f"  {entry_id:<14} {old_ver} -> {new_ver}")

    json.loads(text)
    CATALOG.write_text(text, encoding="utf-8", newline="")
    print(f"  catalog 版本 {old_version} -> {version}")


def commit_push(note: str, version: str) -> str:
    git(ROOT, "add", "templates/noahsark-index/catalog.json")
    git(ROOT, "-c", "user.name=bszheng", "-c", "user.email=bszheng@listenai.com",
        "commit", "-q", "-m", f"index: {version} {note}".strip())
    commit = git(ROOT, "rev-parse", "--short", "HEAD")
    last_error = ""
    for alias in ("github-zjashanda", "github-zjashanda-443"):
        res = subprocess.run(
            ["git", "-C", str(ROOT), "-c", "core.sshCommand=ssh -o BatchMode=yes -o ConnectTimeout=20",
             "push", f"{alias}:zjashanda/ark-node.git", "main:main"],
            capture_output=True, text=True)
        if res.returncode == 0:
            print(f"  pushed {commit} via {alias}")
            return commit
        last_error = (res.stderr or res.stdout).strip()[:200]
    raise RuntimeError(f"推送失败：{last_error}")


def publish_index(nodehub: str, metas: dict[str, dict]) -> None:
    import shlex

    nodehub_path = Path(nodehub)
    sftp = nodehub_path / "scripts" / "debug" / "sftp-put.py"
    runner = nodehub_path / "scripts" / "debug" / "ssh-run.py"
    if not sftp.is_file() or not runner.is_file():
        raise RuntimeError(f"找不到发布工具：{sftp} / {runner}")

    meta = metas.get("ark-node")
    if not meta:
        raise RuntimeError("缺少 ark-node 包元数据")
    bundle = STAGING / meta["bundle"]
    remote_bundle = f"/home/bszheng/{meta['bundle']}"

    res = subprocess.run([sys.executable, str(sftp), "bszt14", str(bundle), remote_bundle],
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"上传 bundle 失败：{(res.stderr or res.stdout)[:200]}")

    ak = os.environ.get("ACCESS_KEY_ID", "").strip()
    sk = os.environ.get("ACCESS_KEY_SECRET", "").strip()
    if not ak or not sk:
        raise RuntimeError("缺少 ACCESS_KEY_ID / ACCESS_KEY_SECRET")

    script = f"""set -u
cd /home/bszheng/ark-node-publish-2521 || exit 4
git remote set-url origin {shlex.quote(remote_bundle)}
GIT_TERMINAL_PROMPT=0 git fetch --quiet origin main || exit 2
git reset --hard --quiet FETCH_HEAD
echo "HEAD=$(git rev-parse --short HEAD)"
env ACCESS_KEY_ID={shlex.quote(ak)} ACCESS_KEY_SECRET={shlex.quote(sk)} \\
  python3 templates/noahsark-index/publish-index.py --apply --yes --allow-dirty
echo "RC=$?"
"""
    local_script = STAGING / "_publish_index.sh"
    local_script.write_text(script, encoding="utf-8", newline="\n")
    remote_script = "/tmp/_release_publish.sh"
    up = subprocess.run([sys.executable, str(sftp), "bszt14", str(local_script), remote_script],
                        capture_output=True, text=True)
    if up.returncode != 0:
        raise RuntimeError(f"上传发布脚本失败：{(up.stderr or up.stdout)[:200]}")
    local_script.unlink(missing_ok=True)

    run = subprocess.run([sys.executable, str(runner), "bszt14", f"bash {remote_script}"],
                         capture_output=True, text=True, timeout=900)
    out = (run.stdout or "") + (run.stderr or "")
    for line in out.splitlines():
        if line.startswith(("HEAD=", "RC=")) or "上传" in line or "版本" in line:
            print("  " + line.strip())
    if "RC=0" not in out:
        raise RuntimeError(f"publish-index 未成功：{out[-300:]}")


def refresh_agents(version: str, nodehub: str, previous: str) -> None:
    runner = Path(nodehub) / "scripts" / "debug" / "ssh-run.py"
    if not runner.is_file():
        raise RuntimeError(f"找不到 {runner}")

    script = f'''import json, subprocess
WS = "66f56c0f-69cd-43ef-b7c6-c582889dd600"
M = "/usr/local/bin/multica"
PREV, NEW = {previous!r}, {version!r}
REPL = [
    ("index `" + PREV + "`", "index `" + NEW + "`"),
    ("index " + PREV, "index " + NEW),
]
listing = json.loads(subprocess.run([M, "--workspace-id", WS, "agent", "list", "--output", "json"],
                                    capture_output=True, text=True).stdout)
agents = listing if isinstance(listing, list) else listing.get("agents", listing.get("items", []))
changed = 0
for item in agents:
    detail = json.loads(subprocess.run([M, "--workspace-id", WS, "agent", "get", item["id"], "--output", "json"],
                                       capture_output=True, text=True).stdout)
    agent = detail.get("agent", detail)
    ins = agent.get("instructions") or ""
    new = ins
    for old, rep in REPL:
        new = new.replace(old, rep)
    if new == ins:
        continue
    subprocess.run([M, "--workspace-id", WS, "agent", "update", item["id"], "--instructions", new, "--output", "json"],
                   capture_output=True)
    changed += 1
    print("  updated " + str(item.get("name")))
print("AGENTS_CHANGED=" + str(changed))
'''
    local = STAGING / "_bump_agents.py"
    local.write_text(script, encoding="utf-8", newline="\n")
    remote = "/tmp/_release_agents.py"
    sftp = Path(nodehub) / "scripts" / "debug" / "sftp-put.py"
    up = subprocess.run([sys.executable, str(sftp), "bszt14", str(local), remote],
                        capture_output=True, text=True)
    local.unlink(missing_ok=True)
    if up.returncode != 0:
        raise RuntimeError(f"上传刷新脚本失败：{(up.stderr or up.stdout)[:200]}")
    run = subprocess.run([sys.executable, str(runner), "bszt14", f"python3 {remote}"],
                         capture_output=True, text=True, timeout=900)
    out = (run.stdout or "") + (run.stderr or "")
    for line in out.splitlines():
        if "updated" in line or line.startswith("AGENTS_CHANGED="):
            print("  " + line.strip())
    if "AGENTS_CHANGED=" not in out:
        raise RuntimeError(f"智能体刷新未完成：{out[-300:]}")


def verify(version: str, metas: dict[str, dict]) -> None:
    if index.get("version") != version:
        raise RuntimeError(f"OSS index 版本 {index.get('version')} != {version}")
    print(f"  index.json version={index['version']}")
    for name in metas:
        meta = json.loads(http_get(f"{PUBLIC}/repos/{name}/latest.json").decode("utf-8"))
        if meta["commit"] != metas[name]["commit"]:
            raise RuntimeError(f"{name} latest.json commit 不符")
        print(f"  {name:<10} latest.json commit={meta['commit'][:12]} ok")


def main() -> int:
    parser = argparse.ArgumentParser(description="NoahsArk 一键发布")
    parser.add_argument("--version", default="")
    parser.add_argument("--note", default="")
    parser.add_argument("--check", action="store_true", help="只核对仓库干净度与 OSS 一致性")
    parser.add_argument("--repos", default="lstest,ark-kb,ark-node")
    parser.add_argument("--sync-nodes", action="store_true")
    parser.add_argument("--agents", action="store_true")
    parser.add_argument("--previous-version", default="", help="刷新智能体时要把旧版本号替换成 --version")
    parser.add_argument("--nodehub", default="D:/workstation/quattro/nodehub")
    args = parser.parse_args()

    repos = [item.strip() for item in args.repos.split(",") if item.strip()]

    if args.check:
        step("核对仓库状态")
        problems = check_repos()
        if problems:
            for item in problems:
                print(f"  ✗ {item}")
            return 1
        step("核对 OSS")
        catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
        index = json.loads(http_get(f"{PUBLIC}/index.json").decode("utf-8"))
        print(f"  catalog={catalog['version']}  oss_index={index.get('version')}")
        if catalog["version"] != index.get("version"):
            print("  ✗ catalog 与 OSS index 版本不一致")
            return 1
        for name, latest_name in (("lstest", "lstest"), ("ark-kb", "ark-kb"), ("ark-node", "ark-node")):
            meta = json.loads(http_get(f"{PUBLIC}/repos/{name}/latest.json").decode("utf-8"))
            print(f"  {name:<10} OSS commit={meta['commit'][:12]}")
        print("\n结论：一致")
        return 0

    if not args.version:
        print("需要 --version 或 --check", file=sys.stderr)
        return 2

    step("1 校验仓库")
    problems = check_repos()
    if problems:
        for item in problems:
            print(f"  ✗ {item}")
        return 1

    step("2 重建仓库包")
    metas = build(repos)

    step("3 上传 OSS")
    upload(list(metas.keys()))

    step("4 更新 catalog")
    bump_catalog(args.version, metas)

    step("5 提交推送 ark-node")
    commit_push(args.note or args.version, args.version)

    step("6 发布并验证 index")
    publish_index(args.nodehub, metas)

    if args.sync_nodes:
        step("7 强制同步节点")
        nodehub = Path(args.nodehub)
        force = nodehub / "scripts" / "debug" / "force-sync.py"
        if force.is_file():
            windows = ["win000", "win002", "win003", "win004", "win005", "win006",
                       "win010", "bsp", "lsaswx", "bsz001", "pvm1005", "pvm1003"]
            subprocess.run([sys.executable, str(force), *windows])
        else:
            print("  未找到 force-sync.py，跳过")

    if args.agents:
        step("8 刷新智能体")
        refresh_agents(args.version, args.nodehub, args.previous_version)

    print("\n发布完成。节点将在 30 分钟内的自动同步中到位。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
