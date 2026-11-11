#!/usr/bin/env python3
"""仓库凭据扫描器：防止 token / 私钥 / 内网地址被误提交。

用法::

    python3 tools/secret_scan.py <path> [<path> ...]

对每个文件（或目录递归）逐行扫描，命中即打印 ``path:line: rule``；
有任何命中返回 1，全部干净返回 0，参数缺失/路径不存在返回 2。

规则
----
* GitHub token（经典与 fine-grained PAT）
* 云厂商 AccessKey（阿里云 / AWS）
* PEM 私钥块
* URL 内嵌 ``user:password@host``
* 长度 ≥32 且高熵的 base64 / hex 串（``sha256`` 摘要与 ``commit`` 提交号除外，它们是标识符）
* RFC1918 私网 IP 字面量（位于 ``*.example.*`` 占位域名的行除外）

约定
----
某些行本身需要写出“像密钥”的字面量（例如本文件的检测用正则）。这类行可加
``# noqa: secret-scan`` 标记，扫描器会整行跳过。扫描器必须能干净地扫描它自己。
"""

from __future__ import annotations

import math
import os
import re
import sys
from pathlib import Path
from typing import Iterable, List, Pattern, Tuple

NOQA_MARKER = "noqa: secret-scan"

# --------------------------------------------------------------------- 规则

GITHUB_TOKEN = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")  # noqa: secret-scan
GITHUB_PAT = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")  # noqa: secret-scan
ALIYUN_AK = re.compile(r"\bLTAI[0-9A-Za-z]{12,}")  # noqa: secret-scan
AWS_AK = re.compile(r"\bAKIA[0-9A-Z]{12,}")  # noqa: secret-scan
PRIVATE_KEY = re.compile("-----BEGIN" + r"[ \t]+[A-Z ]*PRIVATE KEY-----")  # noqa: secret-scan
URL_CREDENTIAL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^/\s:@]+:[^/\s@]+@")  # noqa: secret-scan
HEX_BLOB = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{32,}(?![0-9A-Za-z])")  # noqa: secret-scan
B64_BLOB = re.compile(r"(?<![0-9A-Za-z+/=])[A-Za-z0-9+/]{32,}={0,2}(?![0-9A-Za-z+/=])")  # noqa: secret-scan
# 内容摘要（sha256）与 Git 提交号都是标识符，不是凭据：同行内出现即放行。
DIGEST_FIELD = re.compile(
    r'"(?:content_|evidence_)?sha256"\s*:\s*"[0-9A-Fa-f]{64}"'
    r'|"commit"\s*:\s*"[0-9A-Fa-f]{40}"'
)

PRIVATE_IP = re.compile(
    r"\b(?:"
    r"10\.(?:\d{1,3}\.){2}\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.(?:\d{1,3}\.)\d{1,3}"
    r"|192\.168\.(?:\d{1,3}\.)\d{1,3}"
    r")\b"
)  # noqa: secret-scan

SIMPLE_RULES: List[Tuple[str, Pattern[str]]] = [
    ("github-token", GITHUB_TOKEN),
    ("github-pat", GITHUB_PAT),
    ("aliyun-ak", ALIYUN_AK),
    ("aws-ak", AWS_AK),
    ("private-key", PRIVATE_KEY),
    ("url-credentials", URL_CREDENTIAL),
]

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache", ".pytest_cache"}
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".tar", ".tgz", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib", ".pyc",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3",
}


def _entropy(text: str) -> float:
    counts = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    total = len(text)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def _alternating_alpha_digit(token: str) -> bool:
    """像 ``A1b2C3...`` 这类人工拼接的占位串不是随机凭据。"""
    if len(token) % 2:
        return False

    def _pair(start: int) -> bool:
        return all(
            token[index].isalpha() and token[index + 1].isdigit()
            for index in range(start, len(token) - 1, 2)
        )

    return _pair(0) or _pair(1)


def _high_entropy(token: str) -> bool:
    """随机 base64/hex 的特征：足够长、字符多样、含数字与字母、熵足够高。"""
    if len(token) < 32:
        return False
    if len(set(token)) < 12:
        return False
    if not any(char.isdigit() for char in token):
        return False
    if not any(char.isalpha() for char in token):
        return False
    if _alternating_alpha_digit(token):
        return False
    return _entropy(token) >= 3.5


def _line_hits(line: str) -> List[str]:
    # 摘要先掩掉，避免 sha256 被当成高熵密钥串
    line = DIGEST_FIELD.sub('"sha256": "<digest>"', line)
    hits: List[str] = []
    for name, pattern in SIMPLE_RULES:
        if pattern.search(line):
            hits.append(name)
    for token in HEX_BLOB.findall(line):
        if _high_entropy(token):
            hits.append("high-entropy-hex")
            break
    if "high-entropy-hex" not in hits:
        for token in B64_BLOB.findall(line):
            if _high_entropy(token):
                hits.append("high-entropy-base64")
                break
    if ".example." not in line and PRIVATE_IP.search(line):
        hits.append("rfc1918-ip")
    return hits


def _iter_files(paths: Iterable[str]) -> Iterable[Tuple[str, Path]]:
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            for root, dirs, files in os.walk(str(path)):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
                for name in sorted(files):
                    candidate = Path(root) / name
                    if candidate.suffix.lower() in SKIP_SUFFIXES:
                        continue
                    yield str(candidate), candidate
        else:
            yield raw, path


def scan_text(display: str, text: str) -> List[str]:
    findings: List[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        if NOQA_MARKER in line:
            continue
        for rule in _line_hits(line):
            findings.append(f"{display}:{number}: {rule}")
    return findings


def main(argv: List[str]) -> int:
    if not argv:
        sys.stderr.write("usage: python3 tools/secret_scan.py <path> [<path> ...]\n")
        return 2
    for raw in argv:
        if not Path(raw).exists():
            sys.stderr.write(f"secret_scan: 路径不存在: {raw}\n")
            return 2

    findings: List[str] = []
    for display, path in _iter_files(argv):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            sys.stderr.write(f"secret_scan: 无法读取 {display}: {exc}\n")
            return 2
        if b"\x00" in raw:
            continue
        findings.extend(scan_text(display, raw.decode("utf-8", errors="replace")))

    for line in findings:
        print(line)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
