"""``ark_node.envsync`` 单元测试（仅标准库 unittest）。

覆盖：键过滤、dry-run 不落盘、只改变化键并保留其他键、备份、bashrc 钩子幂等、
状态文件不含值、缺口令 / 401 / 离线 / Linux 机器级作用域等退出码路径。
HTTP 用本机 ``ThreadingHTTPServer`` 假服务，不访问外网。
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ark_node import envsync, util  # noqa: E402

VERSION = "2026-11-09T10:00:00+08:00"
USER = "ark"
PASSWORD = "test-pass"
UNRELATED_VALUE = "keep-me"

ENV_PAYLOAD = {
    "__version": "v-env-1",
    "GITHUB_TOKEN": "value-new",
    "ACCESS_KEY_ID": "id-new",
    "EMPTY_KEY": "",
    "SPACES_KEY": "   ",
}


def _make_server(password: str, payload: dict, version: str):
    """起一个只认 Basic 口令的假 ark-env 服务，返回 (server, base_url)。"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: D102 - 静音默认访问日志
            pass

        def _send(self, code: int, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
            if self.path == "/health":
                self._send(200, b'{"ok":true}')
                return

            expected = "Basic " + base64.b64encode(f"{USER}:{password}".encode("utf-8")).decode("ascii")
            if self.headers.get("Authorization", "") != expected:
                self._send(401, b'{"error":"unauthorized"}')
                return

            if self.path == "/ark/version.json":
                self._send(200, json.dumps({"version": version}).encode("utf-8"))
            elif self.path == "/ark/env.json":
                self._send(200, json.dumps(payload).encode("utf-8"))
            else:
                self._send(404, b'{"error":"not found"}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _args(**overrides):
    values = {
        "base": None,
        "user": USER,
        "pass_stdin": False,
        "auth_file": None,
        "scope": "user",
        "diff": False,
        "apply": False,
        "offline": None,
        "timeout": 2.0,
        "json": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class EnvSyncTest(unittest.TestCase):
    """共享临时家目录 + 假服务。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home" / "ark"  # 即 ARK_NODE_HOME
        patcher = mock.patch.dict(
            os.environ,
            {
                "ARK_NODE_HOME": str(self.home),
                "ARK_ENV_BASE_URL": "",
                "ARK_HTTP_PASS": "",
                "ARK_HTTP_USER": "",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---------------------------------------------------------------- helpers

    def start_server(self, password: str = PASSWORD, payload: dict = None, version: str = VERSION) -> str:
        server, base = _make_server(password, payload if payload is not None else dict(ENV_PAYLOAD), version)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return base

    def run_env(self, **overrides):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = envsync.run(_args(**overrides))
        return code, out.getvalue(), err.getvalue()

    def env_file(self) -> Path:
        return self.home / "env"

    def write_env_file(self, text: str) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.env_file().write_text(text, encoding="utf-8")

    def read_state(self) -> dict:
        return json.loads((self.home / "env-state.json").read_text(encoding="utf-8"))

    # -------------------------------------------------------------- 键过滤

    def test_key_filtering(self):
        version, updates, skipped = envsync._parse_remote_env(dict(ENV_PAYLOAD))

        self.assertEqual(version, "v-env-1")
        self.assertEqual([key for key, _ in updates], ["GITHUB_TOKEN", "ACCESS_KEY_ID"])
        reasons = {item["key"]: item["reason"] for item in skipped}
        self.assertEqual(reasons["__version"], "internal_prefix")
        self.assertEqual(reasons["EMPTY_KEY"], "empty_value")
        self.assertEqual(reasons["SPACES_KEY"], "empty_value")

        _, filtered, more = envsync._parse_remote_env({"NUMBER_KEY": 5, "__x": "1"})
        self.assertEqual(filtered, [])
        self.assertEqual({item["key"]: item["reason"] for item in more}["NUMBER_KEY"], "not_a_string")

    # --------------------------------------------------------------- 入口

    def test_missing_base_returns_usage(self):
        code, _out, err = self.run_env()

        self.assertEqual(code, util.EXIT_USAGE)
        self.assertIn("--base", err)

    def test_missing_password_returns_usage(self):
        base = self.start_server()

        code, _out, err = self.run_env(base=base)

        self.assertEqual(code, util.EXIT_USAGE)
        self.assertIn("口令", err)
        self.assertFalse((self.home / "auth").exists())
        self.assertFalse((self.home / "env-state.json").exists())

    def test_unauthorized_returns_forbidden(self):
        base = self.start_server()

        code, _out, err = self.run_env(base=base)
        self.assertEqual(code, util.EXIT_USAGE)

        with mock.patch.dict(os.environ, {"ARK_HTTP_PASS": "wrong-pass"}):
            code, _out, err = self.run_env(base=base)
        self.assertEqual(code, util.EXIT_FORBIDDEN)
        self.assertIn("认证失败", err)
        self.assertNotIn("wrong-pass", err)

    def test_health_probe_picks_first_reachable_candidate(self):
        base = self.start_server()
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "env-sources.json").write_text(
            json.dumps(
                {
                    "schema": "ark-env-sources.v1",
                    "candidates": ["http://127.0.0.1:1", base],
                }
            ),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"ARK_HTTP_PASS": PASSWORD}):
            code, out, _err = self.run_env()

        self.assertEqual(code, util.EXIT_OK)
        self.assertEqual(self.read_state()["base"], base)

    def test_pass_stdin_is_persisted_but_never_printed(self):
        base = self.start_server()

        with mock.patch.object(sys, "stdin", io.StringIO(PASSWORD + "\n")):
            code, out, err = self.run_env(base=base, pass_stdin=True, diff=True)

        self.assertEqual(code, util.EXIT_OK)
        auth = self.home / "auth"
        self.assertEqual(auth.read_text(encoding="utf-8"), PASSWORD)
        self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(PASSWORD, out + err)

    # ---------------------------------------------------------- dry-run

    def test_dry_run_does_not_write(self):
        base = self.start_server()
        original = "# managed by hand\nKEEP=1\nGITHUB_TOKEN=value-old\n"
        self.write_env_file(original)

        with mock.patch.dict(os.environ, {"ARK_HTTP_PASS": PASSWORD}):
            code, out, _err = self.run_env(base=base, diff=True, json=False)

        self.assertEqual(code, util.EXIT_OK)
        self.assertEqual(self.env_file().read_text(encoding="utf-8"), original)
        self.assertEqual(list(self.home.glob("env-backup.*.bak")), [])
        self.assertIn("would set: GITHUB_TOKEN", out)
        self.assertIn("would set: ACCESS_KEY_ID", out)
        self.assertNotIn("would set: KEEP", out)
        self.assertNotIn("value-new", out)

        state = self.read_state()
        self.assertEqual(state["mode"], "diff")
        self.assertEqual(set(state["changed_keys"]), {"GITHUB_TOKEN", "ACCESS_KEY_ID"})
        self.assertEqual(state["local_version_before"], "")

    def test_diff_wins_over_apply(self):
        base = self.start_server()

        with mock.patch.dict(os.environ, {"ARK_HTTP_PASS": PASSWORD}):
            code, out, _err = self.run_env(base=base, diff=True, apply=True, json=False)

        self.assertEqual(code, util.EXIT_OK)
        self.assertFalse(self.env_file().exists())
        self.assertIn("would set: GITHUB_TOKEN", out)
        self.assertEqual(self.read_state()["mode"], "diff")

    # ------------------------------------------------------------- apply

    def test_apply_writes_only_changed_keys_and_preserves_others(self):
        base = self.start_server()
        original = "# managed by hand\nKEEP={}\nGITHUB_TOKEN=value-old\n".format(UNRELATED_VALUE)
        self.write_env_file(original)

        with mock.patch.dict(os.environ, {"ARK_HTTP_PASS": PASSWORD}):
            code, out, _err = self.run_env(base=base, apply=True)

        self.assertEqual(code, util.EXIT_OK)
        text = self.env_file().read_text(encoding="utf-8")
        self.assertIn("# managed by hand", text)
        self.assertIn(f"KEEP={UNRELATED_VALUE}", text)
        self.assertIn("GITHUB_TOKEN=value-new", text)
        self.assertIn("ACCESS_KEY_ID=id-new", text)
        self.assertIn(f"ARK_ENV_VERSION={VERSION}", text)
        self.assertNotIn("EMPTY_KEY", text)
        self.assertNotIn("SPACES_KEY", text)
        self.assertNotIn("value-old", text)
        # 无关键保持原顺序（在 GITHUB_TOKEN 之前）
        self.assertLess(text.index("KEEP="), text.index("GITHUB_TOKEN="))
        self.assertEqual(self.env_file().stat().st_mode & 0o777, 0o600)

        state = self.read_state()
        self.assertEqual(state["mode"], "apply")
        self.assertEqual(state["remote_version"], VERSION)
        self.assertEqual(set(state["changed_keys"]), {"GITHUB_TOKEN", "ACCESS_KEY_ID"})
        self.assertEqual(state["target_file"], str(self.env_file()))

    def test_apply_creates_backup_of_previous_content(self):
        base = self.start_server()
        original = "GITHUB_TOKEN=value-old\n"
        self.write_env_file(original)

        with mock.patch.dict(os.environ, {"ARK_HTTP_PASS": PASSWORD}):
            code, _out, _err = self.run_env(base=base, apply=True)

        self.assertEqual(code, util.EXIT_OK)
        backups = list(self.home.glob("env-backup.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_apply_without_previous_file_creates_no_backup(self):
        base = self.start_server()

        with mock.patch.dict(os.environ, {"ARK_HTTP_PASS": PASSWORD}):
            code, _out, _err = self.run_env(base=base, apply=True)

        self.assertEqual(code, util.EXIT_OK)
        self.assertTrue(self.env_file().exists())
        self.assertEqual(list(self.home.glob("env-backup.*.bak")), [])

    def test_bashrc_hook_is_appended_once(self):
        base = self.start_server()
        rc = self.home.parent / ".bashrc"
        rc.parent.mkdir(parents=True, exist_ok=True)
        rc.write_text("# existing rc\n", encoding="utf-8")

        with mock.patch.dict(os.environ, {"ARK_HTTP_PASS": PASSWORD}):
            first, _out, _err = self.run_env(base=base, apply=True)
            second, _out2, _err2 = self.run_env(base=base, apply=True)

        self.assertEqual((first, second), (util.EXIT_OK, util.EXIT_OK))
        lines = [line.strip() for line in rc.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lines.count(envsync.USER_HOOK_LINE), 1)
        self.assertIn("# existing rc", lines)

    def test_state_file_never_contains_values(self):
        base = self.start_server()

        with mock.patch.dict(os.environ, {"ARK_HTTP_PASS": PASSWORD}):
            code, _out, _err = self.run_env(base=base, apply=True)

        self.assertEqual(code, util.EXIT_OK)
        raw = (self.home / "env-state.json").read_text(encoding="utf-8")
        self.assertNotIn("value-new", raw)
        self.assertNotIn("id-new", raw)
        self.assertNotIn(PASSWORD, raw)
        state = json.loads(raw)
        self.assertEqual(state["schema"], "ark-node-env-state.v1")
        self.assertEqual(state["scope"], "user")
        self.assertIn("applied_at", state)

    # ----------------------------------------------------------- offline

    def test_offline_apply(self):
        snapshot = self.root / "snapshot.json"
        snapshot.write_text(
            json.dumps(
                {
                    "__version": "v-offline-1",
                    "GITHUB_TOKEN": "value-offline",
                    "EMPTY_KEY": "",
                }
            ),
            encoding="utf-8",
        )

        code, _out, err = self.run_env(offline=str(snapshot), apply=True)

        self.assertEqual(code, util.EXIT_OK, err)
        text = self.env_file().read_text(encoding="utf-8")
        self.assertIn("GITHUB_TOKEN=value-offline", text)
        self.assertIn("ARK_ENV_VERSION=v-offline-1", text)
        state = self.read_state()
        self.assertEqual(state["base"], "offline")
        self.assertEqual(state["changed_keys"], ["GITHUB_TOKEN"])

    def test_offline_missing_snapshot_is_clean_error(self):
        code, _out, err = self.run_env(offline=str(self.root / "nope.json"))

        self.assertIn(code, (util.EXIT_USAGE, util.EXIT_NETWORK))
        self.assertIn("离线快照", err)
        self.assertNotIn("Traceback", err)

    # ----------------------------------------------------------- machine

    @unittest.skipIf(util.is_windows(), "machine 作用域在 Windows 上需要管理员，Linux 才走拒绝分支")
    def test_machine_scope_on_linux_is_usage_error(self):
        snapshot = self.root / "snapshot.json"
        snapshot.write_text(json.dumps({"__version": "v"}), encoding="utf-8")

        code, _out, err = self.run_env(scope="machine", offline=str(snapshot), apply=True)

        self.assertEqual(code, util.EXIT_USAGE)
        self.assertIn("Windows", err)
        self.assertFalse(self.env_file().exists())

    # ------------------------------------------------------------ 输出

    def test_human_output_ends_with_summary(self):
        base = self.start_server()

        with mock.patch.dict(os.environ, {"ARK_HTTP_PASS": PASSWORD}):
            code, out, _err = self.run_env(base=base, diff=True, json=False)

        self.assertEqual(code, util.EXIT_OK)
        lines = [line for line in out.splitlines() if line]
        self.assertEqual(lines[-1], f"remote version={VERSION} local= changed=2")
        self.assertIn("would set: GITHUB_TOKEN", lines)

    def test_apply_output_uses_set_verb(self):
        base = self.start_server()

        with mock.patch.dict(os.environ, {"ARK_HTTP_PASS": PASSWORD}):
            code, out, _err = self.run_env(base=base, apply=True, json=False)

        self.assertEqual(code, util.EXIT_OK)
        self.assertIn("set: GITHUB_TOKEN", out)
        self.assertNotIn("would set:", out)
        self.assertNotIn("value-new", out)


if __name__ == "__main__":
    unittest.main()
