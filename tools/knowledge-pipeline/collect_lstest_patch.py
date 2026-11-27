"""bszT14 审核员侧收集器：从 8090 取回节点上传的 lstest 修改，复核后创建候选。

只允许在受信节点（bszT14/linux003）运行；执行节点不参与。

用法：
    python scripts/ops/collect_lstest_patch.py --node bszT14 --task PROBE-LSTEST-PATCH --run run-1
    python scripts/ops/collect_lstest_patch.py --node bszT14 --task X --run r1 --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

STORAGE = "http://10.11.63.24:8090"
API = "http://10.11.63.42:8091"
ACTOR = "bszT14"
KIND_BY_PATH = (
    ("markers/catalog.json", "marker_code"),
    ("profiles/", "profile_rule"),
    ("scripts/", "framework_fix"),
    ("adapters/", "framework_fix"),
)
TRUSTED_NODES = ("bszT14", "linux003")


def _get(url: str, node: str, timeout: int = 30) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"X-Ark-Node": node})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except urllib.error.URLError as error:
        return 0, str(error).encode()


def list_objects(storage: str, node: str, task: str, run: str) -> list[dict[str, Any]]:
    prefix = f"{node}/{task}/{run}/lstest-patch"
    query = urllib.parse.urlencode({"category": "evidence", "scope": "noahsark", "prefix": prefix})
    status, body = _get(f"{storage}/storage/v1/list?{query}", ACTOR)
    if status != 200:
        raise RuntimeError(f"list 失败 status={status} body={body[:200]!r}")
    return json.loads(body.decode("utf-8")).get("entries") or []


def fetch_object(storage: str, key: str) -> bytes:
    status, body = _get(f"{storage}/storage/v1/objects/evidence/noahsark/{key}", ACTOR)
    if status != 200:
        raise RuntimeError(f"下载 {key} 失败 status={status}")
    return body


def infer_kind(paths: list[str], override: str | None) -> str:
    if override:
        return override
    for path in paths:
        for marker, kind in KIND_BY_PATH:
            if marker in path:
                return kind
    return "framework_fix"


def verify(manifest: dict[str, Any], files: dict[str, bytes]) -> list[str]:
    problems: list[str] = []
    commit = str(manifest.get("base_commit") or "")
    if not (7 <= len(commit) <= 40) or any(ch not in "0123456789abcdef" for ch in commit):
        problems.append(f"base_commit 非法：{commit!r}")
    if not manifest.get("checks"):
        problems.append("checks 为空")
    sample = manifest.get("log_sample") or []
    if not sample:
        problems.append("log_sample 为空")
    if len(sample) > 50:
        problems.append(f"log_sample 超过 50 行（{len(sample)}）")
    for item in manifest.get("files") or []:
        path = item.get("path")
        data = files.get(path)
        if data is None:
            problems.append(f"缺少文件 {path}")
            continue
        actual = hashlib.sha256(data).hexdigest()
        if actual != item.get("sha256"):
            problems.append(f"{path} sha256 不匹配")
        if len(data) != item.get("bytes"):
            problems.append(f"{path} 字节数不匹配")
    return problems


def build_proposal(manifest: dict[str, Any], files: dict[str, bytes], kind: str,
                   manifest_key: str, manifest_bytes: bytes, task: str, run: str) -> dict[str, Any]:
    paths = [item["path"] for item in manifest.get("files") or []]
    target = paths[0] if paths else "unknown"
    payload = files.get(target, b"").decode("utf-8", "replace")
    checks = manifest.get("checks") or {}
    summary = f"unclaimed={checks.get('unclaimed_lines')} silent={checks.get('silent_rules')} preflight={checks.get('preflight_pass')}"
    return {
        "draft": {
            "type": "method",
            "title": f"[待审] {kind} · {target} · {task}",
            "claim": (
                f"节点 {manifest.get('node')} 在任务 {task} 中修改 {target}，"
                f"基于 lstest {manifest.get('base_commit')}（{manifest.get('base_version') or '未标版本'}）；"
                f"适配校验 {summary}；见 log_sample 与 8090 原件。"
            ),
            "scope": {"device": None, "project": task, "firmware": None, "platform": "lstest"},
            "evidence": [{
                "kind": "file",
                "ref": f"8090://evidence/noahsark/{manifest_key}",
                "bytes": len(manifest_bytes),
                "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "note": "patch manifest（含文件清单与校验值）",
            }],
            "confidence": "medium",
            "kind": kind,
            "publish_target": "lstest",
            "target_path": target,
            "base_commit": manifest.get("base_commit"),
            "base_version": manifest.get("base_version") or None,
            "payload": payload,
            "payload_format": "full",
            "checks": checks,
            "log_sample": manifest.get("log_sample") or [],
            "log_sample_note": manifest.get("log_sample_note") or None,
        },
        "provenance": {"issue": task, "run": run, "agent": "collector", "node": ACTOR},
        "idempotency_key": "collect-" + hashlib.sha256(
            f"{manifest_key}:{manifest.get('base_commit')}".encode()
        ).hexdigest()[:24],
    }


def create_proposal(api: str, body: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{api}/v1/proposals", data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return {"status": response.status, "body": json.loads(response.read().decode("utf-8"))}
    except urllib.error.HTTPError as error:
        return {"status": error.code, "body": error.read().decode("utf-8", "replace")[:300]}
    except urllib.error.URLError as error:
        return {"status": 0, "body": str(error)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 8090 收集 lstest 修改并创建候选")
    parser.add_argument("--node", required=True, help="上传方节点名")
    parser.add_argument("--task", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--kind", default=None, help="覆盖推断的候选类型")
    parser.add_argument("--storage", default=STORAGE)
    parser.add_argument("--api", default=API)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    if ACTOR not in TRUSTED_NODES:
        print(f"拒绝：只允许受信节点提交（当前 {ACTOR}）", file=sys.stderr)
        return 2

    try:
        entries = list_objects(args.storage, args.node, args.task, args.run)
    except RuntimeError as error:
        print(f"拒绝：{error}", file=sys.stderr)
        return 2

    manifest_key = f"{args.node}/{args.task}/{args.run}/lstest-patch/manifest.json"
    if not any(item.get("key") == manifest_key for item in entries):
        print(f"未找到 manifest：{manifest_key}", file=sys.stderr)
        return 2

    manifest_bytes = fetch_object(args.storage, manifest_key)
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if manifest.get("schema") != "lstest.patch-upload.v1":
        print(f"schema 不符：{manifest.get('schema')}", file=sys.stderr)
        return 2
    if manifest.get("node") != args.node or manifest.get("task") != args.task or manifest.get("run") != args.run:
        print("manifest 与请求的 node/task/run 不一致", file=sys.stderr)
        return 2

    files: dict[str, bytes] = {}
    for item in manifest.get("files") or []:
        files[item["path"]] = fetch_object(args.storage, f"{args.node}/{args.task}/{args.run}/lstest-patch/{item['path']}")

    problems = verify(manifest, files)
    if problems:
        print("复核未通过：", file=sys.stderr)
        for item in problems:
            print(f"  - {item}", file=sys.stderr)
        return 1

    kind = infer_kind([item["path"] for item in manifest.get("files") or []], args.kind)
    body = build_proposal(manifest, files, kind, manifest_key, manifest_bytes, args.task, args.run)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(body, handle, ensure_ascii=False, indent=2)
        print(f"候选草案已写入 {args.out}", file=sys.stderr)
    if args.dry_run:
        print(json.dumps(body, ensure_ascii=False, indent=2))
        print("dry-run：未提交", file=sys.stderr)
        return 0

    result = create_proposal(args.api, body)
    ok = result.get("status") in (200, 201)
    print(f"  [{'ok' if ok else 'fail'}] {kind} {body['draft']['target_path']} -> {result.get('status')} {result.get('body')}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
