#!/usr/bin/env python3
"""One-shot XHS image publisher with approval binding and read-back verification."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.xhs_readonly_adapter import (
        MAX_JSON_DEPTH,
        MAX_RESPONSE_BYTES,
        SECRET_KEYS,
        AdapterError,
        _load_object,
        _normalize_secret_key,
        _read_cookie,
    )
    from scripts.xhs_workflow import (
        WorkflowError,
        _format_utc,
        _load_json,
        _parse_utc,
        _write_json,
        _write_json_exclusive,
        load_verified_publication_inputs,
        record_publication_data,
    )
except ModuleNotFoundError:  # Direct execution through an absolute script path.
    from xhs_readonly_adapter import (  # type: ignore[no-redef]
        MAX_JSON_DEPTH,
        MAX_RESPONSE_BYTES,
        SECRET_KEYS,
        AdapterError,
        _load_object,
        _normalize_secret_key,
        _read_cookie,
    )
    from xhs_workflow import (  # type: ignore[no-redef]
        WorkflowError,
        _format_utc,
        _load_json,
        _parse_utc,
        _write_json,
        _write_json_exclusive,
        load_verified_publication_inputs,
        record_publication_data,
    )

MAX_ASSET_BYTES = 30 * 1024 * 1024


class PublishError(ValueError):
    """Raised when a one-shot publication cannot be safely completed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_tool(api_tool: str | Path) -> Path:
    tool = Path(api_tool)
    if not tool.is_absolute():
        raise PublishError("XhsSkills api tool path must be absolute")
    try:
        resolved = tool.resolve(strict=True)
        tool_stat = tool.lstat()
    except (OSError, ValueError) as exc:
        raise PublishError(f"Cannot resolve XhsSkills api tool: {api_tool}") from exc
    if Path(os.path.abspath(tool)) != tool:
        raise PublishError("api tool path must be fully canonical")
    if resolved != tool or stat.S_ISLNK(tool_stat.st_mode):
        raise PublishError("api tool path must not contain a symlink")
    if not stat.S_ISREG(tool_stat.st_mode):
        raise PublishError("api tool must be a regular file")
    return tool


