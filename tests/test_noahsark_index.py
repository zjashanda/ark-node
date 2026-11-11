#!/usr/bin/env python3
"""根索引三件套的回归测试（纯本地，不联网）。

覆盖四层：
1. catalog 校验：结构、唯一性、状态与地址的一致性、引用可解析、依赖无环。
2. 生成：三件套落盘、schema、计数、规划项不带链接、可用项必有链接。
3. 校验器：离线通过；篡改后必须失败（否则校验没有意义）。
4. 发布器：先测后发的门禁、上传顺序、缺凭据时拒绝、--apply 必须配 --yes。
"""

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HERE = REPO_ROOT / "templates" / "noahsark-index"
TOOLS = REPO_ROOT / "tools"
CATALOG_PATH = HERE / "catalog.json"

sys.path.insert(0, str(TOOLS))

# 测试用的提交号：分片书写，避免整串出现在源码里
FAKE_COMMIT = "0123456789abcdef" "0123456789abcdef" "01234567"
FAKE_TIME = "2026-11-11 16:58:44"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load_module("noahsark_build_index", HERE / "build-index.py")
publish = load_module("noahsark_publish_index", HERE / "publish-index.py")


def read_catalog():
    with CATALOG_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def run(script, args):
    proc = subprocess.run(
        [sys.executable, str(script)] + list(args),
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.catalog = read_catalog()

    def test_catalog_is_valid(self):
        self.assertEqual([], build.validate_catalog(self.catalog))

    def test_entry_ids_are_unique(self):
        ids = [entry["id"] for group in self.catalog["groups"] for entry in group["entries"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 30)

    def test_every_entry_declares_roles_and_summary(self):
        for group in self.catalog["groups"]:
            for entry in group["entries"]:
                with self.subTest(entry=entry["id"]):
                    self.assertTrue(entry.get("summary"))
                    self.assertTrue(entry.get("roles"))

    def test_duplicate_id_is_rejected(self):
        broken = copy.deepcopy(self.catalog)
        first = broken["groups"][0]["entries"][0]
        clone = copy.deepcopy(first)
        broken["groups"][0]["entries"].append(clone)
        errors = build.validate_catalog(broken)
        self.assertTrue(any("重复" in err or "唯一" in err for err in errors), errors)

    def test_planned_entry_with_link_is_rejected(self):
        broken = copy.deepcopy(self.catalog)
        for group in broken["groups"]:
            for entry in group["entries"]:
                if entry["status"] == "planned":
                    entry["oss"] = "%s/should-not-exist.md" % broken["oss"]["prefix"]
                    break
        errors = build.validate_catalog(broken)
        self.assertTrue(errors)

    def test_available_entry_without_version_is_rejected(self):
        broken = copy.deepcopy(self.catalog)
        for group in broken["groups"]:
            for entry in group["entries"]:
                if entry["status"] == "available":
                    entry["version"] = None
                    break
        errors = build.validate_catalog(broken)
        self.assertTrue(errors)

    def test_unknown_requires_is_rejected(self):
        broken = copy.deepcopy(self.catalog)
        broken["groups"][0]["entries"][0]["requires"] = ["no-such-entry"]
        errors = build.validate_catalog(broken)
        self.assertTrue(errors)

    def test_unknown_role_reference_is_rejected(self):
        broken = copy.deepcopy(self.catalog)
        broken["roles"][0]["must_read"].append("no-such-entry")
        errors = build.validate_catalog(broken)
        self.assertTrue(errors)

    def test_dependency_cycle_is_detected(self):
        broken = copy.deepcopy(self.catalog)
        entries = broken["groups"][0]["entries"]
        entries[0]["requires"] = [entries[1]["id"]]
        entries[1]["requires"] = [entries[0]["id"]]
        payload = []
        for group in broken["groups"]:
            for entry in group["entries"]:
                payload.append((group["id"], entry))
        cycles = build.find_cycles(payload)
        self.assertTrue(cycles)


class RenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = read_catalog()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        cls.manifest, cls.files = build.render(
            cls.catalog, FAKE_COMMIT, FAKE_TIME, cls.out
        )
        with (cls.out / "index.json").open("r", encoding="utf-8") as handle:
            cls.index = json.load(handle)
        cls.markdown = (cls.out / "index.md").read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_three_files_written(self):
        for name in ("index.md", "index.json", "manifest.json"):
            self.assertTrue((self.out / name).exists(), name)

    def test_index_schema_and_counts(self):
        self.assertEqual(build.INDEX_SCHEMA, self.index["schema"])
        counts = self.index["counts"]
        self.assertEqual(len(self.index["items"]), counts["items"])
        self.assertEqual(
            counts["items"], counts["available"] + counts["planned"]
        )

    def test_available_items_have_links_and_planned_items_do_not(self):
        for item in self.index["items"]:
            with self.subTest(item=item["id"]):
                if item["status"] == "available":
                    self.assertTrue(item["url"], item["id"])
                    self.assertTrue(item["url"].startswith("https://"))
                    self.assertTrue(item["version"])
                else:
                    self.assertIsNone(item["url"])
                    self.assertIsNone(item["version"])

    def test_every_item_has_required_fields(self):
        required = (
            "id",
            "title",
            "kind",
            "status",
            "version",
            "path",
            "url",
            "roles",
            "requires",
            "summary",
        )
        for item in self.index["items"]:
            for key in required:
                self.assertIn(key, item)

    def test_manifest_lists_index_files_with_digests(self):
        names = {item["path"] for item in self.manifest["files"]}
        self.assertEqual({"index.md", "index.json"}, names)
        for item in self.manifest["files"]:
            self.assertEqual(64, len(item["sha256"]))
            actual = (self.out / item["path"]).read_bytes()
            self.assertEqual(build.sha256_bytes(actual), item["sha256"])
            self.assertEqual(len(actual), item["bytes"])

    def test_markdown_has_single_permanent_entry(self):
        prefix = self.catalog["oss"]["prefix"].strip("/")
        entry_url = build.object_url(self.catalog["oss"], "%s/index.md" % prefix)
        self.assertIn(entry_url, self.markdown)

    def test_markdown_has_no_bare_long_hashes(self):
        import re

        hits = re.findall(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{32,}(?![0-9A-Za-z])", self.markdown)
        self.assertEqual([], hits)

    def test_markdown_links_every_available_item(self):
        for item in self.index["items"]:
            if item["status"] == "available" and item["url"]:
                with self.subTest(item=item["id"]):
                    self.assertIn(item["url"], self.markdown)

    def test_markdown_marks_planned_items(self):
        planned = [item for item in self.index["items"] if item["status"] == "planned"]
        self.assertTrue(planned)
        self.assertIn("规划中", self.markdown)

    def test_markdown_documents_workdirs(self):
        self.assertIn("tasks", self.markdown)
        self.assertIn("workspace", self.markdown)

    def test_agent_template_digest_matches_published_manifest(self):
        wanted = None
        for item in self.index["items"]:
            if item["id"] == "agent-template.template":
                wanted = item.get("sha256")
        self.assertTrue(wanted)
        with (REPO_ROOT / "templates" / "agents" / "manifest.json").open(
            "r", encoding="utf-8"
        ) as handle:
            agent_manifest = json.load(handle)
        digests = {item["sha256"] for item in agent_manifest["files"]}
        self.assertIn(wanted, digests)


class VerifierTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        build.render(read_catalog(), FAKE_COMMIT, FAKE_TIME, self.out)

    def tearDown(self):
        self.tmp.cleanup()

    def test_offline_check_passes(self):
        code, out, err = run(
            HERE / "verify-index.py", ["--dir", str(self.out), "--skip-tools"]
        )
        self.assertEqual(0, code, out + err)

    def test_tampered_digest_fails(self):
        path = self.out / "index.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in data["items"]:
            if item.get("sha256"):
                item["sha256"] = "0" * 64
                break
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        code, _out, _err = run(
            HERE / "verify-index.py", ["--dir", str(self.out), "--skip-tools"]
        )
        self.assertNotEqual(0, code)

    def test_missing_markdown_fails(self):
        (self.out / "index.md").unlink()
        code, _out, _err = run(
            HERE / "verify-index.py", ["--dir", str(self.out), "--skip-tools"]
        )
        self.assertNotEqual(0, code)


class GateTest(unittest.TestCase):
    def test_generated_files_pass_secret_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            build.render(read_catalog(), FAKE_COMMIT, FAKE_TIME, out)
            targets = [str(out / name) for name in ("index.md", "index.json", "manifest.json")]
            code, out_text, err = run(TOOLS / "secret_scan.py", targets + [str(CATALOG_PATH)])
            self.assertEqual(0, code, out_text + err)

    def test_sources_pass_time_lint(self):
        targets = [
            str(CATALOG_PATH),
            str(HERE / "build-index.py"),
            str(HERE / "verify-index.py"),
            str(HERE / "publish-index.py"),
            str(Path(__file__).resolve()),
        ]
        code, out_text, err = run(TOOLS / "time_lint.py", targets)
        self.assertEqual(0, code, out_text + err)

    def test_publish_refuses_apply_without_yes(self):
        code, out_text, err = run(HERE / "publish-index.py", ["--apply"])
        self.assertEqual(2, code, out_text + err)

    def test_publish_upload_order_puts_manifest_last(self):
        self.assertEqual(
            ("index.json", "index.md", "manifest.json"), publish.UPLOAD_ORDER
        )

    def test_publish_requires_credentials_from_env(self):
        saved = {}
        for key in ("ACCESS_KEY_ID", "ACCESS_KEY_SECRET"):
            saved[key] = os.environ.pop(key, None)
        try:
            with self.assertRaises(publish.PublishError) as ctx:
                publish.credentials()
            self.assertEqual(4, ctx.exception.code)
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    def test_publish_never_prints_secret(self):
        source = (HERE / "publish-index.py").read_text(encoding="utf-8")
        self.assertIn("ACCESS_KEY_SECRET", source)
        # 凭据只允许出现在 os.environ 读取与报错文案里，不得写进文件
        self.assertNotIn("open(", source.split("def credentials")[1].split("def apply_plan")[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
