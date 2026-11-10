"""identity / initspace 单元测试（标准库 unittest）。

所有落盘路径都通过 ``tempfile.TemporaryDirectory()`` + ``ARK_NODE_HOME``
隔离，测试不会碰真实用户目录。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ark_node import identity, initspace, util


class IdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.home = root / "ark-home"
        self.work = root / "work"

        patcher = mock.patch.dict(
            os.environ,
            {"ARK_NODE_HOME": str(self.home), "HOME": str(root / "userhome")},
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("ARK_NODE_ID", "ARK_NODE_ROLE"):
            os.environ.pop(key, None)

    def test_node_id_precedence_arg_beats_env_beats_file(self) -> None:
        util.atomic_write_json(
            self.home / "node.json",
            {"schema": identity.SCHEMA_IDENTITY, "node_id": "from-file"},
        )

        result = identity.resolve()
        self.assertEqual((result["node_id"], result["source"]), ("from-file", "file"))

        os.environ["ARK_NODE_ID"] = "from-env"
        result = identity.resolve()
        self.assertEqual((result["node_id"], result["source"]), ("from-env", "env"))

        result = identity.resolve("from-arg")
        self.assertEqual((result["node_id"], result["source"]), ("from-arg", "arg"))

    def test_hostname_derivation_lowercases_and_strips_domain(self) -> None:
        with mock.patch("socket.gethostname", return_value="Demo-Host.Example.COM"):
            result = identity.resolve()

        self.assertEqual(result["node_id"], "demo-host")
        self.assertEqual(result["source"], "hostname")
        self.assertEqual(result["hostname"], "Demo-Host.Example.COM")
        self.assertIn(result["platform"], {"linux", "windows", "macos"})
        self.assertEqual(result["role"], "node")

    def test_node_json_persisted_with_mode_600(self) -> None:
        os.environ["ARK_NODE_ID"] = "persisted-node"
        identity.resolve()

        state = self.home / "node.json"
        self.assertTrue(state.is_file())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)

        data = util.read_json(state)
        self.assertEqual(data["schema"], identity.SCHEMA_IDENTITY)
        self.assertEqual(data["node_id"], "persisted-node")
        self.assertEqual(
            set(data), {"schema", "node_id", "hostname", "platform", "role", "created_at"}
        )
        blob = json.dumps(data).lower()
        for word in ("token", "password", "secret", "credential"):
            self.assertNotIn(word, blob)

    def test_role_from_env_and_root_override(self) -> None:
        os.environ["ARK_NODE_ROLE"] = "worker"
        result = identity.resolve(root=str(self.work))
        self.assertEqual(result["role"], "worker")
        self.assertEqual(result["root"], str(self.work))

    def test_run_json_mode_emits_identity(self) -> None:
        args = argparse.Namespace(node_id="cli-node", root=str(self.work), json=True)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = identity.run(args)

        self.assertEqual(code, util.EXIT_OK)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["schema"], identity.SCHEMA_IDENTITY)
        self.assertEqual(payload["node_id"], "cli-node")

    def test_run_text_mode_prints_three_lines(self) -> None:
        args = argparse.Namespace(node_id="cli-node", root=None, json=False)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = identity.run(args)

        self.assertEqual(code, util.EXIT_OK)
        lines = buffer.getvalue().strip().splitlines()
        self.assertEqual(lines[0], "node_id: cli-node")
        self.assertTrue(lines[1].startswith("hostname: "))
        self.assertTrue(lines[2].startswith("platform: "))


class InitSpaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.home = root / "ark-home"
        self.base = root / "tasks"
        self.userhome = root / "userhome"
        self.userhome.mkdir(parents=True)

        patcher = mock.patch.dict(
            os.environ,
            {
                "ARK_NODE_HOME": str(self.home),
                "HOME": str(self.userhome),
                "ARK_TASK_ROOT": str(self.base),
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("ARK_NODE_ID", None)

    @staticmethod
    def _run(**overrides: object) -> tuple:
        values = {"task": None, "base_dir": None, "dry_run": False, "json": True}
        values.update(overrides)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = initspace.run(argparse.Namespace(**values))
        return code, buffer.getvalue()

    def test_init_creates_expected_tree(self) -> None:
        code, output = self._run(task="Demo Task")
        self.assertEqual(code, util.EXIT_OK)
        payload = json.loads(output)
        self.assertFalse(payload["reused"])

        task_dir = Path(payload["task_dir"])
        self.assertTrue(task_dir.is_dir())
        self.assertRegex(task_dir.name, r"^\d{8}-\d{6}-demo-task$")
        for sub in ("logs", "artifacts", "tmp"):
            self.assertTrue((task_dir / sub).is_dir())
        for sub in initspace.RUNTIME_SUBDIRS:
            self.assertTrue((self.home / sub).is_dir())

        meta = task_dir / ".ark-task.json"
        self.assertTrue(meta.is_file())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(meta.stat().st_mode), 0o600)
        data = util.read_json(meta)
        self.assertEqual(data["schema"], initspace.SCHEMA_TASK)
        self.assertEqual(data["slug"], "demo-task")
        self.assertTrue(data["node_id"])
        self.assertTrue(data["created_at"])

    def test_init_reuse_is_idempotent(self) -> None:
        code, first_output = self._run(task="demo")
        self.assertEqual(code, util.EXIT_OK)
        first = json.loads(first_output)
        first_meta = util.read_json(Path(first["task_dir"]) / ".ark-task.json")

        code, second_output = self._run(task="demo")
        self.assertEqual(code, util.EXIT_OK)
        second = json.loads(second_output)

        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["task_dir"], second["task_dir"])
        self.assertEqual(
            util.read_json(Path(second["task_dir"]) / ".ark-task.json"), first_meta
        )

        task_dirs = [p for p in self.base.iterdir() if p.is_dir()]
        self.assertEqual(len(task_dirs), 1)

    def test_init_dry_run_creates_nothing(self) -> None:
        code, output = self._run(task="demo", dry_run=True)
        self.assertEqual(code, util.EXIT_OK)

        payload = json.loads(output)
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["reused"])
        self.assertTrue(payload["task_dir"].startswith(str(self.base)))
        self.assertTrue(payload["task_dir"].endswith("-demo"))

        self.assertFalse(self.home.exists())
        self.assertFalse(self.base.exists())

    def test_init_without_task_only_creates_runtime_dirs(self) -> None:
        code, output = self._run(task=None)
        self.assertEqual(code, util.EXIT_OK)
        payload = json.loads(output)
        self.assertIsNone(payload["task_dir"])
        self.assertTrue((self.home / "cache").is_dir())
        self.assertFalse(self.base.exists())

    def test_init_invalid_slug_is_usage_error(self) -> None:
        code, _output = self._run(task="***")
        self.assertEqual(code, util.EXIT_USAGE)
        self.assertFalse(self.home.exists())

    def test_init_reports_os_error_without_traceback(self) -> None:
        blocker = Path(self._tmp.name) / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        code, _output = self._run(task="demo", base_dir=str(blocker / "tasks"))
        self.assertEqual(code, util.EXIT_USAGE)

    def test_sanitize_slug_truncates_to_40(self) -> None:
        slug = initspace.sanitize_slug("A" * 60)
        self.assertEqual(len(slug), 40)
        self.assertEqual(slug, "a" * 40)
        self.assertRegex(initspace.sanitize_slug("Build / Task #1"), re.compile(r"^[a-z0-9._-]+$"))


if __name__ == "__main__":
    unittest.main()
