#!/usr/bin/env python3
"""根索引三件套、服务契约与初始化契约的回归测试（纯本地，不联网）。

覆盖五层：
1. catalog 校验：两区域结构、唯一性、状态与地址一致性、引用可解析、依赖无环、
   服务契约漂移、onboarding 步骤与阻塞码闭合。
2. 生成：三件套落盘、schema、计数、区域划分、规划项不带链接、可用项必有链接。
3. 校验器：离线通过；篡改（摘要、区域标题、分组位置、服务地址、缺失步骤）必须失败。
4. 服务门禁：离线漂移检查、回执校验、token 文件权限。
5. 发布器：先测后发的门禁、上传顺序、缺凭据时拒绝、--apply 必须配 --yes。
"""

import copy
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HERE = REPO_ROOT / "templates" / "noahsark-index"
TOOLS = REPO_ROOT / "tools"
CATALOG_PATH = HERE / "catalog.json"
SERVICES = HERE / "services"
STORAGE_DESCRIPTOR = SERVICES / "storage-service.json"
KB_DESCRIPTOR = SERVICES / "knowledge-service.json"

sys.path.insert(0, str(TOOLS))

FAKE_COMMIT = "0123456789abcdef" "0123456789abcdef" "01234567"
FAKE_TIME = "2026-11-11 16:58:44"
REGION_TITLES = ("一、NoahsArk 体系介绍", "二、体系环境初始化")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load_module("noahsark_build_index", HERE / "build-index.py")
publish = load_module("noahsark_publish_index", HERE / "publish-index.py")
services = load_module("noahsark_check_services", HERE / "check-services.py")


