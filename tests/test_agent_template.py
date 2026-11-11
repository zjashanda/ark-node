"""智能体模板测试（标准库 unittest）。

覆盖模板正文的必备段落、profile 的结构约束、凭据与时间口径的自查，
以及「发布 dry-run → 校验 → 篡改必被发现」这条发布链路的端到端行为。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "templates" / "agents"
TEMPLATE = AGENTS_DIR / "noahsark-agent-template.md"
PROFILE = AGENTS_DIR / "agent-profile.v1.json"
PUBLISHER = AGENTS_DIR / "publish-template.py"
VERIFIER = AGENTS_DIR / "verify-template.py"
ONBOARDING_DOC = REPO_ROOT / "docs" / "agent-onboarding.md"

sys.path.insert(0, str(REPO_ROOT / "tools"))


def load_profile() -> dict:
    return json.loads(PROFILE.read_text(encoding="utf-8"))


def read_template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


class TemplateContentTests(unittest.TestCase):
    """模板正文必须能独立讲清九件事。"""

    def setUp(self) -> None:
        self.text = read_template()

    def test_required_sections_present(self) -> None:
        required = [
            "ark.agent-template.v1",
            "coordinator",
            "node-executor",
            "hardware-bench",
            "knowledge-curator",
            "instructions",
            "description",
            "max_concurrent_tasks",
            "EXECUTION_CHECK",
            "PROGRESS_5M",
            "STATE_DRIFT",
            "RUNNING_NO_EVIDENCE",
            "background-and-yield",
            "ark.agent-onboarding.v1",
            "run.receipt.v1",
            "LOCAL_DRIFT",
            "ark-dispatch",
            "ark-archive",
            "ark-kb",
            "quarantine",
            "_quarantine",
        ]
        missing = [token for token in required if token not in self.text]
        self.assertEqual(missing, [], f"模板缺少关键内容: {missing}")

    def test_platform_is_not_a_skill(self) -> None:
        self.assertIn("不是一个 Skill", self.text)
        self.assertIn("任务闭环", self.text)

    def test_hardware_bench_is_serial(self) -> None:
        self.assertIn("`hardware-bench` | **1**", self.text)
        self.assertIn("设备独占", self.text)

    def test_instructions_vs_description_distinguished(self) -> None:
        self.assertIn("长期行为契约", self.text)
        self.assertIn("一句话定位", self.text)


class TemplatePathTests(unittest.TestCase):
    """任务目录规则：固定根 + 每任务独立 <tasks-id> 目录。"""

    def setUp(self) -> None:
        self.text = read_template()
        self.paths = load_profile()["default"]["paths"]

    def test_windows_task_root_is_d_drive(self) -> None:
        self.assertEqual(self.paths["task_root_windows"], "D:\\zhsh\\workspace\\tasks")
        self.assertIn("D:\\zhsh\\workspace\\tasks\\<tasks-id>\\", self.text)

    def test_linux_task_root_is_absolute_home_zhsh(self) -> None:
        self.assertEqual(self.paths["task_root_linux"], "/home/zhsh/workspace/tasks")
        self.assertIn("/home/zhsh/workspace/tasks/<tasks-id>/", self.text)
        self.assertNotIn("$HOME/zhsh", self.text, "Linux 任务根必须写死绝对路径，不用 $HOME")

    def test_task_dir_is_identified_by_task_id_only(self) -> None:
        self.assertEqual(self.paths["task_dir_pattern"], "<tasks-id>")
        self.assertEqual(self.paths["task_id_source"], "multica-issue-id-first")
        self.assertIn("<tasks-id>", self.text)

    def test_old_task_roots_are_gone(self) -> None:
        for stale in (
            "/home/bszheng/tasks",
            "C:\\NoahsArk\\tasks",
            "D:\\skillhub",
            "<YYYYMMDD-HHMMSS>-<slug>",
        ):
            self.assertNotIn(stale, self.text, f"模板里残留旧任务根: {stale}")

    def test_temp_dirs_are_not_task_roots(self) -> None:
        self.assertEqual(self.paths["on_violation"], "BLOCKED_WORKDIR_POLICY")
        self.assertIn("BLOCKED_WORKDIR_POLICY", self.text)
        self.assertNotIn("/tmp/<", self.text)
        self.assertIn("永远不能当任务根", self.text)

    def test_subdirs_and_root_files(self) -> None:
        self.assertEqual(
            self.paths["subdirs"], ["assets", "work", "logs", "result", "receipts"]
        )
        self.assertEqual(self.paths["root_files"], ["plan.md", "manifest.json"])
        for name in self.paths["subdirs"]:
            self.assertIn(f"`{name}/`", self.text)
        for name in self.paths["root_files"]:
            self.assertIn(f"`{name}`", self.text)


class Template1006Tests(unittest.TestCase):
    """存储节点与 1006 的取件规则。"""

    def setUp(self) -> None:
        self.text = read_template()
        self.storage = load_profile()["default"]["storage"]

    def test_storage_layout(self) -> None:
        self.assertEqual(self.storage["root"], "/srv/storage/ark")
        for key in ("results", "receipts", "artifacts", "quarantine"):
            self.assertTrue(self.storage[key].startswith("/srv/storage/ark/"))
        self.assertIn("_quarantine/", self.text)

    def test_delegate_when_node_unreachable(self) -> None:
        self.assertIn("委派给可达的节点", self.text)
        self.assertIn("不证明东西不存在", self.text)

    def test_asset_source_rules(self) -> None:
        for adapters in ("email", "nas", "oss", "node_storage", "local", "http"):
            self.assertIn(adapters, self.text)
        self.assertIn("不碰 IMAP/SMTP", self.text)
        self.assertIn("默认 dry-run", self.text)


class GitAndSkillHubTests(unittest.TestCase):
    """canonical 真源与 SkillHub 七状态。"""

    def setUp(self) -> None:
        self.text = read_template()
        self.sources = load_profile()["default"]["sources"]

    def test_canonical_git_is_source_of_truth(self) -> None:
        self.assertEqual(self.sources["git"]["workflow"], "branch-and-pull-request")
        self.assertEqual(self.sources["git"]["push_to_main"], "forbidden-without-approval")
        self.assertIn("唯一真源", self.text)

    def test_skillhub_states_documented(self) -> None:
        for state in (
            "IN_SYNC",
            "UPDATED",
            "OUTDATED",
            "ABSENT",
            "LOCAL_DRIFT",
            "PRIVATE_SKILL",
            "REMOTE_UNREACHABLE",
        ):
            self.assertIn(state, self.text)

    def test_local_drift_never_overwritten(self) -> None:
        self.assertTrue(self.sources["skillhub"]["protect_local_drift"])
        self.assertIn("`apply` 永不覆盖", self.text)

    def test_sync_is_on_demand(self) -> None:
        self.assertEqual(self.sources["skillhub"]["sync_mode"], "on-demand")
        self.assertIn("不做常驻", self.text)


class LoopTests(unittest.TestCase):
    """六步闭环与回执字段。"""

    def setUp(self) -> None:
        self.text = read_template()
        self.default = load_profile()["default"]

    def test_loop_order(self) -> None:
        self.assertEqual(
            self.default["loop"],
            ["ark-dispatch", "execute", "ark-archive", "receipt", "ark-kb", "next-dispatch"],
        )

    def test_receipt_fields_documented(self) -> None:
        for token in (
            "run.receipt.v1",
            "skill_version",
            "attribution",
            "evidence_sha256",
            "confidence",
            "manifest_sha256",
            "needs_review",
        ):
            self.assertIn(token, self.text)

    def test_dispatch_is_decision_only(self) -> None:
        self.assertIn("只决策，不执行", self.text)
        self.assertIn("fail-closed", self.text)

    def test_code_tests_are_not_device_verification(self) -> None:
        self.assertIn("代码测试通过 ≠ 真机验证通过", self.text)


class SupervisionTests(unittest.TestCase):
    """监督契约必须完整，且明确禁止 background-and-yield。"""

    def setUp(self) -> None:
        self.text = read_template()
        self.supervision = load_profile()["default"]["supervision"]

    def test_four_rules(self) -> None:
        self.assertEqual(
            self.supervision["rules"],
            ["EXECUTION_CHECK", "PROGRESS_5M", "STATE_DRIFT", "RUNNING_NO_EVIDENCE"],
        )

    def test_progress_interval(self) -> None:
        self.assertEqual(self.supervision["progress_interval_minutes"], 5)
        self.assertIn("每 5 分钟", self.text)

    def test_no_fake_running(self) -> None:
        self.assertEqual(self.supervision["background_and_yield"], "forbidden")
        self.assertIn("不许说 started=YES", self.text)
        self.assertIn("前台阻塞等到结果", self.text)

    def test_four_rules_have_body(self) -> None:
        for rule in self.supervision["rules"]:
            self.assertIn(f"`{rule}`", self.text)


class ProfileSchemaTests(unittest.TestCase):
    """profile 是机器可读契约，字段必须自洽。"""

    def setUp(self) -> None:
        self.profile = load_profile()

    def test_schema_and_version(self) -> None:
        self.assertEqual(self.profile["schema"], "ark.agent-profile.v1")
        self.assertEqual(self.profile["template_version"], "1.0.1")

    def test_every_default_leaf_has_a_field_entry(self) -> None:
        fields = self.profile["fields"]

        def walk(node, prefix=""):
            for key, value in node.items():
                dotted = f"{prefix}{key}"
                if isinstance(value, dict):
                    walk(value, f"{dotted}.")
                else:
                    yield dotted

        missing = [dotted for dotted in walk(self.profile["default"]) if dotted not in fields]
        self.assertEqual(missing, [], f"default 里有字段没有 schema 定义: {missing}")

    def test_roles_complete(self) -> None:
        roles = self.profile["roles"]
        self.assertEqual(
            sorted(roles), ["coordinator", "hardware-bench", "knowledge-curator", "node-executor"]
        )
        for name, spec in roles.items():
            for key in ("label", "purpose", "max_concurrent_tasks", "default_description"):
                self.assertIn(key, spec, f"{name} 缺少 {key}")
            self.assertLessEqual(spec["max_concurrent_tasks"], 3)

    def test_hardware_bench_concurrency_is_one(self) -> None:
        self.assertEqual(self.profile["roles"]["hardware-bench"]["max_concurrent_tasks"], 1)
        self.assertEqual(self.profile["default"]["runtime"]["max_concurrent_tasks"], 2)

    def test_env_keys_match_ark_node_contract(self) -> None:
        canonical = json.loads((REPO_ROOT / "config" / "env-keys.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(self.profile["default"]["env_keys"]), sorted(canonical["keys"]))

    def test_env_keys_are_names_only(self) -> None:
        for key in self.profile["default"]["env_keys"]:
            self.assertRegex(key, r"^[A-Z][A-Z0-9_]*$")

    def test_time_contract(self) -> None:
        time_cfg = self.profile["default"]["time"]
        self.assertEqual(time_cfg["display_format"], "YYYY-MM-DD HH:MM:SS")
        self.assertEqual(time_cfg["stamp_format"], "YYYYMMDD-HHMMSS")
        self.assertEqual(time_cfg["offset_hours"], 8)
        self.assertEqual(time_cfg["zone_designator"], "forbidden")

    def test_approval_boundaries(self) -> None:
        approval = self.profile["default"]["approval"]
        self.assertEqual(
            approval["requires"],
            [
                "git-push",
                "external-publish",
                "real-device-action",
                "file-deletion",
                "permission-change",
            ],
        )
        self.assertEqual(approval["default_mode"], "dry-run")


class TimeContractTests(unittest.TestCase):
    """时间口径必须与 ark-node 契约同口径。"""

    def test_time_lint_clean(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "tools" / "time_lint.py"),
                str(TEMPLATE),
                str(PROFILE),
                str(PUBLISHER),
                str(VERIFIER),
                str(ONBOARDING_DOC),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_single_allowed_display_form_documented(self) -> None:
        text = read_template()
        self.assertIn("`YYYY-MM-DD HH:MM:SS`", text)
        self.assertIn("`2026-11-10 19:04:31`", text)

    def test_forbidden_forms_are_named_not_exemplified(self) -> None:
        text = read_template()
        self.assertIn("用字母 `T` 分隔", text)
        self.assertIn("`Z` 后缀", text)
        self.assertNotIn("2026-11-10T", text)

    def test_raw_prefix_exception(self) -> None:
        self.assertIn("`raw_` 前缀", read_template())


class CredentialTests(unittest.TestCase):
    """发布包必须零凭据。"""

    def test_secret_scan_clean(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "tools" / "secret_scan.py"),
                str(AGENTS_DIR),
                str(ONBOARDING_DOC),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_template_lists_names_only(self) -> None:
        import secret_scan

        text = read_template()
        for key in load_profile()["default"]["env_keys"]:
            self.assertIn(key, text)
        self.assertEqual(secret_scan.scan_text("template", text), [])

    def test_no_private_key_or_token_literals(self) -> None:
        text = read_template()
        self.assertNotIn("BEGIN " + "PRIVATE KEY", text)
        self.assertNotIn("ghp_", text)
        self.assertNotIn("LTAI", text)

    def test_no_plaintext_url_credentials(self) -> None:
        self.assertNotRegex(read_template(), r"://[^/\s:@]+:[^/\s@]+@")


class PublishVerifyTests(unittest.TestCase):
    """发布链路：dry-run 出包 → 校验通过 → 篡改被发现。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _publish(self) -> tuple[int, str, str]:
        proc = subprocess.run(
            [sys.executable, str(PUBLISHER), "--out-dir", str(self.tmp)],
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def _stage_package(self) -> None:
        for name in ("noahsark-agent-template.md", "agent-profile.v1.json", "verify-template.py"):
            shutil.copy2(AGENTS_DIR / name, self.tmp / name)

    def test_dry_run_writes_manifest_without_uploading(self) -> None:
        code, out, err = self._publish()
        self.assertEqual(code, 0, out + err)
        self.assertIn("DRY-RUN", out)
        manifest = json.loads((self.tmp / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "ark.agent-template.manifest.v1")
        self.assertEqual(manifest["version"], "1.0.1")
        self.assertRegex(manifest["published_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(manifest["checks"], {"secret_scan": "pass", "time_lint": "pass"})
        self.assertEqual(
            [entry["role"] for entry in manifest["files"]], ["template", "profile", "verifier"]
        )

    def test_latest_channel_manifest_passes_object_path_check(self) -> None:
        code, out, err = self._publish()
        self.assertEqual(code, 0, out + err)
        self._stage_package()
        path = self.tmp / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        prefix = manifest["oss"]["prefix"]
        manifest["channel"] = "latest"
        for entry in manifest["files"]:
            entry["object"] = f"{prefix}/latest/{entry['path']}"
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        proc = subprocess.run(
            [sys.executable, str(VERIFIER), "--dir", str(self.tmp), "--manifest", str(path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("渠道 latest", proc.stdout)

    def test_manifest_digests_match_files(self) -> None:
        import hashlib

        code, out, err = self._publish()
        self.assertEqual(code, 0, out + err)
        manifest = json.loads((self.tmp / "manifest.json").read_text(encoding="utf-8"))
        for entry in manifest["files"]:
            data = (AGENTS_DIR / entry["path"]).read_bytes()
            self.assertEqual(entry["bytes"], len(data))
            self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())

    def test_object_paths_are_immutable_plus_latest(self) -> None:
        code, out, err = self._publish()
        self.assertEqual(code, 0, out + err)
        self.assertIn("noahsark/agent-template/v1.0.1/noahsark-agent-template.md", out)
        self.assertIn("noahsark/agent-template/latest/noahsark-agent-template.md", out)
        self.assertIn("noahsark/agent-template/latest/manifest.json", out)

    def test_verify_passes_on_fresh_package(self) -> None:
        code, out, err = self._publish()
        self.assertEqual(code, 0, out + err)
        self._stage_package()
        proc = subprocess.run(
            [sys.executable, str(self.tmp / "verify-template.py"), "--dir", str(self.tmp)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("结果: pass", proc.stdout)

    def test_verify_detects_tampering(self) -> None:
        code, out, err = self._publish()
        self.assertEqual(code, 0, out + err)
        self._stage_package()
        tampered = self.tmp / "noahsark-agent-template.md"
        original = tampered.read_bytes()
        # 同长度改写：字节数不变，只有摘要能发现
        tampered.write_bytes(original.replace(b"1.0.1", b"9.9.9", 1))
        proc = subprocess.run(
            [sys.executable, str(self.tmp / "verify-template.py"), "--dir", str(self.tmp)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("sha256 不符", proc.stdout)

    def test_verify_without_manifest_is_no_source(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(VERIFIER), "--dir", str(self.tmp)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)

    def test_verify_json_is_machine_readable(self) -> None:
        code, out, err = self._publish()
        self.assertEqual(code, 0, out + err)
        self._stage_package()
        proc = subprocess.run(
            [
                sys.executable,
                str(self.tmp / "verify-template.py"),
                "--dir",
                str(self.tmp),
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["schema"], "ark.agent-template.verify.v1")
        self.assertEqual(payload["result"], "pass")
        self.assertGreaterEqual(len(payload["checks"]), 8)

    def _clean_env(self) -> dict:
        env = {key: value for key, value in os.environ.items() if key not in ("ACCESS_KEY_ID", "ACCESS_KEY_SECRET")}
        return env

    def test_apply_without_yes_is_usage_error(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(PUBLISHER), "--out-dir", str(self.tmp), "--apply", "--allow-dirty"],
            capture_output=True,
            text=True,
            env=self._clean_env(),
        )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_apply_without_credentials_is_forbidden(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(PUBLISHER),
                "--out-dir",
                str(self.tmp),
                "--apply",
                "--yes",
                "--endpoint",
                "oss.example.com",
                "--allow-dirty",
            ],
            capture_output=True,
            text=True,
            env=self._clean_env(),
        )
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertIn("凭据", proc.stderr)


class OnboardingDocTests(unittest.TestCase):
    """维护者文档必须覆盖发布与回滚，且不泄漏凭据。"""

    def setUp(self) -> None:
        self.text = ONBOARDING_DOC.read_text(encoding="utf-8")

    def test_covers_publish_and_rollback(self) -> None:
        for token in ("dry-run", "--apply --yes", "回滚", "latest", "verify-template.py", "语义化"):
            self.assertIn(token, self.text)

    def test_no_manifest_hand_editing(self) -> None:
        self.assertIn("只能由 `publish-template.py` 生成", self.text)


class OssUploadTests(unittest.TestCase):
    """上传细节：endpoint 归一、显式对象 ACL、ACL 必须进签名。"""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("publish_template", PUBLISHER)
        assert spec and spec.loader
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def _capture_put(self, visibility: str) -> object:
        captured: list = []

        class _Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            captured.append(request)
            return _Response()

        original = self.mod.urllib.request.urlopen
        self.mod.urllib.request.urlopen = fake_urlopen
        self.addCleanup(setattr, self.mod.urllib.request, "urlopen", original)
        self.mod.oss_put(
            endpoint_host="https://oss-cn-beijing.aliyuncs.com/",
            bucket="listenai-test-internal",
            key="noahsark/agent-template/v1.0.0/manifest.json",
            data=b"{}\n",
            ak="AKIDFORTEST",
            sk="SKFORTEST",
            visibility=visibility,
        )
        return captured[0]

    def test_normalize_endpoint_strips_scheme_and_trailing_slash(self) -> None:
        self.assertEqual(
            self.mod.normalize_endpoint("https://oss-cn-beijing.aliyuncs.com/"),
            "oss-cn-beijing.aliyuncs.com",
        )
        self.assertEqual(
            self.mod.normalize_endpoint("oss-cn-hangzhou.aliyuncs.com"),
            "oss-cn-hangzhou.aliyuncs.com",
        )

    def test_put_builds_virtual_hosted_url(self) -> None:
        request = self._capture_put("public-read")
        self.assertEqual(
            request.full_url,
            "https://listenai-test-internal.oss-cn-beijing.aliyuncs.com"
            "/noahsark/agent-template/v1.0.0/manifest.json",
        )

    def test_put_sends_object_level_acl_header(self) -> None:
        request = self._capture_put("public-read")
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["x-oss-object-acl"], "public-read")
        self.assertNotIn("x-oss-acl", headers)

    def test_acl_is_part_of_signature(self) -> None:
        import base64
        import hashlib
        import hmac as hmac_mod

        request = self._capture_put("public-read")
        headers = {key.lower(): value for key, value in request.header_items()}
        expected = base64.b64encode(
            hmac_mod.new(
                b"SKFORTEST",
                "\n".join(
                    [
                        "PUT",
                        headers["content-md5"],
                        headers["content-type"],
                        headers["date"],
                        "x-oss-object-acl:public-read",
                        "/listenai-test-internal/noahsark/agent-template/v1.0.0/manifest.json",
                    ]
                ).encode("utf-8"),
                hashlib.sha1,
            ).digest()
        ).decode("ascii")
        self.assertEqual(headers["authorization"], f"OSS AKIDFORTEST:{expected}")

    def test_unknown_visibility_rejected(self) -> None:
        import unittest.mock as mock

        env = {"ACCESS_KEY_ID": "AKIDFORTEST", "ACCESS_KEY_SECRET": "SKFORTEST"}
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(self.mod.PublishError) as ctx:
                self.mod.upload_all([], "oss-cn-beijing.aliyuncs.com", "b", "world-readable")
        self.assertEqual(ctx.exception.code, 2)

    def _run_upload(self, get_status: int, get_body: bytes) -> int:
        class _Response:
            def __init__(self, status, body):
                self.status = status
                self._body = body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            if request.get_method() == "GET":
                if get_status != 200:
                    raise self.mod.urllib.error.HTTPError(
                        request.full_url, get_status, "Forbidden", {}, None
                    )
                return _Response(200, get_body)
            return _Response(200, b"")

        original = self.mod.urllib.request.urlopen
        self.mod.urllib.request.urlopen = fake_urlopen
        self.addCleanup(setattr, self.mod.urllib.request, "urlopen", original)

        payload = b"{}\n"
        steps = [
            {
                "object": "noahsark/agent-template/v1.0.0/manifest.json",
                "sha256": self.mod.hashlib.sha256(payload).hexdigest(),
                "_data": payload,
            }
        ]
        import unittest.mock as mock

        env = {"ACCESS_KEY_ID": "AKIDFORTEST", "ACCESS_KEY_SECRET": "SKFORTEST"}
        with mock.patch.dict(os.environ, env, clear=False):
            return self.mod.upload_all(
                steps, "oss-cn-beijing.aliyuncs.com", "listenai-test-internal", "public-read"
            )

    def test_upload_confirms_anonymous_readback(self) -> None:
        payload = b"{}\n"
        self.assertEqual(self._run_upload(200, payload), 1)

    def test_upload_fails_when_object_stays_private(self) -> None:
        payload = b"{}\n"
        with self.assertRaises(self.mod.PublishError) as ctx:
            self._run_upload(403, b"")
        self.assertEqual(ctx.exception.code, 4)

    def test_upload_fails_when_readback_content_differs(self) -> None:
        with self.assertRaises(self.mod.PublishError) as ctx:
            self._run_upload(200, b"tampered\n")
        self.assertEqual(ctx.exception.code, 1)

    def _dirty_with(self, porcelain: str) -> bool:
        import unittest.mock as mock

        with mock.patch.object(self.mod, "git", lambda args: porcelain):
            return self.mod.git_dirty()

    def test_generated_manifest_does_not_count_as_dirty(self) -> None:
        self.assertFalse(self._dirty_with("M templates/agents/manifest.json"))

    def test_multiple_entries_still_detect_real_change(self) -> None:
        porcelain = "M templates/agents/manifest.json\nM templates/agents/publish-template.py"
        self.assertTrue(self._dirty_with(porcelain))

    def test_clean_tree_is_not_dirty(self) -> None:
        self.assertFalse(self._dirty_with(""))


if __name__ == "__main__":
    unittest.main()
