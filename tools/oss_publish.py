#!/usr/bin/env python3
"""阿里云 OSS 最小客户端：零第三方依赖，签名 V1。

用途：给 NoahsArk 的发布脚本（模板发布、根索引发布）提供同一份上传/回读实现，
避免每个发布器各写一套签名逻辑。凭据只从调用方传入（调用方从环境变量读），
本模块不读任何文件、不打印任何密钥。

用法::

    from oss_publish import normalize_endpoint, object_url, oss_put, anon_get

    endpoint = normalize_endpoint("https://oss-cn-beijing.aliyuncs.com")
    result = oss_put(bucket, endpoint, key, data, ak, sk, "public-read")

设计约束
--------
* 只做 PUT 与匿名 GET 两种动作，不做列举、删除、改 ACL —— 发布器不该有删除能力。
* 返回值是 ``(status, body)``，把 HTTP 结果交回调用方判断，不用异常表达业务失败。
* 网络异常统一转成 ``status=0``，由调用方映射到自己的退出码（5 = NETWORK）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import email.utils
import urllib.error
import urllib.request

DEFAULT_CONTENT_TYPE = "application/octet-stream"
ALLOWED_VISIBILITY = ("public-read", "private")
TIMEOUT = 60
USER_AGENT = "ark-oss-publish/1.0"


def normalize_endpoint(endpoint: str) -> str:
    """去掉协议前缀与尾部斜杠，只留 ``host[:port]``。"""
    host = (endpoint or "").strip()
    for prefix in ("https://", "http://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix):]
    return host.rstrip("/")


def object_url(bucket: str, endpoint: str, key: str) -> str:
    """拼出对象的公网 URL（不参与签名，仅供展示与匿名回读）。"""
    return "https://{bucket}.{host}/{key}".format(
        bucket=bucket, host=normalize_endpoint(endpoint), key=key.lstrip("/")
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sign(secret: str, string_to_sign: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(digest.digest()).decode("ascii")


def oss_put(
    bucket: str,
    endpoint: str,
    key: str,
    data: bytes,
    access_key_id: str,
    access_key_secret: str,
    visibility: str = "public-read",
    content_type: str = DEFAULT_CONTENT_TYPE,
    timeout: int = TIMEOUT,
):
    """上传一个对象，返回 ``(status, body_text)``；网络故障返回 ``(0, 原因)``。

    ``visibility`` 通过 ``x-oss-object-acl`` 请求头逐对象设置，必须落在
    ``public-read`` / ``private`` 内，其余值直接返回 ``(2, ...)``。
    """
    if visibility not in ALLOWED_VISIBILITY:
        return 2, "visibility 只允许 {allowed}，收到 {got}".format(
            allowed="/".join(ALLOWED_VISIBILITY), got=visibility
        )
    if not access_key_id or not access_key_secret:
        return 4, "缺少 ACCESS_KEY_ID / ACCESS_KEY_SECRET（本模块不读文件）"

    host = normalize_endpoint(endpoint)
    clean_key = key.lstrip("/")
    date = email.utils.formatdate(usegmt=True)
    content_md5 = base64.b64encode(hashlib.md5(data).digest()).decode("ascii")
    canonical_header = "x-oss-object-acl:" + visibility
    resource = "/{bucket}/{key}".format(bucket=bucket, key=clean_key)
    string_to_sign = "\n".join(
        ["PUT", content_md5, content_type, date, canonical_header, resource]
    )
    signature = _sign(access_key_secret, string_to_sign)

    url = object_url(bucket, host, clean_key)
    request = urllib.request.Request(url, data=data, method="PUT")
    request.add_header("Date", date)
    request.add_header("Content-Type", content_type)
    request.add_header("Content-MD5", content_md5)
    request.add_header("Content-Length", str(len(data)))
    request.add_header("x-oss-object-acl", visibility)
    request.add_header("User-Agent", USER_AGENT)
    request.add_header(
        "Authorization", "OSS {ak}:{sig}".format(ak=access_key_id, sig=signature)
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc)


def anon_get(url: str, timeout: int = TIMEOUT):
    """匿名 GET，返回 ``(status, body_bytes)``；网络故障返回 ``(0, b"")``。"""
    request = urllib.request.Request(url, method="GET")
    request.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError):
        return 0, b""


def anon_status(url: str, timeout: int = TIMEOUT) -> int:
    """只取状态码，用于轻量链接检查。"""
    status, _ = anon_get(url, timeout=timeout)
    return status
