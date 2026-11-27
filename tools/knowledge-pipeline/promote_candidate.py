"""把已批准的 lstest 候选物化为待合并分支（只建分支与推送，不合并 main）。

在受信节点（bszT14/linux003）运行。

用法：
    python scripts/ops/promote_candidate.py --proposal-id prop-xxx
    python scripts/ops/promote_candidate.py --proposal-id prop-xxx --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

OUTBOX = "/home/zhsh/workspace/services/noahsark-knowledge/data/outbox"
REPO = "/home/bszheng/workspace/repos/lstest"
REMOTE = "github-zjashanda:zjashanda/lstest.git"
LSTEST_KINDS = ("marker_code", "profile_rule", "framework_fix")


def load_candidate(outbox: str, proposal_id: str) -> dict[str, Any] | None:
    sys.path.insert(0, "/home/zhsh/workspace/services/noahsark-knowledge")
    from arkks import proposals

    obj = proposals.Outbox(outbox)
    folded = obj._fold()
    for key, item in folded.items():
        if key == proposal_id or item.get("proposal_id") == proposal_id:
            return item
    return None


def run(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=check)


def safe_rel(path: str) -> str:
    raw = path.replace("\\", "/")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise ValueError(f"绝对路径不允许：{path}")
    if ".." in raw.split("/"):
        raise ValueError(f"含上跳不允许：{path}")
    rel = raw.strip("/")
    if not rel:
        raise ValueError("空路径")
    return rel


def plan(candidate: dict[str, Any], repo: str) -> dict[str, Any]:
    draft = candidate.get("draft") or {}
    kind = str(draft.get("kind") or "")
    if kind not in LSTEST_KINDS:
        raise ValueError(f"kind={kind} 不是 lstest 类候选")
    if str(draft.get("publish_target") or "") != "lstest":
        raise ValueError("publish_target 必须是 lstest")
    target = safe_rel(str(draft.get("target_path") or ""))
    base = str(draft.get("base_commit") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,40}", base):
        raise ValueError(f"base_commit 非法：{base!r}")
    exists = run(repo, "cat-file", "-e", f"{base}^{{commit}}", check=False).returncode == 0
    if not exists:
        raise ValueError(f"仓库中找不到 base_commit {base}（需要先 fetch）")
    payload = draft.get("payload")
    if not isinstance(payload, str) or not payload:
        raise ValueError("payload 为空")
    fmt = str(draft.get("payload_format") or "full")
    if fmt not in ("full", "unified_diff"):
        raise ValueError(f"payload_format 非法：{fmt}")
    proposal_id = str(candidate.get("proposal_id") or candidate.get("id") or "")
    branch = "promo/" + re.sub(r"[^A-Za-z0-9._-]", "-", proposal_id)
    return {
        "proposal_id": proposal_id,
        "branch": branch,
        "kind": kind,
        "target_path": target,
        "base_commit": base,
        "payload": payload,
        "payload_format": fmt,
        "checks": draft.get("checks") or {},
        "issue": str((candidate.get("provenance") or {}).get("issue") or ""),
        "node": str((candidate.get("provenance") or {}).get("node") or ""),
        "status": str(candidate.get("status") or ""),
    }


def commit_message(item: dict[str, Any]) -> str:
    checks = item["checks"]
    summary = ", ".join(f"{k}={checks[k]}" for k in sorted(checks)) or "无"
    return (
        f"{item['kind']}: {item['target_path']}\n\n"
        f"候选: {item['proposal_id']}\n"
        f"基准: lstest {item['base_commit']}\n"
        f"来源: issue={item['issue']} node={item['node']}\n"
        f"校验: {summary}\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="候选 → lstest 待合并分支")
    parser.add_argument("--proposal-id", required=True)
    parser.add_argument("--outbox", default=OUTBOX)
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--remote", default=REMOTE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-push", action="store_true", help="只本地建分支，不推送（自测用）")
    parser.add_argument("--allow-pending", action="store_true", help="仅用于自测，正式必须 accepted")
    parser.add_argument("--force", action="store_true", help="分支已存在时重建")
    args = parser.parse_args(argv)

    candidate = load_candidate(args.outbox, args.proposal_id)
    if not candidate:
        print(f"未找到候选 {args.proposal_id}", file=sys.stderr)
        return 2

    try:
        item = plan(candidate, args.repo)
    except ValueError as error:
        print(f"拒绝：{error}", file=sys.stderr)
        return 2

    if item["status"] != "accepted" and not args.allow_pending:
        print(f"拒绝：候选状态是 {item['status']}，正式推广要求 accepted", file=sys.stderr)
        return 2

    print(json.dumps({k: v for k, v in item.items() if k != "payload"}, ensure_ascii=False, indent=2))
    print(f"  payload 长度 = {len(item['payload'])} 字符（{item['payload_format']}）")

    if args.dry_run:
        print("dry-run：未改动仓库", file=sys.stderr)
        return 0

    if run(args.repo, "rev-parse", "--verify", item["branch"], check=False).returncode == 0:
        if not args.force:
            print(f"拒绝：分支 {item['branch']} 已存在（加 --force 重建）", file=sys.stderr)
            return 2
        run(args.repo, "branch", "-D", item["branch"])

    run(args.repo, "fetch", "--quiet", args.remote, "main")
    run(args.repo, "checkout", "--quiet", "-b", item["branch"], item["base_commit"])

    target = Path(args.repo) / item["target_path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    if item["payload_format"] == "full":
        target.write_text(item["payload"], encoding="utf-8")
    else:
        patch = Path(args.repo) / ".candidate.patch"
        patch.write_text(item["payload"], encoding="utf-8")
        applied = run(args.repo, "apply", "--whitespace=nowarn", str(patch), check=False)
        patch.unlink(missing_ok=True)
        if applied.returncode != 0:
            print(f"拒绝：补丁无法应用（{applied.stderr.strip()[:200]}）", file=sys.stderr)
            return 1

    run(args.repo, "add", item["target_path"])
    run(args.repo, "-c", "user.name=bszheng", "-c", "user.email=bszheng@listenai.com",
        "commit", "-q", "-m", commit_message(item))
    head = run(args.repo, "rev-parse", "--short", "HEAD").stdout.strip()
    diff = run(args.repo, "diff", "--stat", item["base_commit"], "HEAD").stdout.strip()
    if args.no_push:
        print(f"  branch={item['branch']} head={head} pushed=skipped（--no-push）")
        if diff:
            print("  " + diff.replace("\n", "\n  "))
        return 0
    pushed = run(args.repo, "push", args.remote, f"{item['branch']}:{item['branch']}", check=False)
    ok = pushed.returncode == 0
    print(f"  branch={item['branch']} head={head} pushed={'yes' if ok else 'no'}")
    if diff:
        print("  " + diff.replace("\n", "\n  "))
    if not ok:
        print(f"  推送失败：{pushed.stderr.strip()[:300]}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