def read_catalog():
    with CATALOG_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_descriptor(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def run(script, args, cwd=None):
    proc = subprocess.run(
        [sys.executable, str(script)] + list(args),
        cwd=str(cwd or REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def all_entries(catalog):
    for group in catalog["groups"]:
        for entry in group["entries"]:
            yield group, entry


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.catalog = read_catalog()

    def test_catalog_is_valid(self):
        self.assertEqual([], build.validate_catalog(self.catalog))

    def test_two_regions_in_fixed_order(self):
        self.assertEqual(
            ["system", "onboarding"], [r["id"] for r in self.catalog["regions"]]
        )
        for group in self.catalog["groups"]:
            self.assertIn(group["region"], ("system", "onboarding"))

    def test_entry_ids_are_unique(self):
        ids = [entry["id"] for _group, entry in all_entries(self.catalog)]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 60)

    def test_every_entry_declares_roles_and_summary(self):
        for _group, entry in all_entries(self.catalog):
            with self.subTest(entry=entry["id"]):
                self.assertTrue(entry.get("summary"))
                self.assertTrue(entry.get("roles"))

    def test_services_cover_storage_and_knowledge(self):
        ids = {s["id"] for s in self.catalog["services"]}
        self.assertEqual({"svc.storage-service", "svc.knowledge-service"}, ids)
        for svc in self.catalog["services"]:
            descriptor = read_descriptor(REPO_ROOT / svc["descriptor"])
            self.assertEqual(svc["id"], descriptor["id"])
            canonical = descriptor["discovery"]["canonical"]
            if canonical.get("entry_kind") == "fixed-ip":
                self.assertIn(descriptor["endpoint"], build.APPROVED_INTERNAL_ENDPOINTS)
            else:
                self.assertNotRegex(descriptor["endpoint"], services.RFC1918)

    def test_onboarding_has_steps_acceptance_and_block_codes(self):
        onboarding = self.catalog["onboarding"]
        self.assertGreaterEqual(len(onboarding["steps"]), 8)
        self.assertEqual(
            {"auto", "confirm", "admin"}, set(onboarding["automation_levels"])
        )
        codes = {c["code"] for c in onboarding["block_codes"]}
        self.assertIn("BLOCKED_STORAGE_QUARANTINE", codes)
        self.assertIn("BLOCKED_KB_STALE", codes)
        accepted = {a["id"] for a in onboarding["acceptance"]}
        for step in onboarding["steps"]:
            for item in step["acceptance"]:
                self.assertIn(item, accepted)
            for code in step["block_codes"]:
                self.assertIn(code, codes)

    def test_duplicate_id_is_rejected(self):
        broken = copy.deepcopy(self.catalog)
        first = broken["groups"][0]["entries"][0]
        broken["groups"][0]["entries"].append(copy.deepcopy(first))
        errors = build.validate_catalog(broken)
        self.assertTrue(any("重复" in err or "唯一" in err for err in errors), errors)

    def test_planned_entry_with_link_is_rejected(self):
        broken = copy.deepcopy(self.catalog)
        for _group, entry in all_entries(broken):
            if entry["status"] == "planned":
                entry["oss"] = "%s/should-not-exist.md" % broken["oss"]["prefix"]
                break
        self.assertTrue(build.validate_catalog(broken))

    def test_available_entry_without_version_is_rejected(self):
        broken = copy.deepcopy(self.catalog)
        for _group, entry in all_entries(broken):
            if entry["status"] == "available":
                entry["version"] = None
                break
        self.assertTrue(build.validate_catalog(broken))

    def test_group_without_region_is_rejected(self):
        broken = copy.deepcopy(self.catalog)
        broken["groups"][0]["region"] = "misc"
        errors = build.validate_catalog(broken)
        self.assertTrue(any("region" in err for err in errors), errors)

    def test_service_version_drift_is_rejected(self):
        broken = copy.deepcopy(self.catalog)
        for _group, entry in all_entries(broken):
            if entry["kind"] == "service":
                entry["version"] = "9.9.9"
                break
        errors = build.validate_catalog(broken)
        self.assertTrue(any("版本" in err for err in errors), errors)

    def test_service_endpoint_must_be_resolvable_node_name(self):
        broken = copy.deepcopy(self.catalog)
        target = None
        for _group, entry in all_entries(broken):
            if entry["kind"] == "service":
                target = entry
                break
        descriptor = read_descriptor(REPO_ROOT / target["descriptor"])
        descriptor["endpoint"] = "http://10.0.0.9:%s" % descriptor["host"]["port"]  # noqa: secret-scan
        descriptor["host"]["name"] = "10.0.0.9"  # noqa: secret-scan
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(descriptor, handle, ensure_ascii=False)
            tmp = handle.name
        try:
            target["descriptor"] = os.path.relpath(tmp, str(REPO_ROOT))
            errors = build.validate_catalog(broken)
            self.assertTrue(any("私网" in err for err in errors), errors)
        finally:
            os.unlink(tmp)

    def test_onboarding_step_with_unknown_block_code_is_rejected(self):
        broken = copy.deepcopy(self.catalog)
        broken["onboarding"]["steps"][0]["block_codes"].append("BLOCKED_NOPE")
        errors = build.validate_catalog(broken)
        self.assertTrue(any("BLOCKED_NOPE" in err for err in errors), errors)

    def test_onboarding_missing_automation_level_is_rejected(self):
        broken = copy.deepcopy(self.catalog)
        del broken["onboarding"]["automation_levels"]["admin"]
        errors = build.validate_catalog(broken)
        self.assertTrue(any("automation_levels" in err for err in errors), errors)

    def test_unknown_requires_is_rejected(self):
        broken = copy.deepcopy(self.catalog)
        broken["groups"][0]["entries"][0]["requires"] = ["no-such-entry"]
        self.assertTrue(build.validate_catalog(broken))

    def test_unknown_role_reference_is_rejected(self):
        broken = copy.deepcopy(self.catalog)
        broken["roles"][0]["must_read"].append("no-such-entry")
        self.assertTrue(build.validate_catalog(broken))

    def test_dependency_cycle_is_detected(self):
        broken = copy.deepcopy(self.catalog)
        entries = broken["groups"][0]["entries"]
        entries[0]["requires"] = [entries[1]["id"]]
        entries[1]["requires"] = [entries[0]["id"]]
        payload = [(group["id"], entry) for group, entry in all_entries(broken)]
        self.assertTrue(build.find_cycles(payload))


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
        self.assertEqual(counts["items"], counts["available"] + counts["planned"])
        self.assertEqual(2, counts["regions"])
        self.assertEqual(2, counts["services"])
        self.assertEqual(len(self.catalog["onboarding"]["steps"]), counts["onboarding_steps"])

    def test_navigation_has_two_domains(self):
        self.assertEqual(["system", "onboarding"], [n["id"] for n in self.index["navigation"]])
        self.assertIn("system", self.index)
        self.assertIn("onboarding", self.index)
        self.assertTrue(self.index["system"]["services"])
        self.assertTrue(self.index["onboarding"]["steps"])

    def test_region_items_match_group_membership(self):
        for region_id in ("system", "onboarding"):
            block = self.index[region_id]
            group_items = [iid for g in block["groups"] for iid in g["items"]]
            self.assertEqual(sorted(group_items), sorted(r["id"] for r in block["items"]))
            expected = {i["id"] for i in self.index["items"] if i["region"] == region_id}
            self.assertEqual(expected, {r["id"] for r in block["items"]})

    def test_available_items_have_links_and_planned_items_do_not(self):
        for item in self.index["items"]:
            with self.subTest(item=item["id"]):
                if item["status"] == "available":
                    if item["kind"] == "service" and item.get("entry_kind") == "fixed-ip":
                        self.assertIsNone(item.get("url"))
                        self.assertNotIn("endpoint", item)
                        self.assertTrue(item.get("endpoint_ref"))
                    else:
                        self.assertTrue(item["url"], item["id"])
                        if item["kind"] == "service":
                            self.assertTrue(item["url"].startswith("http"))
                        else:
                            self.assertTrue(item["url"].startswith("https://"))
                    self.assertTrue(item["version"])
                else:
                    self.assertIsNone(item["url"])
                    self.assertIsNone(item["version"])

    def test_every_item_has_required_fields(self):
        required = (
            "id",
            "region",
            "title",
            "kind",
            "status",
            "version",
            "path",
            "url",
            "sha256",
            "source_repo",
            "source_commit",
            "roles",
            "requires",
            "summary",
        )
        for item in self.index["items"]:
            for key in required:
                self.assertIn(key, item)

    def test_service_blocks_match_descriptors(self):
        for block in self.index["system"]["services"]:
            descriptor = read_descriptor(REPO_ROOT / block["descriptor"])
            self.assertEqual(descriptor["version"], block["version"])
            if block["entry_kind"] == "fixed-ip":
                self.assertNotIn("endpoint", block)
                self.assertNotIn("endpoint", block["discovery"]["canonical"])
                self.assertEqual(
                    "descriptor:%s#discovery.canonical.fixed_entry" % block["descriptor"],
                    block["endpoint_ref"],
                )
            else:
                self.assertEqual(descriptor["endpoint"], block["endpoint"])
                self.assertNotRegex(block["endpoint"], services.RFC1918)
            self.assertTrue(block["auth"]["scopes"])
            self.assertTrue(block["verification"]["confirmed"])

    def test_manifest_lists_index_files_with_digests(self):
        names = {item["path"] for item in self.manifest["files"]}
        self.assertEqual({"index.md", "index.json"}, names)
        for item in self.manifest["files"]:
            self.assertEqual(64, len(item["sha256"]))
            actual = (self.out / item["path"]).read_bytes()
            self.assertEqual(build.sha256_bytes(actual), item["sha256"])
            self.assertEqual(len(actual), item["bytes"])
        self.assertEqual(len(self.index["system"]["services"]), len(self.manifest["services"]))
        self.assertEqual(2, len(self.manifest["regions"]))

    def test_markdown_has_exactly_two_regions(self):
        headings = re.findall(r"^# (.+)$", self.markdown, re.M)
        self.assertEqual(list(REGION_TITLES), headings)

    def test_markdown_places_groups_in_their_region(self):
        first, _, second = self.markdown.partition("# 二、")
        for group in self.index["groups"]:
            marker = "### %s" % group["label"]
            if group["region"] == "system":
                self.assertIn(marker, first)
                self.assertNotIn(marker, second)
            else:
                self.assertIn(marker, second)
                self.assertNotIn(marker, first)

    def test_markdown_covers_every_item_and_step(self):
        for item in self.index["items"]:
            self.assertIn(item["id"], self.markdown)
        _first, _, second = self.markdown.partition("# 二、")
        for step in self.index["onboarding"]["steps"]:
            self.assertIn(step["id"], second)
        for code in self.index["onboarding"]["block_codes"]:
            self.assertIn(code["code"], self.markdown)

    def test_markdown_has_single_permanent_entry(self):
        prefix = self.catalog["oss"]["prefix"].strip("/")
        entry_url = build.object_url(self.catalog["oss"], "%s/index.md" % prefix)
        self.assertIn(entry_url, self.markdown)

    def test_markdown_has_no_bare_long_hashes(self):
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

    def test_no_legacy_entry_in_generated_artifacts(self):
        legacy = "http://bszT14:" + "8091"
        blobs = [
            self.markdown,
            (self.out / "index.json").read_text(encoding="utf-8"),
            (self.out / "manifest.json").read_text(encoding="utf-8"),
        ]
        for blob in blobs:
            self.assertNotIn(legacy, blob)

    def test_markdown_documents_workdirs(self):
        self.assertIn("tasks", self.markdown)
        self.assertIn("workspace", self.markdown)

    def test_agent_template_digest_matches_published_manifest(self):
        wanted = None
        for item in self.index["items"]:
            if item["id"] == "agent-template.template":
                wanted = item.get("sha256")
                self.assertEqual("1.7.0", item.get("version"))
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

    def verify(self):
        return run(HERE / "verify-index.py", ["--dir", str(self.out), "--skip-tools"])

    def rewrite_index(self, mutate):
        path = self.out / "index.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_offline_check_passes(self):
        code, out, err = self.verify()
        self.assertEqual(0, code, out + err)

    def test_tampered_digest_fails(self):
        self.rewrite_index(lambda data: data["items"][0].__setitem__("sha256", "0" * 64))
        code, _out, _err = self.verify()
        self.assertNotEqual(0, code)

    def test_missing_markdown_fails(self):
        (self.out / "index.md").unlink()
        code, _out, _err = self.verify()
        self.assertNotEqual(0, code)

    def test_missing_region_domain_fails(self):
        self.rewrite_index(lambda data: data.pop("onboarding"))
        code, out, _err = self.verify()
        self.assertNotEqual(0, code)
        self.assertIn("onboarding", out)

    def test_region_group_mismatch_fails(self):
        def mutate(data):
            data["system"]["groups"] = data["system"]["groups"][1:]

        self.rewrite_index(mutate)
        code, _out, _err = self.verify()
        self.assertNotEqual(0, code)

    def test_service_endpoint_drift_fails(self):
        def mutate(data):
            data["system"]["services"][0]["endpoint"] = "http://linux003:9999"

        self.rewrite_index(mutate)
        code, _out, _err = self.verify()
        self.assertNotEqual(0, code)

    def test_private_endpoint_fails(self):
        def mutate(data):
            data["system"]["services"][0]["endpoint"] = "http://10.0.0.7:8091"  # noqa: secret-scan

        self.rewrite_index(mutate)
        code, out, _err = self.verify()
        self.assertNotEqual(0, code)
        self.assertIn("私网", out)


class ServiceGateTest(unittest.TestCase):
    def test_offline_drift_check_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            build.render(read_catalog(), FAKE_COMMIT, FAKE_TIME, out)
            code, stdout, stderr = run(
                HERE / "check-services.py", ["--dir", str(out)]
            )
            self.assertEqual(0, code, stdout + stderr)

    def test_receipt_is_accepted_and_drift_is_rejected(self):
        receipt = {
            "schema": "ark.service-probe.v1",
            "service": "svc.knowledge-service",
            "endpoint": "http://10.11.63.42:8091",
            "contract_version": read_descriptor(KB_DESCRIPTOR)["version"],
            "ok": True,
            "checks": [{"id": "health", "http": 200, "ok": True}],
            "note": "fixture",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipt.json"
            path.write_text(json.dumps(receipt), encoding="utf-8")
            code, stdout, stderr = run(HERE / "check-services.py", ["--receipt", str(path)])
            self.assertEqual(0, code, stdout + stderr)

            receipt["endpoint"] = "http://bszT14:9999"
            path.write_text(json.dumps(receipt), encoding="utf-8")
            code, _stdout, _stderr = run(HERE / "check-services.py", ["--receipt", str(path)])
            self.assertNotEqual(0, code)

    def test_token_file_must_be_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token"
            path.write_text("not-a-real-token", encoding="utf-8")
            os.chmod(str(path), 0o644)
            with self.assertRaises(services.CheckError) as ctx:
                services.read_token(str(path))
            self.assertEqual(4, ctx.exception.code)
            os.chmod(str(path), 0o600)
            self.assertEqual("not-a-real-token", services.read_token(str(path)))

    def test_receipt_never_contains_authorization(self):
        source = (HERE / "check-services.py").read_text(encoding="utf-8")
        self.assertIn('"token": "<redacted>"', source)
        self.assertNotIn("print(token", source)


class GateTest(unittest.TestCase):
    def test_generated_files_pass_secret_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            build.render(read_catalog(), FAKE_COMMIT, FAKE_TIME, out)
            targets = [str(out / name) for name in ("index.md", "index.json", "manifest.json")]
            targets.append(str(CATALOG_PATH))
            code, out_text, err = run(TOOLS / "secret_scan.py", targets)
            self.assertEqual(0, code, out_text + err)

    def test_descriptors_only_hold_approved_internal_literals(self):
        for path in (STORAGE_DESCRIPTOR, KB_DESCRIPTOR):
            data = read_descriptor(path)
            canonical = (data.get("discovery") or {}).get("canonical") or {}
            fixed_ip_paths = (
                ("endpoint", "discovery.canonical.endpoint")
                if canonical.get("entry_kind") == "fixed-ip"
                else ()
            )
            for json_path, ip in build.iter_internal_literals(data):
                with self.subTest(descriptor=path.name, path=json_path):
                    self.assertTrue(
                        json_path.endswith(build.INTERNAL_PATH_SUFFIXES)
                        or json_path in fixed_ip_paths
                    )
                    self.assertIn(ip, build.APPROVED_INTERNAL_HOSTS)

    def test_no_legacy_entry_in_repo_sources(self):
        legacy = "http://bszT14:" + "8091"
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(0, proc.returncode, proc.stderr.decode("utf-8", "replace"))
        skip_prefixes = ("result/", "receipts/", "logs/")
        skip_exact = {"docs/migration-20261118-knowledge-entry.md"}
        hits = []
        for rel in proc.stdout.decode("utf-8").splitlines():
            if rel in skip_exact or rel.startswith(skip_prefixes):
                continue
            path = REPO_ROOT / rel
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if legacy in text:
                hits.append(rel)
        self.assertEqual([], hits)

    def test_sources_pass_time_lint(self):
        targets = [
            str(CATALOG_PATH),
            str(STORAGE_DESCRIPTOR),
            str(KB_DESCRIPTOR),
            str(HERE / "build-index.py"),
            str(HERE / "verify-index.py"),
            str(HERE / "publish-index.py"),
            str(HERE / "check-services.py"),
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
        self.assertNotIn("open(", source.split("def credentials")[1].split("def apply_plan")[0])


class KnowledgeChainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = read_catalog()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        build.render(cls.catalog, FAKE_COMMIT, FAKE_TIME, cls.out)
        cls.index = json.loads((cls.out / "index.json").read_text(encoding="utf-8"))
        cls.markdown = (cls.out / "index.md").read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _validate_temp_tree(self, mutate):
        root = Path(tempfile.mkdtemp())
        try:
            (root / "templates" / "noahsark-index" / "services").mkdir(parents=True)
            catalog = copy.deepcopy(self.catalog)
            descriptors = {
                "templates/noahsark-index/services/storage-service.json": read_descriptor(
                    STORAGE_DESCRIPTOR
                ),
                "templates/noahsark-index/services/knowledge-service.json": read_descriptor(
                    KB_DESCRIPTOR
                ),
            }
            mutate(catalog, descriptors)
            (root / "templates/noahsark-index/catalog.json").write_text(
                json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
            )
            for rel, data in descriptors.items():
                (root / rel).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            saved = build.REPO_ROOT
            build.REPO_ROOT = root
            try:
                return build.validate_catalog(catalog)
            finally:
                build.REPO_ROOT = saved
        finally:
            shutil.rmtree(str(root), ignore_errors=True)

    def test_versions_match_contract(self):
        self.assertEqual("2.1.1", self.catalog["version"])
        self.assertEqual("1.1.0", read_descriptor(STORAGE_DESCRIPTOR)["version"])
        self.assertEqual("1.1.1", read_descriptor(KB_DESCRIPTOR)["version"])

    def test_knowledge_canonical_uses_fixed_entry(self):
        canonical = read_descriptor(KB_DESCRIPTOR)["discovery"]["canonical"]
        self.assertEqual("http://10.11.63.42:8091", canonical["endpoint"])
        self.assertEqual("fixed-ip", canonical["entry_kind"])

    def test_generated_service_uses_endpoint_ref(self):
        block = next(
            s for s in self.index["system"]["services"] if s["id"] == "svc.knowledge-service"
        )
        self.assertEqual("fixed-ip", block["entry_kind"])
        self.assertIn("endpoint_ref", block)
        self.assertNotIn("endpoint", block)
        blob = json.dumps(block, ensure_ascii=False)
        self.assertNotIn("10.11.63.42", blob)
        self.assertNotIn("bszT14:" + "8091", blob)

    def test_descriptors_have_all_three_contracts(self):
        for path in (STORAGE_DESCRIPTOR, KB_DESCRIPTOR):
            descriptor = read_descriptor(path)
            with self.subTest(descriptor=path.name):
                self.assertEqual(
                    "ark.service-discovery.v1", descriptor["discovery"]["schema"]
                )
                self.assertEqual(
                    "ark.token-bootstrap.v1", descriptor["token_bootstrap"]["schema"]
                )
                self.assertEqual(
                    "ark.service-health.v1", descriptor["health_contract"]["schema"]
                )

    def test_pvm1003_uses_measured_storage_endpoint(self):
        descriptor = read_descriptor(STORAGE_DESCRIPTOR)
        discovery = descriptor["discovery"]
        profiles = {p["node"]: p for p in discovery["node_profiles"]}
        endpoint = profiles["pvm1003"]["verified_endpoint"]
        self.assertEqual("http://10.210.8.205:8090", endpoint)  # noqa: secret-scan
        self.assertIn("pvm1003", discovery["vm_boundary"]["nodes"])

    def test_generated_artifacts_have_no_rfc1918(self):
        for blob in (self.markdown, json.dumps(self.index, ensure_ascii=False)):
            self.assertIsNone(build.RFC1918.search(blob))

    def test_service_node_profiles_strip_verified_endpoint(self):
        for service in self.index["system"]["services"]:
            self.assertTrue(service["discovery"]["node_profiles"])
            for profile in service["discovery"]["node_profiles"]:
                self.assertNotIn("verified_endpoint", profile)
                self.assertTrue(profile["verified"])
            canonical = service["discovery"]["canonical"]
            self.assertTrue(canonical["fixed_entry_present"])
            self.assertEqual(
                "descriptor:discovery.canonical.fixed_entry", canonical["fixed_entry_ref"]
            )

    def test_failure_taxonomy_has_required_codes(self):
        codes = {rule["code"] for rule in self.index["system"]["failure_taxonomy"]}
        for code in (
            "BLOCKED_STORAGE_AUTH",
            "BLOCKED_KB_AUTH",
            "STALE_INDEX",
            "EVIDENCE_GAP",
            "SOURCE_MISSING",
            "SERVICE_DISCOVERY_DRIFT",
            "BLOCKED_KB_BOOTSTRAP_PARITY",
        ):
            self.assertIn(code, codes)

    def test_knowledge_loop_has_nine_unique_steps(self):
        loop = self.catalog["knowledge_loop"]
        ids = [step["id"] for step in loop]
        self.assertEqual(9, len(loop))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual("evidence.produce", ids[0])
        self.assertEqual("kb.reuse", ids[-1])

    def test_unapproved_fixed_entry_fails_build(self):
        def mutate(_catalog, descriptors):
            storage = descriptors["templates/noahsark-index/services/storage-service.json"]
            storage["discovery"]["canonical"]["fixed_entry"] = "http://10.1.2.3:8090"  # noqa: secret-scan

        errors = self._validate_temp_tree(mutate)
        self.assertTrue(any("未批准的固定入口" in err for err in errors), errors)

    def test_private_literal_outside_allowlist_fails_build(self):
        def mutate(_catalog, descriptors):
            storage = descriptors["templates/noahsark-index/services/storage-service.json"]
            storage["discovery"]["node_profiles"][0]["note"] = "10.9.9.9"  # noqa: secret-scan

        errors = self._validate_temp_tree(mutate)
        self.assertTrue(any("私网字面量" in err for err in errors), errors)

    def test_write_path_rule_says_health_is_not_write_proof(self):
        for path in (STORAGE_DESCRIPTOR, KB_DESCRIPTOR):
            descriptor = read_descriptor(path)
            self.assertIn("写链路", descriptor["health_contract"]["write_path_rule"])

    def test_check_services_passes_in_repo(self):
        code, stdout, stderr = run(
            HERE / "check-services.py", ["--check", "--dir", str(self.out)]
        )
        self.assertEqual(0, code, stdout + stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
