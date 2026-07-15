#!/usr/bin/env python3
"""One-shot XHS image publisher with approval binding and read-back verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.xhs_readonly_adapter import (
        MAX_RESPONSE_BYTES,
        AdapterError,
        _load_object,
        _read_cookie,
    )
    from scripts.xhs_workflow import (
        WorkflowError,
        _load_json,
        _write_json,
        _write_json_exclusive,
        record_publication,
        verify_approval,
    )
except ModuleNotFoundError:  # Direct execution through an absolute script path.
    from xhs_readonly_adapter import (  # type: ignore[no-redef]
        MAX_RESPONSE_BYTES,
        AdapterError,
        _load_object,
        _read_cookie,
    )
    from xhs_workflow import (  # type: ignore[no-redef]
        WorkflowError,
        _load_json,
        _write_json,
        _write_json_exclusive,
        record_publication,
        verify_approval,
    )

MAX_ASSET_BYTES = 30 * 1024 * 1024


class PublishError(ValueError):
    """Raised when a one-shot publication cannot be safely completed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_tool(api_tool: str | Path) -> Path:
    try:
        tool = Path(api_tool).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise PublishError(f"Cannot resolve XhsSkills api tool: {api_tool}") from exc
    if not tool.is_file():
        raise PublishError("api tool must be a regular file")
    return tool


def _resolve_python(api_python: str | Path) -> Path:
    try:
        executable = Path(api_python).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise PublishError(f"Cannot resolve backend Python: {api_python}") from exc
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise PublishError("backend Python must be an executable regular file")
    return executable


