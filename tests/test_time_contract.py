"""时间契约测试（标准库 unittest）。

覆盖两部分：``ark_node.util`` 的展示 / 文件名 / 解析三种时间形态，
以及 ``tools/time_lint.py`` 对违规与合法内容的判定。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ark_node import util

REPO_ROOT = Path(__file__).resolve().parent.parent
LINTER = REPO_ROOT / "tools" / "time_lint.py"

DISPLAY_PATTERN = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
STAMP_PATTERN = r"^\d{8}-\d{6}$"


class UtilTimeTests(unittest.TestCase):
    def test_now_display_is_naive_beijing_shape(self) -> None:
        value = util.now_display()
        self.assertRegex(value, DISPLAY_PATTERN)
        self.assertNotIn("T", value)
        self.assertNotIn("Z", value)
        _, _, clock = value.partition(" ")
        self.assertNotIn("+", clock)
        self.assertNotIn("-", clock)

    def test_now_stamp_shape(self) -> None:
        self.assertRegex(util.now_stamp(), STAMP_PATTERN)

    def test_parse_display_round_trip(self) -> None:
        text = "2026-11-10 19:04:31"
        self.assertEqual(util.parse_display(text).strftime("%Y-%m-%d %H:%M:%S"), text)


class LinterFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def lint(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(LINTER), *extra, str(self.root)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def assert_rule_flag(self, rule: str) -> None:
        proc = self.lint()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(rule, proc.stdout)

    def test_detects_iso_t_separator(self) -> None:
        self.write("iso-t.md", 'started_at = 2026-11-10T19:04:31\n')  # noqa: time-lint
        self.assert_rule_flag("iso-t-separator")

    def test_detects_iso_z_suffix(self) -> None:
        self.write("iso-z.json", '{"updated_at": "2026-11-10 19:04:31Z"}\n')  # noqa: time-lint
        self.assert_rule_flag("iso-z-suffix")

    def test_detects_utc_offset(self) -> None:
        self.write("offset.ini", "created_at = 2026-11-10 19:04:31+0800\n")  # noqa: time-lint
        self.assert_rule_flag("utc-offset")

    def test_detects_time_recommendation(self) -> None:
        self.write("guide.md", "guide: emit RFC3339 strings for machine fields\n")  # noqa: time-lint
        self.assert_rule_flag("rfc3339-mention")  # noqa: time-lint

    def test_detects_doc_placeholder(self) -> None:
        self.write("table.md", "| 时间 | YYYY-MM-DDThh:mm:ss |\n")  # noqa: time-lint
        self.assert_rule_flag("doc-placeholder")

    def test_detects_stamp_in_field(self) -> None:
        self.write("stamp.json", '{\n  "checked_at": "20261110-190431"\n}\n')  # noqa: time-lint
        self.assert_rule_flag("stamp-in-field")

    def test_json_report_shape(self) -> None:
        self.write("bad.md", 'x = 2026-11-10T19:04:31\n')  # noqa: time-lint
        proc = self.lint("--json")
        payload = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(payload["checked"], 1)
        self.assertEqual(payload["violations_count"], len(payload["violations"]))
        self.assertEqual(payload["violations"][0]["rule"], "iso-t-separator")

    def test_accepts_legitimate_content(self) -> None:
        self.write("20261110-190431/README.md", "archive directory\n")
        self.write(
            "good.json",
            "{\n"
            '  "checked_at": "2026-11-10 19:04:31",\n'
            '  "raw_started_at": "2026-11-10T20:54:13.225",\n'  # noqa: time-lint
            '  "note": "20261110-190431 is a directory name"\n'
            "}\n",
        )
        proc = self.lint()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_noqa_marker_suppresses_violation(self) -> None:
        self.write("noqa.md", "bad = 2026-11-10T19:04:31  # noqa: time-lint\n")
        proc = self.lint()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class RepoWideTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("ARK_TIME_LINT_STRICT"),
        "set ARK_TIME_LINT_STRICT=1 to enforce repo-wide cleanliness",
    )
    def test_repo_root_is_clean(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(LINTER), str(REPO_ROOT)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        offending = "\n".join(proc.stdout.splitlines()[:40])
        self.assertEqual(proc.returncode, 0, "time-lint violations:\n" + offending)


if __name__ == "__main__":
    unittest.main()
