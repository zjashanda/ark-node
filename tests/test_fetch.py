"""``ark_node.fetch`` 的单元测试（unittest，标准库）。

全部测试都在本机完成：不连真实网络、不调用真实 ssh/scp。
需要“传输”的用例用 mock 直接替换 ``probe_node`` / ``remote_hash`` /
``scp_fetch``，只验证编排与校验逻辑。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ark_node import fetch, util

BEIJING_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
SECRET = "supersecretvalue-DO-NOT-LEAK"


class FetchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ark-fetch-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved_env = {}
        for key in (
            "ARK_NODE_HOME",
            "ARK_MESH_CONFIG",
            "ARK_MESH_CACHE",
            "SSHPASS",
            "ARK_TEST_PASS",
        ):
            self._saved_env[key] = os.environ.pop(key, None)
        os.environ["ARK_NODE_HOME"] = str(self.tmp / "home")
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _node(host: str = "node-a.example.com", port: int = 22, auth: str = "key", **extra) -> dict:
        node = {
            "host": host,
            "port": port,
            "user": "ark",
            "kind": "linux",
            "auth": auth,
        }
        node.update(extra)
        return node

    def _cfg(self, nodes: dict, artifacts: dict = None, name: str = "mesh.json") -> Path:
        path = self.tmp / name
        path.write_text(
            json.dumps(
                {"schema": fetch.SCHEMA, "nodes": nodes, "artifacts": artifacts or {}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _run(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fetch.main(argv)
        return rc, out.getvalue(), err.getvalue()

    # --------------------------------------------------------------- guard

    def test_forbidden_filenames_rejected_without_subprocess(self) -> None:
        cfg = self._cfg({"node-a": self._node()})
        dest = self.tmp / "out"
        bad_paths = [
            "/tmp/.env",
            "/tmp/secret/id_rsa",
            "/tmp/foo.env.bak",
            "/tmp/id_ed25519.bak",
            "/tmp/auth.json",
            "/tmp/git-credentials",
        ]
        for bad in bad_paths:
            with self.subTest(path=bad):
                with mock.patch("subprocess.run", side_effect=AssertionError("no subprocess")):
                    with self.assertRaises(SystemExit) as raised:
                        self._run(
                            [
                                "--config",
                                str(cfg),
                                "fetch",
                                "--source",
                                "node-a",
                                "--path",
                                bad,
                                "--dest",
                                str(dest),
                            ]
                        )
                self.assertEqual(raised.exception.code, util.EXIT_FORBIDDEN)

    def test_traversal_rejected(self) -> None:
        cfg = self._cfg({"node-a": self._node()})
        with mock.patch("subprocess.run", side_effect=AssertionError("no subprocess")):
            with self.assertRaises(SystemExit) as raised:
                self._run(
                    [
                        "--config",
                        str(cfg),
                        "fetch",
                        "--source",
                        "node-a",
                        "--path",
                        "/tmp/../etc/passwd",
                        "--dest",
                        str(self.tmp / "out"),
                    ]
                )
        self.assertEqual(raised.exception.code, util.EXIT_FORBIDDEN)

    # -------------------------------------------------------------- config

    def test_config_resolution_precedence(self) -> None:
        env_cfg = self._cfg({}, name="env.json")
        explicit = self._cfg({}, name="explicit.json")
        with mock.patch.dict(os.environ, {"ARK_MESH_CONFIG": str(env_cfg)}, clear=False):
            self.assertEqual(fetch.resolve_config_path(None), env_cfg)
            # 显式 --config 压过环境变量
            self.assertEqual(fetch.resolve_config_path(str(explicit)), explicit)
        os.environ.pop("ARK_MESH_CONFIG", None)
        self.assertEqual(
            fetch.resolve_config_path(None),
            Path(os.environ["ARK_NODE_HOME"]) / "mesh-sources.json",
        )

    def test_missing_config_is_usage_error(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            fetch.load_config(str(self.tmp / "nope.json"))
        self.assertEqual(raised.exception.code, util.EXIT_USAGE)

    # ------------------------------------------------------------ manifest

    def test_manifest_shape_and_beijing_time(self) -> None:
        dest = self.tmp / "dest"
        record = {
            "name": "sample/tool",
            "project": "sample",
            "version": "1.0.0",
            "source_node": "node-a",
            "source_path": "/srv/ark/artifacts/sample/tool.bin",
            "destination": str(dest / "tool.bin"),
            "bytes": 7,
            "sha256": "0" * 64,
            "fetched_at": util.now_display(),
        }
        path = fetch.write_manifest(dest, record, local=True)
        data = util.read_json(path)

        self.assertEqual(data["schema"], fetch.MANIFEST_SCHEMA)
        self.assertEqual(len(data["artifacts"]), 1)
        self.assertEqual(data["artifacts"][0]["sha256"], record["sha256"])
        self.assertRegex(data["updated_at"], BEIJING_RE)
        self.assertRegex(data["artifacts"][0]["fetched_at"], BEIJING_RE)

    # ---------------------------------------------------------- quarantine

    def test_sha256_mismatch_quarantines_and_returns_no_source(self) -> None:
        cfg = self._cfg({"node-a": self._node()})
        dest = self.tmp / "dest"
        expected = "b" * 64

        def fake_probe(*_args, **_kwargs):
            return True, "node-a", 0.01

        def fake_hash(*_args, **_kwargs):
            return expected, ""

        def fake_scp(_cfg, _name, _remote, local_dest, _timeout):
            local_dest.parent.mkdir(parents=True, exist_ok=True)
            local_dest.write_bytes(b"tampered-bytes")
            return True, ""

        with mock.patch.object(fetch, "probe_node", fake_probe), mock.patch.object(
            fetch, "remote_hash", fake_hash
        ), mock.patch.object(fetch, "scp_fetch", fake_scp):
            rc, _out, _err = self._run(
                [
                    "--config",
                    str(cfg),
                    "fetch",
                    "--source",
                    "node-a",
                    "--path",
                    "/srv/ark/artifacts/sample/tool.bin",
                    "--filename",
                    "tool.bin",
                    "--sha256",
                    expected,
                    "--dest",
                    str(dest),
                    "--timeout",
                    "1",
                ]
            )

        self.assertEqual(rc, util.EXIT_NO_SOURCE)
        qdir = Path(os.environ["ARK_NODE_HOME"]) / "quarantine"
        quarantined = qdir / "tool.bin.sha256-mismatch"
        self.assertTrue(quarantined.exists())
        self.assertEqual(quarantined.read_bytes(), b"tampered-bytes")
        sidecar = qdir / "tool.bin.sha256-mismatch.json"
        note = util.read_json(sidecar)
        self.assertEqual(note["expected_sha256"], expected)
        self.assertEqual(note["actual_sha256"], util.sha256_file(quarantined))
        self.assertRegex(note["detected_at"], BEIJING_RE)
        # 失败报告同样落盘，且目的目录没有被写入半成品
        self.assertTrue((dest / fetch.FAILURE_NAME).exists())
        self.assertFalse((dest / "tool.bin").exists())

    # ------------------------------------------------------- failure report

    def test_failure_report_lists_sources_and_never_leaks_secret(self) -> None:
        os.environ["ARK_TEST_PASS"] = SECRET
        cfg = self._cfg(
            {
                "node-a": self._node(auth="password", pass_env="ARK_TEST_PASS"),
                "node-b": self._node(
                    host="node-b.example.com", auth="password", pass_env="ARK_TEST_PASS"
                ),
            },
            {
                "sample/tool": {
                    "name": "sample/tool",
                    "project": "sample",
                    "version": "1.0.0",
                    "sha256": "c" * 64,
                    "locations": [
                        {"node": "node-a", "path": "/srv/ark/artifacts/sample/tool.bin", "priority": 1},
                        {"node": "node-b", "path": "/srv/ark/artifacts/sample/tool.bin", "priority": 2},
                    ],
                }
            },
        )
        dest = self.tmp / "dest"

        def fake_probe(*_args, **_kwargs):
            return False, "connection refused", 0.01

        with mock.patch.object(fetch, "probe_node", fake_probe):
            rc, out, err = self._run(
                [
                    "--config",
                    str(cfg),
                    "fetch",
                    "--name",
                    "sample/tool",
                    "--dest",
                    str(dest),
                    "--timeout",
                    "1",
                ]
            )

        self.assertEqual(rc, util.EXIT_NETWORK)  # 全部来源都是连通性失败
        report = util.read_json(dest / fetch.FAILURE_NAME)
        self.assertEqual(report["schema"], fetch.FAILURE_SCHEMA)
        self.assertEqual(report["verdict"], "BLOCKED")
        self.assertEqual(len(report["attempts"]), 2)
        self.assertEqual(
            {attempt["source_node"] for attempt in report["attempts"]}, {"node-a", "node-b"}
        )
        for attempt in report["attempts"]:
            self.assertIn("connection refused", attempt["error"])
        self.assertRegex(report["generated_at"], BEIJING_RE)

        blob = json.dumps(report, ensure_ascii=False) + out + err
        self.assertNotIn(SECRET, blob)

    # -------------------------------------------------------------- status

    def test_status_unreachable_node_returns_network_exit(self) -> None:
        cfg = self._cfg(
            {"local-unreachable": self._node(host="127.0.0.1", port=1, auth="key")}
        )
        rc, out, _err = self._run(
            ["--config", str(cfg), "status", "--json", "--timeout", "2"]
        )
        self.assertEqual(rc, util.EXIT_NETWORK)
        data = json.loads(out)
        self.assertRegex(data["checked_at"], BEIJING_RE)
        self.assertFalse(data["nodes"][0]["reachable"])


if __name__ == "__main__":
    unittest.main()