def _resolve_python(api_python: str | Path | None) -> Path:
    if api_python is None:
        raise PublishError(
            "backend Python must be explicitly configured with XHS_API_PYTHON"
        )
    launcher = Path(api_python)
    if not launcher.is_absolute():
        raise PublishError("backend Python path must be absolute")
    try:
        launcher_stat = launcher.lstat()
        target = launcher.resolve(strict=True)
        target_stat = target.stat()
        venv_root = launcher.parent.parent.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise PublishError(f"Cannot resolve backend Python: {api_python}") from exc
    config = venv_root / "pyvenv.cfg"
    try:
        config_stat = config.lstat()
    except OSError as exc:
        raise PublishError(
            "backend Python must belong to an isolated virtual environment"
        ) from exc
    if not (stat.S_ISREG(launcher_stat.st_mode) or stat.S_ISLNK(launcher_stat.st_mode)):
        raise PublishError("backend Python launcher must be a regular file or symlink")
    if not stat.S_ISREG(target_stat.st_mode) or not os.access(target, os.X_OK):
        raise PublishError("backend Python target must be an executable regular file")
    if not stat.S_ISREG(config_stat.st_mode) or stat.S_ISLNK(config_stat.st_mode):
        raise PublishError(
            "backend Python must belong to an isolated virtual environment"
        )
    probe = (
        "import json,sys;"
        "print(json.dumps({'prefix':sys.prefix,'base_prefix':sys.base_prefix}))"
    )
    try:
        completed = subprocess.run(  # noqa: S603 -- validated absolute venv launcher
            [str(launcher), "-I", "-c", probe],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        runtime = json.loads(completed.stdout)
        runtime_prefix = Path(runtime["prefix"]).resolve(strict=True)
        base_prefix = Path(runtime["base_prefix"]).resolve(strict=True)
    except (
        OSError,
        KeyError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as exc:
        raise PublishError("cannot verify isolated backend Python runtime") from exc
    if runtime_prefix != venv_root or runtime_prefix == base_prefix:
        raise PublishError(
            "backend Python must run inside the configured virtual environment"
        )
    return launcher


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


def _snapshot_assets(
    manifest: dict[str, Any], snapshot_root: Path
) -> tuple[list[str], list[int]]:
    package_root = Path(manifest["package_root"]).resolve(strict=True)
    if Path("/proc/self/fd").is_dir():
        descriptor_root = "/proc/self/fd"
    elif Path("/dev/fd").is_dir():
        descriptor_root = "/dev/fd"
    else:
        raise PublishError("platform does not expose inherited descriptor paths")
    paths: list[str] = []
    descriptors: list[int] = []
    try:
        for asset in manifest["assets"]:
            relative = asset["path"]
            data = _read_asset(package_root, relative, asset)
            if all(
                hasattr(namespace, name)
                for namespace, name in (
                    (os, "memfd_create"),
                    (os, "MFD_ALLOW_SEALING"),
                    (fcntl, "F_ADD_SEALS"),
                    (fcntl, "F_SEAL_WRITE"),
                    (fcntl, "F_SEAL_GROW"),
                    (fcntl, "F_SEAL_SHRINK"),
                    (fcntl, "F_SEAL_SEAL"),
                )
            ):
                read_descriptor = os.memfd_create(
                    "xhs-approved-image",
                    flags=os.MFD_ALLOW_SEALING | getattr(os, "MFD_CLOEXEC", 0),
                )
                try:
                    view = memoryview(data)
                    while view:
                        written = os.write(read_descriptor, view)
                        if written <= 0:
                            raise PublishError("cannot write anonymous asset snapshot")
                        view = view[written:]
                    os.lseek(read_descriptor, 0, os.SEEK_SET)
                    seals = (
                        fcntl.F_SEAL_WRITE
                        | fcntl.F_SEAL_GROW
                        | fcntl.F_SEAL_SHRINK
                        | fcntl.F_SEAL_SEAL
                    )
                    fcntl.fcntl(read_descriptor, fcntl.F_ADD_SEALS, seals)
                except Exception:
                    os.close(read_descriptor)
                    raise
            else:
                write_descriptor, temporary_name = tempfile.mkstemp(dir=snapshot_root)
                read_descriptor = -1
                try:
                    handle = os.fdopen(write_descriptor, "wb")
                    write_descriptor = -1
                    with handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                        os.fchmod(handle.fileno(), 0o400)
                    read_descriptor = os.open(
                        temporary_name,
                        os.O_RDONLY
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0),
                    )
                    os.unlink(temporary_name)
                except Exception:
                    if write_descriptor >= 0:
                        os.close(write_descriptor)
                    if read_descriptor >= 0:
                        os.close(read_descriptor)
                    try:
                        os.unlink(temporary_name)
                    except OSError:
                        pass
                    raise
            descriptors.append(read_descriptor)
            snapshot_stat = os.fstat(read_descriptor)
            if not stat.S_ISREG(snapshot_stat.st_mode) or snapshot_stat.st_size != len(
                data
            ):
                raise PublishError("anonymous asset snapshot is invalid")
            paths.append(f"{descriptor_root}/{read_descriptor}")
        return paths, descriptors
    except Exception:
        for descriptor in descriptors:
            os.close(descriptor)
        raise


def _call_backend(
    api_python: Path,
    tool: Path,
    method: str,
    payload: dict[str, Any],
    temporary_root: Path,
    *,
    timeout_seconds: int,
    inherited_descriptors: tuple[int, ...] = (),
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
            pass_fds=inherited_descriptors,
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


def _reject_response_secret(
    value: Any, secret: str, location: str = "$", depth: int = 0
) -> None:
    if depth > MAX_JSON_DEPTH:
        raise PublishError(
            f"backend response JSON nesting exceeds {MAX_JSON_DEPTH} levels"
        )
    if isinstance(value, dict):
        for key, child in value.items():
            if _normalize_secret_key(key) in SECRET_KEYS:
                raise PublishError(
                    f"backend response contains credential-like field at {location}.{key}"
                )
            if isinstance(key, str) and secret and secret in key:
                raise PublishError(
                    f"backend response contains credential material at {location}"
                )
            _reject_response_secret(child, secret, f"{location}.{key}", depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_response_secret(child, secret, f"{location}[{index}]", depth + 1)
    elif isinstance(value, str) and secret and secret in value:
        raise PublishError(
            f"backend response contains credential material at {location}"
        )


def _post_note_id(body: dict[str, Any]) -> str:
    data = body.get("data")
    if not isinstance(data, dict):
        raise PublishError("post response does not contain data")
    note_id = data.get("note_id") or data.get("noteId") or data.get("id")
    if not isinstance(note_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{6,128}", note_id.strip()
    ):
        raise PublishError(
            "post response note_id is missing or unsafe; outcome is unknown"
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


def _attempt_path(package_root: Path, approval_id: str) -> Path:
    try:
        normalized = str(uuid.UUID(approval_id))
    except (ValueError, AttributeError) as exc:
        raise PublishError("approval_id is not a valid UUID") from exc
    return package_root / f".xhs-publish-attempt-{normalized}.json"


def _recover_from_existing_record(
    record_path: Path,
    attempt_path: Path,
    attempt: dict[str, Any],
    approval: dict[str, Any],
) -> dict[str, Any]:
    record = _load_json(record_path, required_mode=0o600)
    _reject_response_secret(record, "")
    record_fields = {
        "schema_version",
        "publication_id",
        "manifest_id",
        "content_hash",
        "approval_id",
        "status",
        "platform",
        "note_id",
        "url",
        "published_at",
        "verification",
        "recorded_at",
    }
    if set(record) != record_fields:
        raise PublishError("existing publication record has invalid schema fields")
    verification = record.get("verification")
    if not isinstance(verification, dict) or set(verification) != {
        "method",
        "verified_by",
        "verified_at",
    }:
        raise PublishError(
            "existing publication record has invalid verification fields"
        )
    note_id = attempt["note_id"].strip()
    expected = {
        "schema_version": 1,
        "manifest_id": approval["manifest_id"],
        "content_hash": approval["content_hash"],
        "approval_id": approval["approval_id"],
        "status": "publication_recorded",
        "platform": "xiaohongshu",
        "note_id": note_id,
        "url": f"https://www.xiaohongshu.com/explore/{note_id}",
        "published_at": attempt["backend_accepted_at"],
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise PublishError("existing publication record does not match the attempt")
    try:
        uuid.UUID(str(record.get("publication_id", "")))
    except ValueError as exc:
        raise PublishError("existing publication record has an invalid ID") from exc
    if (
        verification.get("method") != "creator_api_readback"
        or verification.get("verified_by") != "agent"
    ):
        raise PublishError("existing publication record has invalid verification")
    timestamps: dict[str, datetime] = {}
    for label, value in (
        ("published_at", record["published_at"]),
        ("verified_at", verification["verified_at"]),
        ("recorded_at", record["recorded_at"]),
    ):
        if not isinstance(value, str):
            raise PublishError(
                f"existing publication record {label} is not a timestamp"
            )
        try:
            parsed = _parse_utc(value)
        except WorkflowError as exc:
            raise PublishError(
                f"existing publication record {label} is not a valid timestamp"
            ) from exc
        if _format_utc(parsed) != value:
            raise PublishError(
                f"existing publication record {label} is not canonical UTC"
            )
        timestamps[label] = parsed
    now = datetime.now(timezone.utc)
    if any(value > now for value in timestamps.values()):
        raise PublishError("existing publication record timestamp is in the future")
    if timestamps["verified_at"] < timestamps["published_at"]:
        raise PublishError("existing publication verification predates publication")
    if timestamps["recorded_at"] < timestamps["verified_at"]:
        raise PublishError("existing publication record predates verification")
    attempt["status"] = "verified"
    attempt["verified_at"] = verification["verified_at"]
    attempt["publication_id"] = record["publication_id"]
    _write_json(attempt_path, attempt, mode=0o600)
    return record


def publish_once(
    manifest_path: str | Path,
    approval_path: str | Path,
    record_path: str | Path,
    *,
    api_tool: str | Path,
    cookie_file: str | Path,
    api_python: str | Path | None = None,
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
    manifest, approval_data, approval = load_verified_publication_inputs(
        manifest_path, approval_path
    )
    if not approval["valid"]:
        raise PublishError(
            "publication approval is invalid: " + "; ".join(approval["errors"])
        )
    post = manifest["post"]
    if post.get("visibility") != "public":
        raise PublishError(
            "automatic publisher currently supports public visibility only"
        )
    if post.get("publish_mode") != "immediate":
        raise PublishError("automatic publisher requires publish_mode='immediate'")

    package_root = Path(manifest["package_root"]).resolve(strict=True)
    attempt_destination = _attempt_path(package_root, approval["approval_id"])
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
        image_paths, asset_descriptors = _snapshot_assets(manifest, snapshot_root)
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
            if datetime.now(timezone.utc) >= _parse_utc(approval_data["expires_at"]):
                raise PublishError("publication approval expired before external write")
            try:
                _write_json_exclusive(attempt_destination, attempt, mode=0o600)
            except WorkflowError as exc:
                raise PublishError(
                    "publication authorization was already consumed; "
                    "do not retry before reconciliation"
                ) from exc
            try:
                post_response = _call_backend(
                    backend_python,
                    tool,
                    "post_note",
                    payload,
                    temporary_root,
                    timeout_seconds=timeout_seconds,
                    inherited_descriptors=tuple(asset_descriptors),
                )
                _reject_response_secret(post_response, cookie)
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
        finally:
            for descriptor in asset_descriptors:
                os.close(descriptor)

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
                _reject_response_secret(readback, cookie)
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
        result = {
            "platform": "xiaohongshu",
            "note_id": note_id,
            "url": f"https://www.xiaohongshu.com/explore/{note_id}",
            "published_at": published_at,
            "verification": {
                "method": "creator_api_readback",
                "verified_by": "agent",
                "verified_at": verified_at,
            },
        }
        record = record_publication_data(approval, result, record_destination)
        attempt["status"] = "verified"
        attempt["verified_at"] = verified_at
        attempt["publication_id"] = record["publication_id"]
        _write_json(attempt_destination, attempt, mode=0o600)
        return record


def reconcile_attempt(
    manifest_path: str | Path,
    approval_path: str | Path,
    record_path: str | Path,
    *,
    api_tool: str | Path,
    cookie_file: str | Path,
    api_python: str | Path | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Reconcile an uncertain attempt using read-only creator history; never republish."""
    manifest, _approval_data, approval = load_verified_publication_inputs(
        manifest_path, approval_path, allow_expired=True
    )
    if not approval["valid"]:
        raise PublishError(
            "publication approval is invalid: " + "; ".join(approval["errors"])
        )
    package_root = Path(manifest["package_root"]).resolve(strict=True)
    attempt_source = _attempt_path(package_root, approval["approval_id"])
    record_destination = _safe_output_path(record_path, package_root, "record file")
    try:
        attempt_stat = attempt_source.lstat()
    except OSError as exc:
        raise PublishError("publication attempt does not exist") from exc
    if stat.S_ISLNK(attempt_stat.st_mode) or not stat.S_ISREG(attempt_stat.st_mode):
        raise PublishError("attempt file must be a regular, non-symlink file")
    if stat.S_IMODE(attempt_stat.st_mode) != 0o600:
        raise PublishError("attempt file must have mode 600")
    attempt = _load_json(attempt_source, required_mode=0o600)
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
        return _recover_from_existing_record(
            record_destination, attempt_source, attempt, approval
        )
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
        _reject_response_secret(readback, cookie)
        body = _api_result(readback, "get_publish_note_info")
        if not _readback_matches(body, note_id.strip(), manifest["post"]["title"]):
            raise PublishError(
                "note is still absent from creator readback; do not retry publication"
            )
        verified_at = _utc_now()
        result = {
            "platform": "xiaohongshu",
            "note_id": note_id.strip(),
            "url": f"https://www.xiaohongshu.com/explore/{note_id.strip()}",
            "published_at": published_at,
            "verification": {
                "method": "creator_api_readback",
                "verified_by": "agent",
                "verified_at": verified_at,
            },
        }
        record = record_publication_data(approval, result, record_destination)
    attempt["status"] = "verified"
    attempt["verified_at"] = verified_at
    attempt["publication_id"] = record["publication_id"]
    _write_json(attempt_source, attempt, mode=0o600)
    return record


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--approval", required=True)
    parser.add_argument("--record", required=True)
    parser.add_argument("--api-tool", default=os.environ.get("XHS_API_TOOL"))
    parser.add_argument(
        "--api-python",
        default=os.environ.get("XHS_API_PYTHON"),
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
    if not args.api_tool or not args.api_python or not args.cookie_file:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": (
                        "XHS_API_TOOL, XHS_API_PYTHON, and XHS_COOKIE_FILE are required"
                    ),
                }
            )
        )
        return 2
    try:
        if args.command == "publish":
            result = publish_once(
                args.manifest,
                args.approval,
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
