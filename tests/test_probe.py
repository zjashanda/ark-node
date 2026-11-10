"""probe 单元测试（标准库 unittest）。

要点：能力 JSON 结构完整、缺工具不崩、**任何凭据值都不得出现在输出里**。
所有落盘路径通过 ``TemporaryDirectory`` + ``ARK_NODE_HOME`` / ``HOME`` 隔离。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ark_node import SCHEMA_CAPABILITY, probe, util

# 构造一个“像 token”的种子值用于泄漏检测；前缀用 join 拼装，避免字面量落进
# 源码/字节码，也避免被 grep 误判为真实凭据。
TOKEN_PREFIX = "".join(("g", "h", "p", "_"))
SECRET_VALUE = TOKEN_PREFIX + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "ark-home"
        self.userhome = self.root / "userhome"
        self.userhome.mkdir(parents=True)
        self.task_root = self.root / "tasks"

        env = {
            "ARK_NODE_HOME": str(self.home),
            "HOME": str(self.userhome),
            "ARK_TASK_ROOT": str(self.task_root),
            "GITHUB_TOKEN": SECRET_VALUE,
        }
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("ARK_ENV_VERSION", "ARK_NODE_ID", "ARK_NODE_ROLE"):
            os.environ.pop(key, None)

        for name, value in (("_dns_ok", True), ("_https_ok", True)):
            net = mock.patch(f"ark_node.probe.{name}", return_value=value)
            net.start()
            self.addCleanup(net.stop)

    def test_collect_has_required_shape_and_no_secret_value(self) -> None:
        payload = probe.collect()

        self.assertEqual(payload["schema"], SCHEMA_CAPABILITY)
        for key in (
            "ark_node_version",
            "generated_at",
            "node",
            "tools",
            "resources",
            "env",
            "task_root",
            "capabilities",
        ):
            self.assertIn(key, payload)

        self.assertEqual(
            set(payload["node"]), {"node_id", "hostname", "platform", "arch", "role"}
        )
        self.assertEqual(
            set(payload["tools"]),
            {"python", "git", "ssh", "scp", "curl", "tar", "powershell", "usb_tree"},
        )
        for name in ("python", "git", "ssh", "curl", "powershell"):
            self.assertEqual(set(payload["tools"][name]), {"present", "version"})
        for name in ("scp", "tar", "usb_tree"):
            self.assertEqual(set(payload["tools"][name]), {"present"})

        self.assertEqual(payload["capabilities"]["network"], {"dns": True, "https": True})

        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(SECRET_VALUE, text)
        self.assertNotIn(TOKEN_PREFIX, text)
        self.assertNotIn("GITHUB_TOKEN=", text)

    def test_collect_tolerates_missing_tools(self) -> None:
        with mock.patch("ark_node.probe.util.which", return_value=None):
            payload = probe.collect()

        for name in ("python", "git", "ssh", "curl", "powershell"):
            self.assertEqual(payload["tools"][name], {"present": False, "version": None})
        for name in ("scp", "tar", "usb_tree"):
            self.assertEqual(payload["tools"][name], {"present": False})

    def test_version_parsing_keeps_short_token(self) -> None:
        with mock.patch("ark_node.probe.util.which", return_value="/usr/bin/git"), mock.patch(
            "ark_node.probe.util.run_cmd",
            return_value=(0, "git version 2.43.0\nCopyright (C) ...", ""),
        ):
            payload = probe.collect()

        self.assertEqual(payload["tools"]["git"], {"present": True, "version": "2.43.0"})

    def test_env_reports_missing_key_names_only(self) -> None:
        repo = self.root / "repo"
        config = repo / "config"
        config.mkdir(parents=True)
        (config / "env-keys.json").write_text(
            json.dumps(["ARK_SAMPLE_KEY", "GITHUB_TOKEN", "ARK_SECOND_KEY"]),
            encoding="utf-8",
        )
        os.environ.pop("ARK_SAMPLE_KEY", None)
        os.environ.pop("ARK_SECOND_KEY", None)

        with mock.patch("ark_node.probe.util.repo_root", return_value=repo):
            payload = probe.collect()

        env = payload["env"]
        self.assertEqual(env["key_count"], 3)
        self.assertEqual(sorted(env["missing_keys"]), ["ARK_SAMPLE_KEY", "ARK_SECOND_KEY"])
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(SECRET_VALUE, text)

    def test_deep_mode_adds_extended_capabilities(self) -> None:
        with mock.patch("ark_node.probe.util.which", return_value=None):
            payload = probe.collect(deep=True)

        deep = payload["capabilities"]["deep"]
        self.assertEqual(
            set(deep),
            {"ssh_keys", "git_remotes", "listening_ports", "git_version", "git_email_set"},
        )
        self.assertEqual(deep["git_remotes"], [])
        self.assertIsInstance(deep["ssh_keys"], list)
        self.assertIsInstance(deep["listening_ports"], list)
        self.assertIsInstance(deep["git_email_set"], bool)

    def test_run_writes_out_file_and_emits_json(self) -> None:
        out = self.root / "nested" / "probe.json"
        args = argparse.Namespace(json=True, out=str(out), deep=False)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = probe.run(args)

        self.assertEqual(code, util.EXIT_OK)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["schema"], SCHEMA_CAPABILITY)
        self.assertTrue(out.is_file())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8")), payload)


if __name__ == "__main__":
    unittest.main()
