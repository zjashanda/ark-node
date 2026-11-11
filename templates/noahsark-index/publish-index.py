#!/usr/bin/env python3
"""NoahsArk 根索引发布器：把 build/noahsark-index/ 三件套推到 OSS。

设计约束
--------
* 只发布 ``index.json`` / ``index.md`` / ``manifest.json`` 三个固定对象，
  永远不碰 ``noahsark/agent-template/`` 等版本目录 —— 版本物料不可变，
  根索引只做「重指向」。发布器没有删除能力。
* 上传顺序固定为 index.json -> index.md -> manifest.json。
  manifest 最后上传，保证读者看到 manifest 时它描述的内容已经就位。
* 全部凭据只从进程环境变量读取（ACCESS_KEY_ID / ACCESS_KEY_SECRET），
  缺失即退出码 4，绝不落盘、绝不打印。
* 发布前必须通过：secret_scan、time_lint、生成、离线校验。
  任何一项失败即拒绝上传（先测后发的硬规则）。
* 上传后逐对象匿名回读并比对 sha256，回读不符即视为失败。

用法::

    # 只生成上传计划，不联网写入（默认）
    python3 templates/noahsark-index/publish-index.py

    # 真正上传
    ACCESS_KEY_ID=... ACCESS_KEY_SECRET=... \\
        python3 templates/noahsark-index/publish-index.py --apply --yes

退出码
------
0 成功（含 dry-run）
1 校验或上传失败
2 用法错误
3 NO_SOURCE：源文件缺失 / 未纳入版本库 / 工作树脏
4 FORBIDDEN：凭据缺失或来源不明
5 NETWORK：准备阶段联网查询失败
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
BUILD_DIR = REPO_ROOT / "build" / "noahsark-index"
CATALOG = HERE / "catalog.json"
TOOLS = REPO_ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NO_SOURCE = 3
EXIT_FORBIDDEN = 4
EXIT_NETWORK = 5

CONTENT_TYPES = {
    "index.md": "text/markdown; charset=utf-8",
    "index.json": "application/json; charset=utf-8",
    "manifest.json": "application/json; charset=utf-8",
}
# manifest 必须最后上传
UPLOAD_ORDER = ("index.json", "index.md", "manifest.json")


class PublishError(Exception):
    def __init__(self, message: str, code: int = EXIT_FAILED) -> None:
        super().__init__(message)
        self.code = code


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def git(args) -> str:
    try:
        out = subprocess.run(
            ["git"] + list(args),
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:  # pragma: no cover - git 缺失时
        raise PublishError("无法执行 git：%s" % exc) from exc
    if out.returncode != 0:
        return ""
    return out.stdout.decode("utf-8", "replace").strip()


def git_tracked(path: Path) -> bool:
    rel = path.resolve().relative_to(REPO_ROOT).as_posix()
    return bool(git(["ls-files", "--error-unmatch", rel]))


def git_dirty(paths) -> bool:
    rel = [p.resolve().relative_to(REPO_ROOT).as_posix() for p in paths]
    status = git(["status", "--porcelain", "--untracked-files=no", "--"] + rel)
    return bool(status)


def run_script(path: Path, args, timeout: int = 300):
    proc = subprocess.run(
        [sys.executable, str(path)] + list(args),
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode(
        "utf-8", "replace"
    )


def require_source() -> None:
    """源文件必须在版本库中且干净，否则拒绝发布。"""
    for path in (CATALOG, HERE / "build-index.py", HERE / "publish-index.py"):
        if not path.exists():
            raise PublishError("缺少源文件：%s" % path, EXIT_NO_SOURCE)
    if not git_tracked(CATALOG):
        raise PublishError(
            "catalog.json 未纳入版本库，先 git add 再发布", EXIT_NO_SOURCE
        )


def run_gates(manifest: dict) -> dict:
    """先测后发：secret_scan + time_lint，通过后把结果写回 manifest.checks。"""
    generated = [BUILD_DIR / name for name in UPLOAD_ORDER]
    scanned = generated + [CATALOG]
    code, out, err = run_script(
        TOOLS / "secret_scan.py", [str(p) for p in scanned]
    )
    if code != 0:
        raise PublishError(
            "密钥扫描未通过（退出码 %d）\n%s" % (code, (out + err).strip())
        )

    linted = scanned + [HERE / "build-index.py", HERE / "publish-index.py"]
    code, out, err = run_script(TOOLS / "time_lint.py", [str(p) for p in linted])
    if code != 0:
        raise PublishError(
            "时间口径检查未通过（退出码 %d）\n%s" % (code, (out + err).strip())
        )

    manifest.setdefault("checks", {})
    manifest["checks"]["secret_scan"] = "pass"
    manifest["checks"]["time_lint"] = "pass"
    write_json(BUILD_DIR / "manifest.json", manifest)
    return manifest


def build(allow_dirty: bool) -> dict:
    args = ["--out-dir", str(BUILD_DIR)]
    if allow_dirty:
        args += ["--allow-dirty"]
    code, out, err = run_script(HERE / "build-index.py", args)
    if code != 0:
        raise PublishError("生成失败（退出码 %d）\n%s" % (code, (out + err).strip()))
    for name in UPLOAD_ORDER:
        if not (BUILD_DIR / name).exists():
            raise PublishError("生成后缺少 %s" % name, EXIT_NO_SOURCE)
    return load_json(BUILD_DIR / "manifest.json")


def offline_verify() -> None:
    code, out, err = run_script(
        HERE / "verify-index.py", ["--dir", str(BUILD_DIR), "--skip-tools"]
    )
    if code != 0:
        raise PublishError(
            "离线校验未通过（退出码 %d）\n%s" % (code, (out + err).strip())
        )


def plan(manifest: dict) -> list:
    """(object_key, local_path, sha256, bytes, content_type) 顺序列表。"""
    oss = manifest["oss"]
    prefix = oss["prefix"].rstrip("/")
    by_object = {item["object"]: item for item in manifest["files"]}
    steps = []
    for name in UPLOAD_ORDER:
        key = "%s/%s" % (prefix, name)
        local = BUILD_DIR / name
        data = local.read_bytes()
        digest = sha256_bytes(data)
        listed = by_object.get(key)
        if listed is not None:
            if listed.get("sha256") != digest:
                raise PublishError("manifest 中 %s 的 sha256 与实际文件不符" % key)
            if listed.get("bytes") not in (None, len(data)):
                raise PublishError("manifest 中 %s 的字节数与实际文件不符" % key)
        steps.append((key, local, digest, len(data), CONTENT_TYPES[name]))
    return steps


def remote_probe(base_url: str):
    """匿名读线上 index.json：返回 (version, sha256, status)。"""
    import hashlib

    from oss_publish import anon_get

    url = "%s/index.json" % base_url.rstrip("/")
    try:
        status, body = anon_get(url, timeout=30)
    except Exception as exc:  # noqa: BLE001 - 统一映射为 NETWORK
        raise PublishError("联网读取 %s 失败：%s" % (url, exc), EXIT_NETWORK) from exc
    if status == 404:
        return None, None, status
    if status != 200:
        raise PublishError(
            "线上 index.json 返回 HTTP %s（既非 200 也非 404）" % status, EXIT_NETWORK
        )
    try:
        remote = json.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 线上内容损坏同样按网络处理
        raise PublishError("线上 index.json 不是合法 JSON：%s" % exc, EXIT_NETWORK) from exc
    return remote.get("version"), hashlib.sha256(body).hexdigest(), status


def guard_version(manifest: dict, base_url: str, allow_republish: bool, allow_downgrade: bool) -> dict:
    local_version = manifest["version"]
    local_index_sha = sha256_bytes((BUILD_DIR / "index.json").read_bytes())
    remote_version, remote_sha, status = remote_probe(base_url)
    info = {
        "local_version": local_version,
        "remote_version": remote_version,
        "remote_status": status,
        "remote_index_sha256": remote_sha,
    }
    if remote_version is None:
        info["action"] = "create"
        return info
    if remote_version == local_version:
        if remote_sha == local_index_sha:
            info["action"] = "identical"
            return info
        if not allow_republish:
            raise PublishError(
                "线上已是版本 %s 但内容不同；用 --allow-republish 覆盖同一版本"
                % remote_version
            )
        info["action"] = "republish"
        return info
    try:
        remote_num = tuple(int(part) for part in str(remote_version).split("."))
        local_num = tuple(int(part) for part in str(local_version).split("."))
    except ValueError as exc:
        raise PublishError("版本号不是数字点分形式：%r / %r"
                           % (remote_version, local_version)) from exc
    if remote_num > local_num and not allow_downgrade:
        raise PublishError(
            "线上版本 %s 高于本次 %s；用 --allow-downgrade 才会降级"
            % (remote_version, local_version)
        )
    info["action"] = "downgrade" if remote_num > local_num else "upgrade"
    return info


def credentials():
    ak = os.environ.get("ACCESS_KEY_ID", "").strip()
    sk = os.environ.get("ACCESS_KEY_SECRET", "").strip()
    if not ak or not sk:
        raise PublishError(
            "缺少环境变量 ACCESS_KEY_ID / ACCESS_KEY_SECRET（不落盘、不打印）",
            EXIT_FORBIDDEN,
        )
    return ak, sk


def apply_plan(manifest: dict, steps) -> list:
    from oss_publish import anon_get, normalize_endpoint, object_url, oss_put

    oss = manifest["oss"]
    bucket = oss["bucket"]
    endpoint = normalize_endpoint(oss["endpoint"])
    visibility = oss.get("visibility", "public-read")
    if visibility not in ("public-read", "private"):
        raise PublishError("visibility 必须是 public-read 或 private：%r" % visibility,
                           EXIT_USAGE)
    ak, sk = credentials()

    results = []
    for key, local, digest, size, content_type in steps:
        data = local.read_bytes()
        status, body = oss_put(
            bucket,
            endpoint,
            key,
            data,
            ak,
            sk,
            visibility=visibility,
            content_type=content_type,
        )
        if status not in (200, 201, 204):
            raise PublishError(
                "上传 %s 失败：HTTP %s %s" % (key, status, body[:200]), EXIT_NETWORK
            )
        url = object_url(bucket, endpoint, key)
        read_status, read_body = anon_get(url, timeout=60)
        if read_status != 200:
            raise PublishError(
                "匿名回读 %s 返回 HTTP %s" % (url, read_status), EXIT_NETWORK
            )
        if sha256_bytes(read_body) != digest:
            raise PublishError("匿名回读 %s 的 sha256 不符" % url)
        results.append(
            {
                "object": key,
                "url": url,
                "sha256": digest,
                "bytes": size,
                "put_status": status,
                "read_status": read_status,
                "acl": visibility,
            }
        )
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="发布 NoahsArk 根索引（index.json / index.md / manifest.json）"
    )
    parser.add_argument("--apply", action="store_true", help="真正上传（默认只出计划）")
    parser.add_argument("--yes", action="store_true", help="确认执行，需与 --apply 同时给出")
    parser.add_argument("--allow-dirty", action="store_true", help="允许工作树未提交（仅调试）")
    parser.add_argument("--allow-republish", action="store_true", help="允许同版本覆盖")
    parser.add_argument("--allow-downgrade", action="store_true", help="允许版本号回退")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = parser.parse_args(argv)

    if args.apply and not args.yes:
        print("拒绝执行：--apply 必须与 --yes 同时给出", file=sys.stderr)
        return EXIT_USAGE

    report = {"dry_run": not args.apply}
    try:
        require_source()
        if not args.allow_dirty and git_dirty(
            [CATALOG, HERE / "build-index.py", HERE / "publish-index.py", HERE / "verify-index.py"]
        ):
            raise PublishError(
                "索引源文件有未提交改动，先提交再发布（或 --allow-dirty 仅用于调试）",
                EXIT_NO_SOURCE,
            )

        manifest = build(args.allow_dirty)
        manifest = run_gates(manifest)
        offline_verify()
        steps = plan(manifest)

        from oss_publish import normalize_endpoint, object_url

        oss = manifest["oss"]
        base_url = object_url(
            oss["bucket"], normalize_endpoint(oss["endpoint"]), oss["prefix"].rstrip("/")
        )
        version_info = guard_version(
            manifest, base_url, args.allow_republish, args.allow_downgrade
        )
        report["manifest"] = {
            "version": manifest["version"],
            "generated_at": manifest["generated_at"],
            "commit": manifest["git"]["commit"],
            "items": manifest.get("counts", {}).get("items"),
        }
        report["version_check"] = version_info
        report["upload_plan"] = [
            {"object": key, "local": str(local.relative_to(REPO_ROOT)),
             "sha256": digest, "bytes": size, "content_type": ctype}
            for key, local, digest, size, ctype in steps
        ]
        report["base_url"] = base_url

        if not args.apply:
            report["status"] = "dry-run"
            emit(report, args.json)
            return EXIT_OK

        report["uploaded"] = apply_plan(manifest, steps)
        report["status"] = "published"
        emit(report, args.json)
        return EXIT_OK
    except PublishError as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        if not args.json:
            print("发布失败：%s" % exc, file=sys.stderr)
        else:
            emit(report, True)
        return exc.code
    except Exception as exc:  # noqa: BLE001 - 任何未预期错误都收敛到 FAILED
        report["status"] = "failed"
        report["error"] = "%s: %s" % (type(exc).__name__, exc)
        if not args.json:
            print("发布失败：%s" % report["error"], file=sys.stderr)
        else:
            emit(report, True)
        return EXIT_FAILED


def emit(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print("状态：%s" % report.get("status"))
    if report.get("base_url"):
        print("根地址：%s/index.md" % report["base_url"])
    version = report.get("version_check") or {}
    if version:
        print(
            "版本：本地 %s / 线上 %s（%s）"
            % (version.get("local_version"), version.get("remote_version"),
               version.get("action"))
        )
    for step in report.get("upload_plan", []):
        print("  计划 %s (%d 字节, %s)" % (step["object"], step["bytes"], step["sha256"][:12]))
    for done in report.get("uploaded", []):
        print("  已上传 %s -> HTTP %s/%s" % (done["object"], done["put_status"], done["read_status"]))


if __name__ == "__main__":
    sys.exit(main())
