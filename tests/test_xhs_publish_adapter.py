import builtins
import json
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import xhs_publish_adapter as publisher_module
from scripts.xhs_publish_adapter import (
    main as publish_main,
    publish_once,
    reconcile_attempt,
)
from scripts.xhs_workflow import approve_manifest, prepare_manifest

PNG_HEADER = b"\x89PNG\r\n\x1a\n"
COOKIE = "a1=test; web_session=test"


class XHSPublishAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._venv_tmp = tempfile.TemporaryDirectory()
        venv_root = Path(cls._venv_tmp.name) / "venv"
        (venv_root / "bin").mkdir(parents=True)
        (venv_root / "pyvenv.cfg").write_text(
            f"home = {Path(sys.executable).parent}\n"
            "include-system-site-packages = false\n"
            f"version = {sys.version_info.major}.{sys.version_info.minor}\n",
            encoding="utf-8",
        )
        cls.api_python = venv_root / "bin" / "python"
        cls.api_python.symlink_to(Path(sys.executable).resolve())

    @classmethod
    def tearDownClass(cls):
        cls._venv_tmp.cleanup()

    def _package(self, root: Path, *, ttl_seconds: int = 1800) -> tuple[Path, Path]:
        (root / "01.png").write_bytes(PNG_HEADER + b"exact-image")
        source = root / "post.json"
        source.write_text(
            json.dumps(
                {
                    "title": "一次发布",
                    "content": "TL;DR：由一次明确指令触发。",
                    "images": ["01.png"],
                    "topics": ["AI"],
                    "visibility": "public",
                    "publish_mode": "immediate",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        manifest = root / "manifest.json"
        approval = root / "approval.json"
        prepared = prepare_manifest(source, manifest)
        approve_manifest(
            manifest,
            approval,
            confirmed_by="user",
            confirmed_content_hash=prepared["content_hash"],
            ttl_seconds=ttl_seconds,
        )
        return manifest, approval

    def _attempt(self, root: Path, approval: Path) -> Path:
        approval_id = json.loads(approval.read_text(encoding="utf-8"))["approval_id"]
        return root / f".xhs-publish-attempt-{approval_id}.json"

    def _recovery_fixture(self, root: Path):
        timestamp = publisher_module._utc_now()
        approval = {
            "manifest_id": "manifest-test",
            "content_hash": "a" * 64,
            "approval_id": str(publisher_module.uuid.uuid4()),
        }
        attempt = {
            **approval,
            "status": "backend_accepted",
            "note_id": "note-123",
            "backend_accepted_at": timestamp,
        }
        record = {
            "schema_version": 1,
            "publication_id": str(publisher_module.uuid.uuid4()),
            **approval,
            "status": "publication_recorded",
            "platform": "xiaohongshu",
            "note_id": "note-123",
            "url": "https://www.xiaohongshu.com/explore/note-123",
            "published_at": timestamp,
            "verification": {
                "method": "creator_api_readback",
                "verified_by": "agent",
                "verified_at": timestamp,
            },
            "recorded_at": timestamp,
        }
        attempt_path = root / "attempt.json"
        record_path = root / "publication.json"
        attempt_path.write_text(json.dumps(attempt), encoding="utf-8")
        record_path.write_text(json.dumps(record), encoding="utf-8")
        os.chmod(attempt_path, 0o600)
        os.chmod(record_path, 0o600)
        return approval, attempt, attempt_path, record, record_path

    def _fake_tool(self, root: Path) -> Path:
        tool = root / "fake_xhs_api_tool.py"
        tool.write_text(
            """#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('command')
p.add_argument('namespace')
p.add_argument('method')
p.add_argument('--params-file', required=True)
p.add_argument('--out', required=True)
a = p.parse_args()
payload = json.loads(Path(a.params_file).read_text(encoding='utf-8'))
assert payload['cookies_str'] == 'a1=test; web_session=test'
calls = Path(__file__).with_name('calls.jsonl')
with calls.open('a', encoding='utf-8') as handle:
    handle.write(json.dumps({'namespace': a.namespace, 'method': a.method}) + '\\n')
if a.method == 'post_note':
    note = payload['noteInfo']
    assert note['title'] == '一次发布'
    assert note['desc'] == 'TL;DR：由一次明确指令触发。'
    assert note['topics'] == ['AI']
    assert len(note['images']) == 1
    assert Path(note['images'][0]).read_bytes() == b'\\x89PNG\\r\\n\\x1a\\nexact-image'
    response = {'namespace': 'creator', 'method': 'post_note', 'result': [True, '成功', {'success': True, 'data': {'note_id': 'note-123'}}]}
elif a.method == 'get_publish_note_info':
    response = {'namespace': 'creator', 'method': 'get_publish_note_info', 'result': [True, '成功', {'success': True, 'data': {'notes': [{'note_id': 'note-123', 'title': '一次发布'}], 'page': -1}}]}
else:
    raise AssertionError(a.method)
Path(a.out).write_text(json.dumps(response, ensure_ascii=False), encoding='utf-8')
""",
            encoding="utf-8",
        )
        return tool

    def _fake_tool_with_failed_readback(self, root: Path) -> Path:
        tool = root / "fake_failed_readback.py"
        tool.write_text(
            """#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('command')
p.add_argument('namespace')
p.add_argument('method')
p.add_argument('--params-file', required=True)
p.add_argument('--out', required=True)
a = p.parse_args()
with Path(__file__).with_name('failed-calls.jsonl').open('a', encoding='utf-8') as handle:
    handle.write(a.method + '\\n')
if a.method == 'post_note':
    response = {'namespace': 'creator', 'method': 'post_note', 'result': [True, '成功', {'success': True, 'data': {'note_id': 'note-unknown'}}]}
    Path(a.out).write_text(json.dumps(response, ensure_ascii=False), encoding='utf-8')
else:
    raise SystemExit(9)
""",
            encoding="utf-8",
        )
        return tool

    def _fake_readback_only_tool(self, root: Path) -> Path:
        tool = root / "fake_readback_only.py"
        tool.write_text(
            """#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('command')
p.add_argument('namespace')
p.add_argument('method')
p.add_argument('--params-file', required=True)
p.add_argument('--out', required=True)
a = p.parse_args()
assert a.method == 'get_publish_note_info'
response = {'namespace': 'creator', 'method': a.method, 'result': [True, '成功', {'success': True, 'data': {'notes': [{'note_id': 'note-unknown', 'title': '一次发布'}], 'page': -1}}]}
Path(a.out).write_text(json.dumps(response, ensure_ascii=False), encoding='utf-8')
Path(__file__).with_name('reconcile-method.txt').write_text(a.method, encoding='utf-8')
""",
            encoding="utf-8",
        )
        return tool

    def test_reconcile_unknown_attempt_uses_readback_without_republishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)
            attempt = self._attempt(root, approval)
            record = root / "publication.json"
            with self.assertRaises(ValueError):
                publish_once(
                    manifest,
                    approval,
                    record,
                    api_tool=self._fake_tool_with_failed_readback(root),
                    api_python=self.api_python,
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )

            result = reconcile_attempt(
                manifest,
                approval,
                record,
                api_tool=self._fake_readback_only_tool(root),
                api_python=self.api_python,
                cookie_file=cookie,
                timeout_seconds=10,
            )

            self.assertEqual(result["note_id"], "note-unknown")
            self.assertEqual(
                (root / "reconcile-method.txt").read_text(encoding="utf-8"),
                "get_publish_note_info",
            )
            self.assertEqual(
                json.loads(attempt.read_text(encoding="utf-8"))["status"], "verified"
            )

    def test_expired_approval_still_allows_read_only_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root, ttl_seconds=1)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)
            record = root / "publication.json"

            with self.assertRaises(ValueError):
                publish_once(
                    manifest,
                    approval,
                    record,
                    api_tool=self._fake_tool_with_failed_readback(root),
                    api_python=self.api_python,
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )

            time.sleep(1.1)
            result = reconcile_attempt(
                manifest,
                approval,
                record,
                api_tool=self._fake_readback_only_tool(root),
                api_python=self.api_python,
                cookie_file=cookie,
                timeout_seconds=10,
            )

            self.assertEqual(result["note_id"], "note-unknown")
            calls = (root / "failed-calls.jsonl").read_text(encoding="utf-8")
            self.assertEqual(calls.splitlines().count("post_note"), 1)

    def test_approval_expiring_during_preflight_blocks_external_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root, ttl_seconds=1)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)
            real_snapshot = publisher_module._snapshot_assets

            def delayed_snapshot(*args, **kwargs):
                snapshots = real_snapshot(*args, **kwargs)
                time.sleep(1.1)
                return snapshots

            with (
                patch.object(
                    publisher_module,
                    "_snapshot_assets",
                    side_effect=delayed_snapshot,
                ),
                self.assertRaisesRegex(ValueError, "expired.*external write"),
            ):
                publish_once(
                    manifest,
                    approval,
                    root / "publication.json",
                    api_tool=self._fake_tool(root),
                    api_python=self.api_python,
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )

            self.assertFalse((root / "calls.jsonl").exists())
            self.assertFalse(self._attempt(root, approval).exists())

    def test_snapshot_failure_does_not_consume_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)

            with (
                patch.object(
                    publisher_module,
                    "_snapshot_assets",
                    side_effect=publisher_module.PublishError(
                        "injected snapshot failure"
                    ),
                ),
                self.assertRaisesRegex(ValueError, "injected snapshot failure"),
            ):
                publish_once(
                    manifest,
                    approval,
                    root / "publication.json",
                    api_tool=self._fake_tool(root),
                    api_python=self.api_python,
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )

            self.assertFalse(self._attempt(root, approval).exists())
            self.assertFalse((root / "calls.jsonl").exists())

    def test_readback_failure_marks_outcome_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)
            attempt = self._attempt(root, approval)

            with self.assertRaisesRegex(ValueError, "outcome is unknown"):
                publish_once(
                    manifest,
                    approval,
                    root / "publication.json",
                    api_tool=self._fake_tool_with_failed_readback(root),
                    api_python=self.api_python,
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )

            self.assertEqual(
                json.loads(attempt.read_text(encoding="utf-8"))["status"],
                "outcome_unknown",
            )

            with self.assertRaisesRegex(
                ValueError, "attempt already exists|do not retry"
            ):
                publish_once(
                    manifest,
                    approval,
                    root / "publication.json",
                    api_tool=root / "fake_failed_readback.py",
                    api_python=self.api_python,
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )
            calls = (
                (root / "failed-calls.jsonl").read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(calls.count("post_note"), 1)

    def test_reconcile_repairs_attempt_after_record_was_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)
            tool = self._fake_tool(root)
            record_path = root / "publication.json"
            original_write_json = publisher_module._write_json

            def fail_verified_attempt(path, data, *, mode):
                if data.get("status") == "verified":
                    raise OSError("injected final attempt write failure")
                return original_write_json(path, data, mode=mode)

            with (
                patch.object(
                    publisher_module,
                    "_write_json",
                    side_effect=fail_verified_attempt,
                ),
                self.assertRaisesRegex(OSError, "final attempt write failure"),
            ):
                publish_once(
                    manifest,
                    approval,
                    record_path,
                    api_tool=tool,
                    api_python=self.api_python,
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )

            self.assertTrue(record_path.exists())
            attempt_path = self._attempt(root, approval)
            self.assertEqual(
                json.loads(attempt_path.read_text(encoding="utf-8"))["status"],
                "backend_accepted",
            )
            calls_before = (root / "calls.jsonl").read_text(encoding="utf-8")

            record = reconcile_attempt(
                manifest,
                approval,
                record_path,
                api_tool=tool,
                api_python=self.api_python,
                cookie_file=cookie,
                timeout_seconds=10,
            )

            self.assertEqual(record["note_id"], "note-123")
            self.assertEqual(
                json.loads(attempt_path.read_text(encoding="utf-8"))["status"],
                "verified",
            )
            self.assertEqual(
                (root / "calls.jsonl").read_text(encoding="utf-8"), calls_before
            )

    def test_recovery_rejects_invalid_verified_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            approval, attempt, attempt_path, record, record_path = (
                self._recovery_fixture(root)
            )
            record["verification"]["verified_at"] = "not-a-time"
            record_path.write_text(json.dumps(record), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "timestamp|verification"):
                publisher_module._recover_from_existing_record(
                    record_path, attempt_path, attempt, approval
                )

            self.assertEqual(
                json.loads(attempt_path.read_text(encoding="utf-8"))["status"],
                "backend_accepted",
            )

    def test_recovery_rejects_unknown_record_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            approval, attempt, attempt_path, record, record_path = (
                self._recovery_fixture(root)
            )
            record["unexpected"] = "value"
            record_path.write_text(json.dumps(record), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "schema|field"):
                publisher_module._recover_from_existing_record(
                    record_path, attempt_path, attempt, approval
                )

            self.assertEqual(
                json.loads(attempt_path.read_text(encoding="utf-8"))["status"],
                "backend_accepted",
            )

    def test_recovery_rejects_future_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            approval, attempt, attempt_path, record, record_path = (
                self._recovery_fixture(root)
            )
            future = publisher_module._format_utc(
                datetime.now(timezone.utc) + timedelta(seconds=1)
            )
            attempt["backend_accepted_at"] = future
            record["published_at"] = future
            record["verification"]["verified_at"] = future
            record["recorded_at"] = future
            record_path.write_text(json.dumps(record), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "future"):
                publisher_module._recover_from_existing_record(
                    record_path, attempt_path, attempt, approval
                )

            self.assertEqual(
                json.loads(attempt_path.read_text(encoding="utf-8"))["status"],
                "backend_accepted",
            )

    def test_recovery_rejects_verification_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            approval, attempt, attempt_path, record, record_path = (
                self._recovery_fixture(root)
            )
            published = publisher_module._parse_utc(record["published_at"])
            record["verification"]["verified_at"] = publisher_module._format_utc(
                published - timedelta(seconds=1)
            )
            record_path.write_text(json.dumps(record), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "predates"):
                publisher_module._recover_from_existing_record(
                    record_path, attempt_path, attempt, approval
                )

            self.assertEqual(
                json.loads(attempt_path.read_text(encoding="utf-8"))["status"],
                "backend_accepted",
            )

    def test_concurrent_calls_consume_one_approval_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)
            tool = self._fake_tool(root)

            def invoke(index: int):
                try:
                    return publish_once(
                        manifest,
                        approval,
                        root / f"publication-{index}.json",
                        api_tool=tool,
                        api_python=self.api_python,
                        cookie_file=cookie,
                        timeout_seconds=10,
                        verification_attempts=1,
                        verification_delay_seconds=0,
                    )
                except ValueError as exc:
                    return exc

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(invoke, (1, 2)))

            self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
            self.assertEqual(sum(isinstance(item, ValueError) for item in outcomes), 1)
            calls = [
                json.loads(line)["method"]
                for line in (root / "calls.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(calls.count("post_note"), 1)

    def test_invalid_backend_python_is_rejected_before_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)
            attempt = self._attempt(root, approval)

            with self.assertRaisesRegex(ValueError, "backend Python"):
                publish_once(
                    manifest,
                    approval,
                    root / "publication.json",
                    api_tool=self._fake_tool(root),
                    api_python=root / "missing-python",
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )

            self.assertFalse(attempt.exists())

    def test_symlinked_api_tool_is_rejected_before_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)
            tool_link = root / "api-tool-link.py"
            tool_link.symlink_to(self._fake_tool(root))

            with self.assertRaisesRegex(ValueError, "symlink|regular"):
                publish_once(
                    manifest,
                    approval,
                    root / "publication.json",
                    api_tool=tool_link,
                    api_python=self.api_python,
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )

            self.assertFalse(self._attempt(root, approval).exists())

    def test_noncanonical_api_tool_path_has_accurate_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "child").mkdir()
            tool = self._fake_tool(root)
            noncanonical = root / "child" / ".." / tool.name

            with self.assertRaisesRegex(ValueError, "canonical"):
                publisher_module._resolve_tool(noncanonical)

    def test_backend_python_must_be_explicitly_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)

            with self.assertRaisesRegex(ValueError, "explicit|XHS_API_PYTHON|isolated"):
                publish_once(
                    manifest,
                    approval,
                    root / "publication.json",
                    api_tool=self._fake_tool(root),
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )

            self.assertFalse(self._attempt(root, approval).exists())

    def test_main_python_is_not_an_isolated_backend_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)

            with self.assertRaisesRegex(ValueError, "isolated|virtual environment"):
                publish_once(
                    manifest,
                    approval,
                    root / "publication.json",
                    api_tool=self._fake_tool(root),
                    api_python=sys.executable,
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )

            self.assertFalse(self._attempt(root, approval).exists())

    def _fake_rejecting_tool(self, root: Path) -> Path:
        tool = root / "fake_rejecting_tool.py"
        tool.write_text(
            """#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('command')
p.add_argument('namespace')
p.add_argument('method')
p.add_argument('--params-file', required=True)
p.add_argument('--out', required=True)
a = p.parse_args()
response = {'namespace': 'creator', 'method': a.method, 'result': [False, 'a1=test; web_session=test', {'success': False}]}
Path(a.out).write_text(json.dumps(response), encoding='utf-8')
""",
            encoding="utf-8",
        )
        return tool

    def _fake_tool_requiring_descriptor_assets(self, root: Path) -> Path:
        tool = root / "fake_descriptor_tool.py"
        tool.write_text(
            """#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('command')
p.add_argument('namespace')
p.add_argument('method')
p.add_argument('--params-file', required=True)
p.add_argument('--out', required=True)
a = p.parse_args()
payload = json.loads(Path(a.params_file).read_text(encoding='utf-8'))
if a.method == 'post_note':
    images = payload['noteInfo']['images']
    assert images and all(path.startswith(('/proc/self/fd/', '/dev/fd/')) for path in images)
    for path in images:
        try:
            with open(path, 'r+b') as mutable:
                mutable.write(b'tampered')
        except OSError:
            pass
        else:
            raise SystemExit('asset descriptor can be reopened for writing')
    assert all(Path(path).read_bytes().startswith(b'\\x89PNG\\r\\n\\x1a\\n') for path in images)
    data = {'note_id': 'note-123'}
else:
    data = {'notes': [{'note_id': 'note-123', 'title': '一次发布'}], 'page': -1}
response = {'namespace': 'creator', 'method': a.method, 'result': [True, 'ok', {'success': True, 'data': data}]}
Path(a.out).write_text(json.dumps(response, ensure_ascii=False), encoding='utf-8')
""",
            encoding="utf-8",
        )
        return tool

    def test_backend_receives_only_inherited_read_only_asset_descriptors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)

            result = publish_once(
                manifest,
                approval,
                root / "publication.json",
                api_tool=self._fake_tool_requiring_descriptor_assets(root),
                api_python=self.api_python,
                cookie_file=cookie,
                timeout_seconds=10,
                verification_attempts=1,
                verification_delay_seconds=0,
            )

            self.assertEqual(result["note_id"], "note-123")

    def test_snapshot_fstat_failure_does_not_leak_descriptor(self):
        if not Path("/proc/self/fd").is_dir() or not hasattr(os, "memfd_create"):
            self.skipTest("Linux memfd descriptor accounting is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, _approval = self._package(root)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            snapshot_root = root / "snapshots"
            snapshot_root.mkdir()
            original_fstat = os.fstat

            def fail_snapshot_fstat(descriptor):
                try:
                    target = os.readlink(f"/proc/self/fd/{descriptor}")
                except OSError:
                    target = ""
                if "memfd:xhs-approved-image" in target:
                    raise OSError("injected snapshot fstat failure")
                return original_fstat(descriptor)

            descriptors_before = len(os.listdir("/proc/self/fd"))
            with (
                patch.object(
                    publisher_module.os,
                    "fstat",
                    side_effect=fail_snapshot_fstat,
                ),
                self.assertRaisesRegex(OSError, "injected snapshot fstat failure"),
            ):
                publisher_module._snapshot_assets(manifest, snapshot_root)
            descriptors_after = len(os.listdir("/proc/self/fd"))

            self.assertEqual(descriptors_after, descriptors_before)

    def test_fallback_fdopen_failure_does_not_leak_descriptor(self):
        if not Path("/proc/self/fd").is_dir():
            self.skipTest("descriptor accounting is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, _approval = self._package(root)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            snapshot_root = root / "snapshots"
            snapshot_root.mkdir()
            original_hasattr = builtins.hasattr

            def disable_memfd(namespace, name):
                if namespace is publisher_module.os and name == "memfd_create":
                    return False
                return original_hasattr(namespace, name)

            descriptors_before = len(os.listdir("/proc/self/fd"))
            with (
                patch("builtins.hasattr", side_effect=disable_memfd),
                patch.object(
                    publisher_module.os,
                    "fdopen",
                    side_effect=OSError("injected fdopen failure"),
                ),
                self.assertRaisesRegex(OSError, "injected fdopen failure"),
            ):
                publisher_module._snapshot_assets(manifest, snapshot_root)
            descriptors_after = len(os.listdir("/proc/self/fd"))

            self.assertEqual(descriptors_after, descriptors_before)

    def _fake_tool_echoing_cookie_as_note_id(self, root: Path) -> Path:
        tool = root / "fake_cookie_note_id.py"
        tool.write_text(
            """#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('command')
p.add_argument('namespace')
p.add_argument('method')
p.add_argument('--params-file', required=True)
p.add_argument('--out', required=True)
a = p.parse_args()
payload = json.loads(Path(a.params_file).read_text(encoding='utf-8'))
secret = payload['cookies_str']
if a.method == 'post_note':
    data = {'note_id': secret}
else:
    data = {'notes': [{'note_id': secret, 'title': '一次发布'}], 'page': -1}
response = {'namespace': 'creator', 'method': a.method, 'result': [True, 'ok', {'success': True, 'data': data}]}
Path(a.out).write_text(json.dumps(response, ensure_ascii=False), encoding='utf-8')
""",
            encoding="utf-8",
        )
        return tool

    def _fake_tool_with_excessive_response_depth(self, root: Path) -> Path:
        tool = root / "fake_deep_response.py"
        tool.write_text(
            """#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('command')
p.add_argument('namespace')
p.add_argument('method')
p.add_argument('--params-file', required=True)
p.add_argument('--out', required=True)
a = p.parse_args()
deep = 'leaf'
for _ in range(40):
    deep = {'next': deep}
data = {'note_id': 'note-123'} if a.method == 'post_note' else {'notes': [{'note_id': 'note-123', 'title': '一次发布'}]}
response = {'namespace': 'creator', 'method': a.method, 'extra': deep, 'result': [True, 'ok', {'success': True, 'data': data}]}
Path(a.out).write_text(json.dumps(response), encoding='utf-8')
""",
            encoding="utf-8",
        )
        return tool

    def _fake_tool_with_credential_field(self, root: Path) -> Path:
        tool = root / "fake_credential_field.py"
        tool.write_text(
            """#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('command')
p.add_argument('namespace')
p.add_argument('method')
p.add_argument('--params-file', required=True)
p.add_argument('--out', required=True)
a = p.parse_args()
data = {'note_id': 'note-123', 'token': 'unexpected'} if a.method == 'post_note' else {'notes': []}
response = {'namespace': 'creator', 'method': a.method, 'result': [True, 'ok', {'success': True, 'data': data}]}
Path(a.out).write_text(json.dumps(response), encoding='utf-8')
""",
            encoding="utf-8",
        )
        return tool

    def test_backend_success_response_with_credential_field_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)

            with self.assertRaisesRegex(ValueError, "credential|secret"):
                publish_once(
                    manifest,
                    approval,
                    root / "publication.json",
                    api_tool=self._fake_tool_with_credential_field(root),
                    api_python=self.api_python,
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )

    def test_backend_response_exceeding_json_depth_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)

            with self.assertRaisesRegex(ValueError, "nesting|depth"):
                publish_once(
                    manifest,
                    approval,
                    root / "publication.json",
                    api_tool=self._fake_tool_with_excessive_response_depth(root),
                    api_python=self.api_python,
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )

    def test_success_response_cannot_persist_cookie_as_note_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)

            with self.assertRaisesRegex(ValueError, "credential|secret|note_id"):
                publish_once(
                    manifest,
                    approval,
                    root / "publication.json",
                    api_tool=self._fake_tool_echoing_cookie_as_note_id(root),
                    api_python=self.api_python,
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )

            for path in root.iterdir():
                if (
                    path.is_file()
                    and path != cookie
                    and path.suffix in {".json", ".txt", ".log", ".py"}
                ):
                    self.assertNotIn(COOKIE, path.read_text(encoding="utf-8"))

    def test_backend_error_never_echoes_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)

            with self.assertRaises(ValueError) as raised:
                publish_once(
                    manifest,
                    approval,
                    root / "publication.json",
                    api_tool=self._fake_rejecting_tool(root),
                    api_python=self.api_python,
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )

            self.assertNotIn(COOKIE, str(raised.exception))

    def test_cli_publish_subcommand_runs_one_shot_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)

            exit_code = publish_main(
                [
                    "publish",
                    "--manifest",
                    str(manifest),
                    "--approval",
                    str(approval),
                    "--record",
                    str(root / "publication.json"),
                    "--api-tool",
                    str(self._fake_tool(root)),
                    "--api-python",
                    str(self.api_python),
                    "--cookie-file",
                    str(cookie),
                    "--verification-attempts",
                    "1",
                    "--verification-delay-seconds",
                    "0",
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue((root / "publication.json").exists())

    def test_same_approval_cannot_republish_with_another_record_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            tool = self._fake_tool(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)

            publish_once(
                manifest,
                approval,
                root / "publication-a.json",
                api_tool=tool,
                api_python=self.api_python,
                cookie_file=cookie,
                timeout_seconds=10,
                verification_attempts=1,
                verification_delay_seconds=0,
            )
            with self.assertRaisesRegex(ValueError, "already consumed|already exists"):
                publish_once(
                    manifest,
                    approval,
                    root / "publication-b.json",
                    api_tool=tool,
                    api_python=self.api_python,
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )

            calls = [
                json.loads(line)
                for line in (root / "calls.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual([call["method"] for call in calls].count("post_note"), 1)
            self.assertEqual(len(list(root.glob(".xhs-publish-attempt-*.json"))), 1)

    def test_manifest_path_replacement_after_verification_cannot_change_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            tool = self._fake_tool(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)
            original_load = publisher_module._load_json

            def replace_then_load(path):
                replacement = json.loads(manifest.read_text(encoding="utf-8"))
                replacement["post"]["title"] = "未获授权的替换标题"
                manifest.write_text(
                    json.dumps(replacement, ensure_ascii=False), encoding="utf-8"
                )
                return original_load(path)

            with patch.object(
                publisher_module, "_load_json", side_effect=replace_then_load
            ):
                result = publish_once(
                    manifest,
                    approval,
                    root / "publication.json",
                    api_tool=tool,
                    api_python=self.api_python,
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )

            self.assertEqual(result["note_id"], "note-123")

    def test_symlinked_manifest_or_approval_is_rejected_before_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            manifest_link = root / "manifest-link.json"
            approval_link = root / "approval-link.json"
            manifest_link.symlink_to(manifest)
            approval_link.symlink_to(approval)

            for candidate_manifest, candidate_approval in (
                (manifest_link, approval),
                (manifest, approval_link),
            ):
                with self.subTest(
                    manifest=candidate_manifest, approval=candidate_approval
                ):
                    with self.assertRaisesRegex(ValueError, "securely read|symlink"):
                        publish_once(
                            candidate_manifest,
                            candidate_approval,
                            root / "publication.json",
                            api_tool=root / "unused-tool.py",
                            api_python=self.api_python,
                            cookie_file=root / "unused-cookie.txt",
                        )

            self.assertFalse(self._attempt(root, approval).exists())

    def test_one_user_trigger_publishes_once_and_records_verified_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            tool = self._fake_tool(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)
            attempt = self._attempt(root, approval)
            record = root / "publication.json"

            result = publish_once(
                manifest,
                approval,
                record,
                api_tool=tool,
                api_python=self.api_python,
                cookie_file=cookie,
                timeout_seconds=10,
                verification_attempts=1,
                verification_delay_seconds=0,
            )

            self.assertEqual(result["status"], "publication_recorded")
            self.assertEqual(result["note_id"], "note-123")
            self.assertEqual(
                result["url"], "https://www.xiaohongshu.com/explore/note-123"
            )
            self.assertEqual(
                json.loads(attempt.read_text(encoding="utf-8"))["status"], "verified"
            )
            self.assertEqual(
                json.loads(record.read_text(encoding="utf-8"))["note_id"], "note-123"
            )
            calls = [
                json.loads(line)
                for line in (root / "calls.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [call["method"] for call in calls],
                ["post_note", "get_publish_note_info"],
            )


if __name__ == "__main__":
    unittest.main()
