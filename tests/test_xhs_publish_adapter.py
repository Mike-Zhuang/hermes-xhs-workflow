import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.xhs_publish_adapter import (
    main as publish_main,
    publish_once,
    reconcile_attempt,
)
from scripts.xhs_workflow import approve_manifest, prepare_manifest

PNG_HEADER = b"\x89PNG\r\n\x1a\n"
COOKIE = "a1=test; web_session=test"


class XHSPublishAdapterTests(unittest.TestCase):
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
            attempt = root / "attempt.json"
            record = root / "publication.json"
            with self.assertRaises(ValueError):
                publish_once(
                    manifest,
                    approval,
                    attempt,
                    record,
                    api_tool=self._fake_tool_with_failed_readback(root),
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )

            result = reconcile_attempt(
                manifest,
                approval,
                attempt,
                record,
                api_tool=self._fake_readback_only_tool(root),
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

    def test_readback_failure_marks_outcome_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)
            attempt = root / "attempt.json"

            with self.assertRaisesRegex(ValueError, "outcome is unknown"):
                publish_once(
                    manifest,
                    approval,
                    attempt,
                    root / "publication.json",
                    api_tool=self._fake_tool_with_failed_readback(root),
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
                    attempt,
                    root / "publication.json",
                    api_tool=root / "fake_failed_readback.py",
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )
            calls = (
                (root / "failed-calls.jsonl").read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(calls.count("post_note"), 1)

    def test_invalid_backend_python_is_rejected_before_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)
            attempt = root / "attempt.json"

            with self.assertRaisesRegex(ValueError, "backend Python"):
                publish_once(
                    manifest,
                    approval,
                    attempt,
                    root / "publication.json",
                    api_tool=self._fake_tool(root),
                    api_python=root / "missing-python",
                    cookie_file=cookie,
                    timeout_seconds=10,
                    verification_attempts=1,
                    verification_delay_seconds=0,
                )

            self.assertFalse(attempt.exists())

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
                    root / "attempt.json",
                    root / "publication.json",
                    api_tool=self._fake_rejecting_tool(root),
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
                    "--attempt",
                    str(root / "attempt.json"),
                    "--record",
                    str(root / "publication.json"),
                    "--api-tool",
                    str(self._fake_tool(root)),
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

    def test_one_user_trigger_publishes_once_and_records_verified_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, approval = self._package(root)
            tool = self._fake_tool(root)
            cookie = root / "cookie.txt"
            cookie.write_text(COOKIE, encoding="utf-8")
            os.chmod(cookie, 0o600)
            attempt = root / "attempt.json"
            record = root / "publication.json"

            result = publish_once(
                manifest,
                approval,
                attempt,
                record,
                api_tool=tool,
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