def _read_asset(
    package_root: Path, relative_path: str, expected: dict[str, Any]
) -> bytes:
    if "\x00" in relative_path:
        raise PublishError("asset path contains a NUL byte")
    relative = Path(relative_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise PublishError(f"asset escapes package directory: {relative_path}")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | nofollow | cloexec
    file_flags = os.O_RDONLY | nofollow | cloexec
    descriptors: list[int] = []
    try:
        current = os.open(package_root, directory_flags)
        descriptors.append(current)
        for part in relative.parts[:-1]:
            current = os.open(part, directory_flags, dir_fd=current)
            descriptors.append(current)
        descriptor = os.open(relative.parts[-1], file_flags, dir_fd=current)
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PublishError(f"asset is not a regular file: {relative_path}")
        if before.st_size > MAX_ASSET_BYTES:
            raise PublishError(
                f"asset exceeds {MAX_ASSET_BYTES} bytes: {relative_path}"
            )
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_ASSET_BYTES:
                raise PublishError(
                    f"asset exceeds {MAX_ASSET_BYTES} bytes: {relative_path}"
                )
            digest.update(chunk)
            chunks.append(chunk)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after:
            raise PublishError(
                f"asset changed while being snapshotted: {relative_path}"
            )
        if digest.hexdigest() != expected.get("sha256") or size != expected.get(
            "size_bytes"
        ):
            raise PublishError(f"asset no longer matches manifest: {relative_path}")
        return b"".join(chunks)
    except PublishError:
        raise
    except OSError as exc:
        raise PublishError(
            f"asset path is missing, unsafe, or contains a symlink: {relative_path}"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _snapshot_assets(manifest: dict[str, Any], snapshot_root: Path) -> list[str]:
    package_root = Path(manifest["package_root"]).resolve(strict=True)
    paths: list[str] = []
    for asset in manifest["assets"]:
        relative = asset["path"]
        data = _read_asset(package_root, relative, asset)
        suffix = Path(relative).suffix.lower()
        destination = snapshot_root / f"{asset['index']:02d}{suffix}"
        destination.write_bytes(data)
        os.chmod(destination, 0o600)
        paths.append(str(destination))
    return paths


def _call_backend(
    api_python: Path,
    tool: Path,
    method: str,
    payload: dict[str, Any],
    temporary_root: Path,
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    params_path = temporary_root / f"{method}-{uuid.uuid4().hex}.json"
    output_path = temporary_root / f"{method}-{uuid.uuid4().hex}-out.json"
    params_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.chmod(params_path, 0o600)
    try:
        completed = subprocess.run(  # noqa: S603 -- fixed argv, reviewed local backend
            [
                str(api_python),
                str(tool),
                "call",
                "creator",
                method,
                "--params-file",
                str(params_path),
                "--out",
                str(output_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishError(
            "XhsSkills backend timed out; publication outcome is unknown"
        ) from exc
    if completed.returncode != 0:
        raise PublishError(
            f"XhsSkills backend exited {completed.returncode}; publication outcome is unknown"
        )
    try:
        return _load_object(output_path, max_bytes=MAX_RESPONSE_BYTES)
    except AdapterError as exc:
        raise PublishError("XhsSkills backend returned an invalid response") from exc


def _api_result(response: dict[str, Any], method: str) -> dict[str, Any]:
    if response.get("namespace") != "creator" or response.get("method") != method:
        raise PublishError(f"backend response is not bound to creator.{method}")
    result = response.get("result")
    if not isinstance(result, list) or len(result) != 3:
        raise PublishError(f"creator.{method} returned an invalid result tuple")
    success, _message, body = result
    if success is not True:
        raise PublishError(f"creator.{method} was rejected by the backend")
    if not isinstance(body, dict) or body.get("success") is not True:
        raise PublishError(
            f"creator.{method} did not return a successful response body"
        )
    return body


def _post_note_id(body: dict[str, Any]) -> str:
    data = body.get("data")
    if not isinstance(data, dict):
        raise PublishError("post response does not contain data")
    note_id = data.get("note_id") or data.get("noteId") or data.get("id")
    if not isinstance(note_id, str) or not note_id.strip():
        raise PublishError(
            "post response does not contain a note ID; outcome is unknown"
        )
    return note_id.strip()


def _readback_matches(body: dict[str, Any], note_id: str, title: str) -> bool:
    data = body.get("data")
    notes = data.get("notes") if isinstance(data, dict) else None
    if not isinstance(notes, list):
        raise PublishError("creator publication readback does not contain a notes list")
    for note in notes:
        if not isinstance(note, dict):
            continue
        candidate_id = note.get("note_id") or note.get("noteId") or note.get("id")
        candidate_title = note.get("title") or note.get("note_title")
        if candidate_id == note_id and candidate_title == title:
            return True
    return False


def _safe_output_path(path: str | Path, package_root: Path, label: str) -> Path:
    candidate = Path(path)
    try:
        parent = candidate.parent.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise PublishError(f"{label} parent directory is invalid") from exc
    if parent != package_root:
        raise PublishError(
            f"{label} must be created directly inside the package directory"
        )
    return candidate


def publish_once(
    manifest_path: str | Path,
    approval_path: str | Path,
    attempt_path: str | Path,
    record_path: str | Path,
    *,
    api_tool: str | Path,
    cookie_file: str | Path,
    api_python: str | Path = sys.executable,
    timeout_seconds: int = 300,
    verification_attempts: int = 6,
    verification_delay_seconds: int = 5,
) -> dict[str, Any]:
    """Publish exactly once after a current manifest-bound user authorization."""
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
        raise PublishError("timeout_seconds must be an integer from 1 to 600")
    if type(verification_attempts) is not int or not 1 <= verification_attempts <= 20:
        raise PublishError("verification_attempts must be an integer from 1 to 20")
    if (
        type(verification_delay_seconds) is not int
        or not 0 <= verification_delay_seconds <= 60
    ):
        raise PublishError("verification_delay_seconds must be an integer from 0 to 60")

    approval = verify_approval(manifest_path, approval_path)
    if not approval["valid"]:
        raise PublishError(
            "publication approval is invalid: " + "; ".join(approval["errors"])
        )
    manifest = _load_json(Path(manifest_path))
    post = manifest["post"]
    if post.get("visibility") != "public":
        raise PublishError(
            "automatic publisher currently supports public visibility only"
        )
    if post.get("publish_mode") != "immediate":
        raise PublishError("automatic publisher requires publish_mode='immediate'")

    package_root = Path(manifest["package_root"]).resolve(strict=True)
    attempt_destination = _safe_output_path(attempt_path, package_root, "attempt file")
    record_destination = _safe_output_path(record_path, package_root, "record file")
    if record_destination.exists() or record_destination.is_symlink():
        raise PublishError("publication record already exists")
    cookie = _read_cookie(Path(cookie_file))
    tool = _resolve_tool(api_tool)
    backend_python = _resolve_python(api_python)

    with tempfile.TemporaryDirectory(prefix="xhs-publish-") as temporary:
        temporary_root = Path(temporary)
        snapshot_root = temporary_root / "assets"
        snapshot_root.mkdir(mode=0o700)
        image_paths = _snapshot_assets(manifest, snapshot_root)
        attempt = {
            "schema_version": 1,
            "attempt_id": str(uuid.uuid4()),
            "manifest_id": manifest["manifest_id"],
            "content_hash": manifest["content_hash"],
            "approval_id": approval["approval_id"],
            "status": "started",
            "started_at": _utc_now(),
            "backend": "xhsskills_creator_post_note",
        }
        try:
            _write_json_exclusive(attempt_destination, attempt, mode=0o600)
        except WorkflowError as exc:
            raise PublishError(
                "publication attempt already exists; do not retry before reconciliation"
            ) from exc
        payload = {
            "noteInfo": {
                "title": post["title"],
                "desc": post["content"],
                "postTime": None,
                "location": None,
                "type": 0,
                "media_type": "image",
                "topics": post["topics"],
                "images": image_paths,
            },
            "cookies_str": cookie,
        }
        try:
            post_response = _call_backend(
                backend_python,
                tool,
                "post_note",
                payload,
                temporary_root,
                timeout_seconds=timeout_seconds,
            )
            note_id = _post_note_id(_api_result(post_response, "post_note"))
            attempt["status"] = "backend_accepted"
            attempt["note_id"] = note_id
            attempt["backend_accepted_at"] = _utc_now()
            _write_json(attempt_destination, attempt, mode=0o600)
        except (PublishError, OSError, WorkflowError) as exc:
            attempt["status"] = "outcome_unknown"
            attempt["failed_at"] = _utc_now()
            attempt["failure"] = type(exc).__name__
            _write_json(attempt_destination, attempt, mode=0o600)
            raise

        verified = False
        try:
            for index in range(verification_attempts):
                readback = _call_backend(
                    backend_python,
                    tool,
                    "get_publish_note_info",
                    {"page": 1, "cookies_str": cookie},
                    temporary_root,
                    timeout_seconds=timeout_seconds,
                )
                body = _api_result(readback, "get_publish_note_info")
                if _readback_matches(body, note_id, post["title"]):
                    verified = True
                    break
                if index + 1 < verification_attempts:
                    time.sleep(verification_delay_seconds)
        except (PublishError, OSError) as exc:
            attempt["status"] = "outcome_unknown"
            attempt["failed_at"] = _utc_now()
            attempt["failure"] = type(exc).__name__
            _write_json(attempt_destination, attempt, mode=0o600)
            raise
        if not verified:
            attempt["status"] = "outcome_unknown"
            attempt["failed_at"] = _utc_now()
            attempt["failure"] = "readback_not_found"
            _write_json(attempt_destination, attempt, mode=0o600)
            raise PublishError(
                "post request was accepted but the note was not found in creator readback; "
                "do not retry automatically"
            )

        published_at = attempt["backend_accepted_at"]
        verified_at = _utc_now()
        result_path = temporary_root / "result.json"
        _write_json(
            result_path,
            {
                "platform": "xiaohongshu",
                "note_id": note_id,
                "url": f"https://www.xiaohongshu.com/explore/{note_id}",
                "published_at": published_at,
                "verification": {
                    "method": "creator_api_readback",
                    "verified_by": "agent",
                    "verified_at": verified_at,
                },
            },
            mode=0o600,
        )
        record = record_publication(
            manifest_path, approval_path, result_path, record_destination
        )
        attempt["status"] = "verified"
        attempt["verified_at"] = verified_at
        attempt["publication_id"] = record["publication_id"]
        _write_json(attempt_destination, attempt, mode=0o600)
        return record


def reconcile_attempt(
    manifest_path: str | Path,
    approval_path: str | Path,
    attempt_path: str | Path,
    record_path: str | Path,
    *,
    api_tool: str | Path,
    cookie_file: str | Path,
    api_python: str | Path = sys.executable,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Reconcile an uncertain attempt using read-only creator history; never republish."""
    approval = verify_approval(manifest_path, approval_path)
    if not approval["valid"]:
        raise PublishError(
            "publication approval is invalid: " + "; ".join(approval["errors"])
        )
    manifest = _load_json(Path(manifest_path))
    package_root = Path(manifest["package_root"]).resolve(strict=True)
    attempt_source = _safe_output_path(attempt_path, package_root, "attempt file")
    record_destination = _safe_output_path(record_path, package_root, "record file")
    try:
        attempt_stat = attempt_source.lstat()
    except OSError as exc:
        raise PublishError("publication attempt does not exist") from exc
    if stat.S_ISLNK(attempt_stat.st_mode) or not stat.S_ISREG(attempt_stat.st_mode):
        raise PublishError("attempt file must be a regular, non-symlink file")
    if stat.S_IMODE(attempt_stat.st_mode) != 0o600:
        raise PublishError("attempt file must have mode 600")
    attempt = _load_json(attempt_source)
    if attempt.get("manifest_id") != manifest["manifest_id"]:
        raise PublishError("attempt manifest_id mismatch")
    if attempt.get("content_hash") != manifest["content_hash"]:
        raise PublishError("attempt content_hash mismatch")
    if attempt.get("approval_id") != approval["approval_id"]:
        raise PublishError("attempt approval_id mismatch")
    if attempt.get("status") not in {"backend_accepted", "outcome_unknown"}:
        raise PublishError("attempt is not eligible for reconciliation")
    note_id = attempt.get("note_id")
    if not isinstance(note_id, str) or not note_id.strip():
        raise PublishError(
            "attempt has no note_id; automatic reconciliation is unavailable"
        )
    published_at = attempt.get("backend_accepted_at")
    if not isinstance(published_at, str):
        raise PublishError("attempt has no backend_accepted_at timestamp")
    if record_destination.exists() or record_destination.is_symlink():
        raise PublishError("publication record already exists")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
        raise PublishError("timeout_seconds must be an integer from 1 to 600")

    cookie = _read_cookie(Path(cookie_file))
    tool = _resolve_tool(api_tool)
    backend_python = _resolve_python(api_python)
    with tempfile.TemporaryDirectory(prefix="xhs-reconcile-") as temporary:
        readback = _call_backend(
            backend_python,
            tool,
            "get_publish_note_info",
            {"page": 1, "cookies_str": cookie},
            Path(temporary),
            timeout_seconds=timeout_seconds,
        )
        body = _api_result(readback, "get_publish_note_info")
        if not _readback_matches(body, note_id.strip(), manifest["post"]["title"]):
            raise PublishError(
                "note is still absent from creator readback; do not retry publication"
            )
        verified_at = _utc_now()
        result_path = Path(temporary) / "result.json"
        _write_json(
            result_path,
            {
                "platform": "xiaohongshu",
                "note_id": note_id.strip(),
                "url": f"https://www.xiaohongshu.com/explore/{note_id.strip()}",
                "published_at": published_at,
                "verification": {
                    "method": "creator_api_readback",
                    "verified_by": "agent",
                    "verified_at": verified_at,
                },
            },
            mode=0o600,
        )
        record = record_publication(
            manifest_path, approval_path, result_path, record_destination
        )
    attempt["status"] = "verified"
    attempt["verified_at"] = verified_at
    attempt["publication_id"] = record["publication_id"]
    _write_json(attempt_source, attempt, mode=0o600)
    return record


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--approval", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--record", required=True)
    parser.add_argument("--api-tool", default=os.environ.get("XHS_API_TOOL"))
    parser.add_argument(
        "--api-python",
        default=os.environ.get("XHS_API_PYTHON", sys.executable),
        help="Python executable for the isolated XhsSkills environment",
    )
    parser.add_argument("--cookie-file", default=os.environ.get("XHS_COOKIE_FILE"))
    parser.add_argument("--timeout-seconds", type=int, default=300)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    publish = subparsers.add_parser("publish", help="Perform one publication attempt")
    _add_common_arguments(publish)
    publish.add_argument("--verification-attempts", type=int, default=6)
    publish.add_argument("--verification-delay-seconds", type=int, default=5)
    reconcile = subparsers.add_parser(
        "reconcile", help="Read back an uncertain attempt without republishing"
    )
    _add_common_arguments(reconcile)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.api_tool or not args.cookie_file:
        print(
            json.dumps(
                {"ok": False, "error": "XHS_API_TOOL and XHS_COOKIE_FILE are required"}
            )
        )
        return 2
    try:
        if args.command == "publish":
            result = publish_once(
                args.manifest,
                args.approval,
                args.attempt,
                args.record,
                api_tool=args.api_tool,
                api_python=args.api_python,
                cookie_file=args.cookie_file,
                timeout_seconds=args.timeout_seconds,
                verification_attempts=args.verification_attempts,
                verification_delay_seconds=args.verification_delay_seconds,
            )
        else:
            result = reconcile_attempt(
                args.manifest,
                args.approval,
                args.attempt,
                args.record,
                api_tool=args.api_tool,
                api_python=args.api_python,
                cookie_file=args.cookie_file,
                timeout_seconds=args.timeout_seconds,
            )
    except (AdapterError, OSError, PublishError, WorkflowError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
