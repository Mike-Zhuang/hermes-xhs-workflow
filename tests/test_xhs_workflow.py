import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts import xhs_workflow as workflow_module
from scripts.xhs_workflow import (
    approve_manifest,
    build_preview,
    main,
    prepare_manifest,
    record_publication,
    validate_manifest,
    verify_approval,
)

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def utc_minutes_ago(minutes: int) -> str:
    value = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return value.isoformat().replace("+00:00", "Z")


class XHSWorkflowTests(unittest.TestCase):
    def test_exclusive_output_fdopen_failure_does_not_leak_descriptor(self):
        if not Path("/proc/self/fd").is_dir():
            self.skipTest("descriptor accounting is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "exclusive.json"
            descriptors_before = len(os.listdir("/proc/self/fd"))

            with (
                patch.object(
                    workflow_module.os,
                    "fdopen",
                    side_effect=OSError("injected exclusive fdopen failure"),
                ),
                self.assertRaisesRegex(OSError, "exclusive fdopen failure"),
            ):
                workflow_module._write_json_exclusive(destination, {"ok": True})

            self.assertFalse(destination.exists())
            self.assertEqual(len(os.listdir("/proc/self/fd")), descriptors_before)

    def test_manifest_and_approval_outputs_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "01.png").write_bytes(PNG_HEADER + b"exclusive-output")
            source = root / "post.json"
            source.write_text(
                json.dumps(
                    {
                        "title": "不可覆盖",
                        "content": "TL;DR：输出必须独占创建。",
                        "images": ["01.png"],
                        "publish_mode": "immediate",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            manifest.write_text("manifest-sentinel", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "exists|already"):
                prepare_manifest(source, manifest)
            self.assertEqual(manifest.read_text(encoding="utf-8"), "manifest-sentinel")

            manifest.unlink()
            prepared = prepare_manifest(source, manifest)
            approval = root / "approval.json"
            approval.write_text("approval-sentinel", encoding="utf-8")
            approval.chmod(0o600)

            with self.assertRaisesRegex(ValueError, "exists|already"):
                approve_manifest(
                    manifest,
                    approval,
                    confirmed_by="user",
                    confirmed_content_hash=prepared["content_hash"],
                )
            self.assertEqual(approval.read_text(encoding="utf-8"), "approval-sentinel")

    def test_prepare_creates_valid_manifest_with_content_and_image_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "01.png"
            image.write_bytes(PNG_HEADER + b"fake-png-content")
            source = root / "post.json"
            source.write_text(
                json.dumps(
                    {
                        "title": "一篇论文精读",
                        "content": "TL;DR：核心结论。",
                        "images": ["01.png"],
                        "topics": ["AI", "论文"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manifest_path = root / "manifest.json"

            manifest = prepare_manifest(source, manifest_path)
            report = validate_manifest(manifest_path)

            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["status"], "prepared")
            self.assertEqual(manifest["post"]["title"], "一篇论文精读")
            self.assertEqual(len(manifest["assets"]), 1)
            self.assertEqual(len(manifest["assets"][0]["sha256"]), 64)
            self.assertEqual(len(manifest["content_hash"]), 64)
            self.assertTrue(report["valid"])
            self.assertEqual(report["content_hash"], manifest["content_hash"])

    def test_direct_publish_command_is_recorded_as_authorization_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "01.png").write_bytes(PNG_HEADER + b"direct-command")
            source = root / "post.json"
            source.write_text(
                json.dumps(
                    {
                        "title": "直接发布",
                        "content": "TL;DR：用户指令本身构成一次授权。",
                        "images": ["01.png"],
                        "publish_mode": "immediate",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            approval = root / "approval.json"
            prepared = prepare_manifest(source, manifest)

            approved = approve_manifest(
                manifest,
                approval,
                confirmed_by="user",
                confirmed_content_hash=prepared["content_hash"],
                authorization_mode="direct_publish_command",
            )

            self.assertEqual(approved["authorization_mode"], "direct_publish_command")
            self.assertTrue(verify_approval(manifest, approval)["valid"])

    def test_malformed_manifest_reports_invalid_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "manifest_id": "broken",
                        "package_root": tmp,
                        "post": {},
                        "assets": None,
                        "content_hash": "invalid",
                    }
                ),
                encoding="utf-8",
            )

            report = validate_manifest(manifest)

            self.assertFalse(report["valid"])
            self.assertTrue(any("assets" in error for error in report["errors"]))

            malformed = json.loads(manifest.read_text(encoding="utf-8"))
            malformed["package_root"] = "bad\u0000root"
            manifest.write_text(json.dumps(malformed), encoding="utf-8")
            nul_report = validate_manifest(manifest)
            self.assertFalse(nul_report["valid"])
            self.assertTrue(
                any(
                    "package_root is invalid" in error for error in nul_report["errors"]
                )
            )

            manifest.write_bytes(b"\xff\xfeinvalid-json")
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["validate", "--manifest", str(manifest)])
            self.assertEqual(exit_code, 2)
            self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_prepare_rejects_mislabeled_image_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "01.png").write_bytes(b"this-is-not-a-png")
            source = root / "post.json"
            source.write_text(
                json.dumps(
                    {
                        "title": "文件签名",
                        "content": "TL;DR：拒绝伪装成图片的文件。",
                        "images": ["01.png"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "signature"):
                prepare_manifest(source, root / "manifest.json")

    def test_prepare_rejects_symlinked_path_components(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as outside,
        ):
            root = Path(tmp)
            outside_root = Path(outside)
            (outside_root / "01.png").write_bytes(PNG_HEADER + b"content")
            (root / "linked").symlink_to(outside_root, target_is_directory=True)
            source = root / "post.json"
            source.write_text(
                json.dumps(
                    {
                        "title": "路径安全",
                        "content": "TL;DR：拒绝目录 symlink。",
                        "images": ["linked/01.png"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "symlink|escapes"):
                prepare_manifest(source, root / "manifest.json")

    def test_cli_returns_json_error_for_missing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "prepare",
                        "--source",
                        str(Path(tmp) / "missing-post.json"),
                        "--manifest",
                        str(Path(tmp) / "unused-manifest.json"),
                    ]
                )
            response = json.loads(output.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertFalse(response["ok"])

    def test_prepare_rejects_more_than_nine_images_for_this_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = []
            for index in range(10):
                name = f"{index:02d}.png"
                (root / name).write_bytes(PNG_HEADER + str(index).encode())
                images.append(name)
            source = root / "post.json"
            source.write_text(
                json.dumps(
                    {
                        "title": "图片上限",
                        "content": "TL;DR：防止误发过多图片。",
                        "images": images,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "at most 9"):
                prepare_manifest(source, root / "manifest.json")

    def test_prepare_rejects_secret_fields_at_any_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "01.png").write_bytes(PNG_HEADER + b"fake-png-content")
            source = root / "post.json"
            source.write_text(
                json.dumps(
                    {
                        "title": "安全测试",
                        "content": "不会写入凭据。",
                        "images": ["01.png"],
                        "metadata": {"cookies_str": "sensitive-value"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "secret-like field"):
                prepare_manifest(source, root / "manifest.json")

    def test_prepare_rejects_secret_aliases_and_excessive_nesting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "01.png").write_bytes(PNG_HEADER + b"content")
            source = root / "post.json"
            source.write_text(
                json.dumps(
                    {
                        "title": "别名凭据",
                        "content": "TL;DR：拒绝常见凭据别名。",
                        "images": ["01.png"],
                        "metadata": {"access-token": "sensitive-value"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "secret-like field"):
                prepare_manifest(source, root / "manifest.json")

            nested: dict[str, object] = {
                "title": "深度限制",
                "content": "TL;DR：拒绝过深 JSON。",
                "images": ["01.png"],
            }
            cursor = nested
            for _ in range(40):
                child: dict[str, object] = {}
                cursor["metadata"] = child
                cursor = child
            source.write_text(json.dumps(nested, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "nesting"):
                prepare_manifest(source, root / "manifest.json")

    def test_approval_is_bound_to_a_valid_manifest_and_written_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "01.png").write_bytes(PNG_HEADER + b"fake-png-content")
            source = root / "post.json"
            source.write_text(
                json.dumps(
                    {
                        "title": "确认发布测试",
                        "content": "TL;DR：先确认，再发布。",
                        "images": ["01.png"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manifest_path = root / "manifest.json"
            approval_path = root / "approval.json"
            manifest_data = prepare_manifest(source, manifest_path)

            approval = approve_manifest(
                manifest_path,
                approval_path,
                ttl_seconds=600,
                confirmed_by="user",
                confirmed_content_hash=manifest_data["content_hash"],
            )
            report = verify_approval(manifest_path, approval_path)

            self.assertEqual(approval["confirmed_by"], "user")
            self.assertEqual(approval_path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(report["valid"])

    def test_approval_requires_exact_confirmed_content_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "01.png").write_bytes(PNG_HEADER + b"content")
            source = root / "post.json"
            source.write_text(
                json.dumps(
                    {
                        "title": "确认哈希",
                        "content": "TL;DR：确认值必须精确匹配。",
                        "images": ["01.png"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            prepare_manifest(source, manifest)

            with self.assertRaisesRegex(ValueError, "confirmed content hash"):
                approve_manifest(
                    manifest,
                    root / "approval.json",
                    ttl_seconds=600,
                    confirmed_by="user",
                    confirmed_content_hash="0" * 64,
                )

    def test_forged_approval_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "01.png").write_bytes(PNG_HEADER + b"content")
            source = root / "post.json"
            source.write_text(
                json.dumps(
                    {
                        "title": "伪造确认",
                        "content": "TL;DR：伪造元数据不能通过。",
                        "images": ["01.png"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manifest_path = root / "manifest.json"
            manifest = prepare_manifest(source, manifest_path)
            approval_path = root / "approval.json"
            approval_path.write_text(
                json.dumps(
                    {
                        "schema_version": 999,
                        "approval_id": "not-a-uuid",
                        "manifest_id": manifest["manifest_id"],
                        "content_hash": manifest["content_hash"],
                        "confirmed_content_hash": manifest["content_hash"],
                        "confirmed_by": "user",
                        "issued_at": "2099-01-01T00:00:00Z",
                        "expires_at": "2099-01-02T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            approval_path.chmod(0o600)

            report = verify_approval(manifest_path, approval_path)

            self.assertFalse(report["valid"])
            self.assertTrue(any("schema_version" in item for item in report["errors"]))
            self.assertTrue(any("approval_id" in item for item in report["errors"]))
            self.assertTrue(any("future" in item for item in report["errors"]))

    def test_expired_approval_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "01.png").write_bytes(PNG_HEADER + b"content")
            source = root / "post.json"
            source.write_text(
                json.dumps(
                    {
                        "title": "过期确认",
                        "content": "TL;DR：过期后必须重新确认。",
                        "images": ["01.png"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            approval_path = root / "approval.json"
            manifest_data = prepare_manifest(source, manifest)
            approval = approve_manifest(
                manifest,
                approval_path,
                ttl_seconds=600,
                confirmed_by="user",
                confirmed_content_hash=manifest_data["content_hash"],
            )
            approval["expires_at"] = "2000-01-01T00:00:00Z"
            approval_path.write_text(json.dumps(approval), encoding="utf-8")

            report = verify_approval(manifest, approval_path)

            self.assertFalse(report["valid"])
            self.assertIn("approval expired", report["errors"])

    def test_record_publication_requires_valid_approval_and_real_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "01.png"
            image.write_bytes(PNG_HEADER + b"fake-png-content")
            source = root / "post.json"
            source.write_text(
                json.dumps(
                    {
                        "title": "发布记录测试",
                        "content": "TL;DR：记录真实发布结果。",
                        "images": ["01.png"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manifest_path = root / "manifest.json"
            approval_path = root / "approval.json"
            result_path = root / "result-input.json"
            record_path = root / "publication.json"
            manifest_data = prepare_manifest(source, manifest_path)
            approve_manifest(
                manifest_path,
                approval_path,
                ttl_seconds=600,
                confirmed_by="user",
                confirmed_content_hash=manifest_data["content_hash"],
            )
            result_path.write_text(
                json.dumps(
                    {
                        "platform": "xiaohongshu",
                        "note_id": "note-123",
                        "url": "https://www.xiaohongshu.com/explore/note-123",
                        "published_at": "2999-07-15T12:00:00Z",
                        "verification": {
                            "method": "official_note_page",
                            "verified_by": "user",
                            "verified_at": "2999-07-15T12:05:00Z",
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "future"):
                record_publication(
                    manifest_path, approval_path, result_path, record_path
                )
            result_path.write_text(
                json.dumps(
                    {
                        "platform": "xiaohongshu",
                        "note_id": "note-123",
                        "url": "https://www.xiaohongshu.com/explore/note-123",
                        "published_at": utc_minutes_ago(10),
                        "verification": {
                            "method": "official_note_page",
                            "verified_by": "user",
                            "verified_at": utc_minutes_ago(9),
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "approval window"):
                record_publication(
                    manifest_path, approval_path, result_path, record_path
                )
            result_path.write_text(
                json.dumps(
                    {
                        "platform": "xiaohongshu",
                        "note_id": "note-123",
                        "url": "https://www.xiaohongshu.com/explore/note-123",
                        "published_at": utc_minutes_ago(2),
                        "verification": {
                            "method": "official_note_page",
                            "verified_by": "user",
                            "verified_at": utc_minutes_ago(1),
                        },
                    }
                ),
                encoding="utf-8",
            )

            record = record_publication(
                manifest_path, approval_path, result_path, record_path
            )

            self.assertEqual(record["status"], "publication_recorded")
            self.assertEqual(record["note_id"], "note-123")
            self.assertEqual(record_path.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(ValueError, "already exists"):
                record_publication(
                    manifest_path, approval_path, result_path, record_path
                )

    def test_asset_change_invalidates_existing_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "01.png"
            image.write_bytes(PNG_HEADER + b"before")
            source = root / "post.json"
            source.write_text(
                json.dumps(
                    {
                        "title": "变更检测",
                        "content": "TL;DR：图片变化后必须重新确认。",
                        "images": ["01.png"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            approval = root / "approval.json"
            manifest_data = prepare_manifest(source, manifest)
            approve_manifest(
                manifest,
                approval,
                ttl_seconds=600,
                confirmed_by="user",
                confirmed_content_hash=manifest_data["content_hash"],
            )

            image.write_bytes(PNG_HEADER + b"after")
            report = verify_approval(manifest, approval)

            self.assertFalse(report["valid"])
            self.assertTrue(any("asset" in error for error in report["errors"]))

    def test_record_rejects_note_id_that_does_not_match_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "01.png").write_bytes(PNG_HEADER + b"content")
            source = root / "post.json"
            source.write_text(
                json.dumps(
                    {
                        "title": "结果绑定",
                        "content": "TL;DR：URL 必须对应 note_id。",
                        "images": ["01.png"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            approval = root / "approval.json"
            result = root / "result.json"
            manifest_data = prepare_manifest(source, manifest)
            approve_manifest(
                manifest,
                approval,
                ttl_seconds=600,
                confirmed_by="user",
                confirmed_content_hash=manifest_data["content_hash"],
            )
            for unsafe_url, message in [
                (
                    "https://user:pass@xiaohongshu.com/explore/expected-note",
                    "credentials",
                ),
                (
                    "https://www.xiaohongshu.com/explore/expected-note?access_token=value",
                    "credential-like query",
                ),
                (
                    "https://www.xiaohongshu.com/explore/expected-note#fragment",
                    "fragment",
                ),
            ]:
                result.write_text(
                    json.dumps(
                        {
                            "platform": "xiaohongshu",
                            "note_id": "expected-note",
                            "url": unsafe_url,
                            "published_at": utc_minutes_ago(2),
                            "verification": {
                                "method": "official_note_page",
                                "verified_by": "user",
                                "verified_at": utc_minutes_ago(1),
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                with (
                    self.subTest(url=unsafe_url),
                    self.assertRaisesRegex(ValueError, message),
                ):
                    record_publication(
                        manifest, approval, result, root / "publication.json"
                    )
            result.write_text(
                json.dumps(
                    {
                        "platform": "xiaohongshu",
                        "note_id": "expected-note",
                        "url": "https://www.xiaohongshu.com/explore/different-note",
                        "published_at": utc_minutes_ago(2),
                        "verification": {
                            "method": "official_note_page",
                            "verified_by": "user",
                            "verified_at": utc_minutes_ago(1),
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "note_id"):
                record_publication(
                    manifest, approval, result, root / "publication.json"
                )

    def test_cli_prepare_and_validate_emit_machine_readable_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "01.png").write_bytes(PNG_HEADER + b"fake-png-content")
            source = root / "post.json"
            manifest_path = root / "manifest.json"
            source.write_text(
                json.dumps(
                    {
                        "title": "CLI 测试",
                        "content": "TL;DR：可被 Agent 稳定调用。",
                        "images": ["01.png"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            output = StringIO()

            with redirect_stdout(output):
                exit_code = main(
                    [
                        "prepare",
                        "--source",
                        str(source),
                        "--manifest",
                        str(manifest_path),
                    ]
                )

            response = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(response["status"], "prepared")
            self.assertTrue(manifest_path.exists())

    def test_cli_supports_validate_approve_verify_and_record_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "01.png").write_bytes(PNG_HEADER + b"fake-png-content")
            source = root / "post.json"
            manifest = root / "manifest.json"
            approval = root / "approval.json"
            result = root / "result.json"
            record = root / "publication.json"
            source.write_text(
                json.dumps(
                    {
                        "title": "CLI 端到端",
                        "content": "TL;DR：人工确认发布。",
                        "images": ["01.png"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result.write_text(
                json.dumps(
                    {
                        "platform": "xiaohongshu",
                        "note_id": "note-cli",
                        "url": "https://www.xiaohongshu.com/explore/note-cli",
                        "published_at": utc_minutes_ago(2),
                        "verification": {
                            "method": "official_note_page",
                            "verified_by": "user",
                            "verified_at": utc_minutes_ago(1),
                        },
                    }
                ),
                encoding="utf-8",
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(
                        [
                            "prepare",
                            "--source",
                            str(source),
                            "--manifest",
                            str(manifest),
                        ]
                    ),
                    0,
                )
            confirmed_hash = json.loads(manifest.read_text(encoding="utf-8"))[
                "content_hash"
            ]
            commands = [
                ["validate", "--manifest", str(manifest)],
                ["preview", "--manifest", str(manifest)],
                [
                    "approve",
                    "--manifest",
                    str(manifest),
                    "--approval",
                    str(approval),
                    "--confirmed-by",
                    "user",
                    "--confirmed-content-hash",
                    confirmed_hash,
                ],
                ["verify", "--manifest", str(manifest), "--approval", str(approval)],
                [
                    "record",
                    "--manifest",
                    str(manifest),
                    "--approval",
                    str(approval),
                    "--result",
                    str(result),
                    "--record",
                    str(record),
                ],
            ]

            for command in commands:
                with self.subTest(command=command[0]), redirect_stdout(StringIO()):
                    self.assertEqual(main(command), 0)

            self.assertTrue(record.exists())

    def test_preview_exposes_exact_content_hash_and_asset_order_for_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "01.png").write_bytes(PNG_HEADER + b"first")
            (root / "02.png").write_bytes(PNG_HEADER + b"second")
            source = root / "post.json"
            manifest_path = root / "manifest.json"
            source.write_text(
                json.dumps(
                    {
                        "title": "确认预览",
                        "content": "TL;DR：这是将要发布的原文。",
                        "images": ["01.png", "02.png"],
                        "topics": ["AI", "论文"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manifest = prepare_manifest(source, manifest_path)

            preview = build_preview(manifest_path)

            self.assertEqual(preview["title"], "确认预览")
            self.assertEqual(preview["content"], "TL;DR：这是将要发布的原文。")
            self.assertEqual(preview["images"], ["01.png", "02.png"])
            self.assertEqual(preview["topics"], ["AI", "论文"])
            self.assertEqual(preview["content_hash"], manifest["content_hash"])
            self.assertEqual(preview["image_count"], 2)


if __name__ == "__main__":
    unittest.main()
