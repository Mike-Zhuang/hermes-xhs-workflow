import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from scripts.xhs_readonly_adapter import AdapterError, run_request


class XHSReadonlyAdapterTests(unittest.TestCase):
    def _write_fake_tool(self, root: Path) -> Path:
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
payload = json.loads(Path(a.params_file).read_text())
assert payload['cookies_str'] == 'a1=test; web_session=test'
Path(a.out).write_text(json.dumps({'namespace': a.namespace, 'method': a.method, 'result': {'ok': True, 'query': payload.get('query'), 'debug': 'prefix ' + payload['cookies_str'] + ' suffix', 'access_token': 'backend-token'}}))
""",
            encoding="utf-8",
        )
        return tool

    def test_calls_allowlisted_method_with_cookie_in_private_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = self._write_fake_tool(root)
            cookie = root / "cookie.txt"
            cookie.write_text("a1=test; web_session=test", encoding="utf-8")
            os.chmod(cookie, 0o600)
            request = root / "request.json"
            request.write_text(
                json.dumps(
                    {
                        "namespace": "pc",
                        "method": "search_note",
                        "params": {"query": "AI 论文", "page": 1},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            response = run_request(request, api_tool=tool, cookie_file=cookie)

            self.assertTrue(response["result"]["ok"])
            self.assertEqual(response["result"]["query"], "AI 论文")
            self.assertNotIn("cookies_str", json.dumps(response))
            serialized = json.dumps(response)
            self.assertNotIn("a1=test; web_session=test", serialized)
            self.assertNotIn("backend-token", serialized)
            self.assertEqual(response["result"]["access_token"], "[REDACTED]")
            self.assertEqual(response["result"]["debug"], "prefix [REDACTED] suffix")

    def test_rejects_write_method(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = root / "request.json"
            request.write_text(
                json.dumps(
                    {
                        "namespace": "creator",
                        "method": "post_note",
                        "params": {},
                    }
                ),
                encoding="utf-8",
            )
            cookie = root / "cookie.txt"
            cookie.write_text("secret", encoding="utf-8")
            os.chmod(cookie, 0o600)

            with self.assertRaisesRegex(AdapterError, "not allowlisted"):
                run_request(request, api_tool=root / "unused.py", cookie_file=cookie)

    def test_rejects_unbounded_all_publish_method(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = root / "request.json"
            request.write_text(
                json.dumps(
                    {
                        "namespace": "creator",
                        "method": "get_all_publish_note_info",
                        "params": {},
                    }
                ),
                encoding="utf-8",
            )
            cookie = root / "cookie.txt"
            cookie.write_text("secret", encoding="utf-8")
            os.chmod(cookie, 0o600)

            with self.assertRaisesRegex(AdapterError, "not allowlisted"):
                run_request(request, api_tool=root / "unused.py", cookie_file=cookie)

    def test_requires_query_and_rejects_boolean_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cookie = root / "cookie.txt"
            cookie.write_text("secret", encoding="utf-8")
            os.chmod(cookie, 0o600)
            request = root / "request.json"
            request.write_text(
                json.dumps(
                    {
                        "namespace": "pc",
                        "method": "search_note",
                        "params": {"page": True},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(AdapterError, "page|query"):
                run_request(request, api_tool=root / "unused.py", cookie_file=cookie)

            for unsafe_url, message in [
                (
                    "https://user:pass@xiaohongshu.com/explore/note-id",
                    "credentials",
                ),
                (
                    "https://www.xiaohongshu.com/explore/note-id?access_token=value",
                    "credential-like query",
                ),
                (
                    "https://www.xiaohongshu.com/explore/note-id#fragment",
                    "fragment",
                ),
            ]:
                request.write_text(
                    json.dumps(
                        {
                            "namespace": "pc",
                            "method": "get_note_info",
                            "params": {"url": unsafe_url},
                        }
                    ),
                    encoding="utf-8",
                )
                with (
                    self.subTest(url=unsafe_url),
                    self.assertRaisesRegex(AdapterError, message),
                ):
                    run_request(
                        request, api_tool=root / "unused.py", cookie_file=cookie
                    )

    def test_rejects_secret_alias_and_cookie_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = root / "request.json"
            request.write_text(
                json.dumps(
                    {
                        "namespace": "pc",
                        "method": "search_note",
                        "params": {"query": "test"},
                        "access-token": "sensitive",
                    }
                ),
                encoding="utf-8",
            )
            cookie_target = root / "cookie-target.txt"
            cookie_target.write_text("secret", encoding="utf-8")
            cookie_target.chmod(0o600)
            cookie_link = root / "cookie-link.txt"
            cookie_link.symlink_to(cookie_target)

            with self.assertRaisesRegex(AdapterError, "secret-like field"):
                run_request(
                    request, api_tool=root / "unused.py", cookie_file=cookie_link
                )

            request.write_text(
                json.dumps(
                    {
                        "namespace": "pc",
                        "method": "search_note",
                        "params": {"query": "test"},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AdapterError, "regular, non-symlink"):
                run_request(
                    request, api_tool=root / "unused.py", cookie_file=cookie_link
                )

    def test_rejects_invalid_utf8_request_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = root / "request.json"
            request.write_bytes(b"\xff\xfeinvalid-json")
            cookie = root / "cookie.txt"
            cookie.write_text("synthetic", encoding="utf-8")
            os.chmod(cookie, 0o600)
            with self.assertRaisesRegex(AdapterError, "Cannot read JSON"):
                run_request(request, api_tool=root / "unused.py", cookie_file=cookie)

    def test_rejects_cookie_file_readable_by_group_or_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = root / "request.json"
            request.write_text(
                json.dumps(
                    {
                        "namespace": "pc",
                        "method": "search_note",
                        "params": {"query": "permission-test"},
                    }
                ),
                encoding="utf-8",
            )
            cookie = root / "cookie.txt"
            cookie.write_text("secret", encoding="utf-8")
            os.chmod(cookie, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)

            with self.assertRaisesRegex(AdapterError, "mode 600"):
                run_request(request, api_tool=root / "unused.py", cookie_file=cookie)


if __name__ == "__main__":
    unittest.main()
