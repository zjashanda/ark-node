from __future__ import annotations

import ipaddress
import json
import logging
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen


HOST = os.environ.get("NOAHSARK_UI_HOST", "0.0.0.0")
PORT = int(os.environ.get("NOAHSARK_UI_PORT", "8092"))
API_BASE = os.environ.get("NOAHSARK_API_BASE", "http://127.0.0.1:8091").rstrip("/")
SQLITE = os.environ.get(
    "NOAHSARK_KB_SQLITE", "/home/bszheng/NoahsArk-data/knowledge_service/kb.sqlite"
)
ALLOWED_NETS = tuple(
    ipaddress.ip_network(raw)
    for raw in os.environ.get(
        "NOAHSARK_UI_NETS",
        "10.11.63.0/24,10.210.8.0/24,10.11.198.0/23,10.11.193.0/24,127.0.0.0/8",
    ).split(",")
)
INDEX = Path(__file__).with_name("index.html")
LOG = logging.getLogger("noahsark-knowledge-ui")


def allowed(remote: str) -> bool:
    try:
        address = ipaddress.ip_address(remote)
    except ValueError:
        return False
    return any(address in network for network in ALLOWED_NETS)


def api_get(path: str, query: str = "") -> tuple[int, bytes, str]:
    target = f"{API_BASE}{path}"
    if query:
        target += f"?{query}"
    request = Request(target, method="GET", headers={"Accept": "application/json"})
    with urlopen(request, timeout=12) as response:
        return response.status, response.read(), response.headers.get_content_type()


def doc_detail(doc_id: str) -> dict | None:
    path = doc_id.split("#", 1)[0]
    if not path:
        return None
    con = sqlite3.connect(f"file:{SQLITE}?mode=ro", uri=True, timeout=8)
    con.row_factory = sqlite3.Row
    try:
        doc = con.execute(
            "SELECT path, kind, scope, status, project, wiki_entity, char_count "
            "FROM documents WHERE path = ?",
            (path,),
        ).fetchone()
        rows = con.execute(
            "SELECT id, heading, line_start, line_end, text "
            "FROM chunks WHERE document_path = ? ORDER BY line_start, id",
            (path,),
        ).fetchall()
    finally:
        con.close()
    if doc is None and not rows:
        return None
    return {
        "doc_id": path,
        "document": dict(doc) if doc is not None else {"path": path},
        "chunks": [dict(row) for row in rows],
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "noahsark-knowledge-ui"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:
        LOG.info("%s %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        if not allowed(self.client_address[0]):
            self.send_error(403, "LAN only")
            return
        split = urlsplit(self.path)
        if split.path in ("/", "/index.html"):
            self.send_bytes(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            return
        if split.path == "/favicon.ico":
            self.send_bytes(204, b"", "image/x-icon")
            return
        if split.path == "/api/healthz":
            self.proxy("/healthz", split.query)
            return
        if split.path == "/api/search":
            self.proxy("/v1/search", split.query)
            return
        if split.path.startswith("/api/entry/"):
            entry_id = unquote(split.path.removeprefix("/api/entry/"))
            if not entry_id:
                self.send_error(400, "invalid entry id")
                return
            if entry_id.startswith("kb-"):
                self.proxy(f"/v1/entries/{quote(entry_id, safe='-')}", "")
            else:
                self.send_doc(entry_id)
            return
        if split.path.startswith("/api/doc/"):
            self.send_doc(unquote(split.path.removeprefix("/api/doc/")))
            return
        self.send_error(404, "not found")

    def send_doc(self, doc_id: str) -> None:
        try:
            payload = doc_detail(doc_id)
        except Exception as exc:
            LOG.warning("sqlite doc lookup failed id=%s error=%s", doc_id, type(exc).__name__)
            self.send_json(503, {"error": "doc_unavailable", "message": "文档读取失败"})
            return
        if payload is None:
            self.send_json(404, {"error": "document_not_found", "message": "文档不存在"})
            return
        self.send_json(200, payload)

    def proxy(self, path: str, query: str) -> None:
        try:
            status, body, content_type = api_get(path, query)
        except Exception as exc:
            LOG.warning("API proxy failed path=%s error=%s", path, type(exc).__name__)
            self.send_json(502, {"error": "knowledge_api_unavailable", "message": "知识服务暂时不可用"})
            return
        self.send_bytes(status, body, f"{content_type}; charset=utf-8")

    def send_json(self, status: int, payload: dict) -> None:
        self.send_bytes(status, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    LOG.info("listening on %s:%s api=%s sqlite=%s", HOST, PORT, API_BASE, SQLITE)
    server.serve_forever()


if __name__ == "__main__":
    main()
