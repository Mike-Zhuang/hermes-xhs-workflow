#!/usr/bin/env python3
"""Build and verify content-addressed Xiaohongshu publication manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

SCHEMA_VERSION = 1
ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_JSON_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 32
VERIFICATION_METHODS = {
    "creator_api_readback",
    "official_creator_ui",
    "official_note_page",
}
SECRET_FIELD_NAMES = {
    "authorization",
    "authorizationheader",
    "apikey",
    "authtoken",
    "bearer",
    "clientsecret",
    "cookie",
    "cookies",
    "cookiesstr",
    "credential",
    "credentials",
    "password",
    "passwd",
    "refreshtoken",
    "secret",
    "session",
    "sessionid",
    "setcookie",
    "token",
    "accesstoken",
    "websession",
    "xapikey",
}


class WorkflowError(ValueError):
    """Raised when a publication package violates the workflow contract."""


def _utc_now() -> str:
    return _format_utc(datetime.now(timezone.utc))


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise WorkflowError(f"Invalid UTC timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise WorkflowError(f"UTC timestamp must include timezone: {value}")
    return parsed.astimezone(timezone.utc)


def _hash_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, mode)
    temporary.replace(path)


def _write_json_exclusive(
    path: Path,
    value: Any,
    *,
    mode: int = 0o600,
    label: str = "JSON output",
) -> None:
    """Create a JSON file atomically without ever replacing an existing path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError as exc:
        raise WorkflowError(f"{label} already exists: {path}") from exc
    try:
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = -1
        with handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise


def _load_json(path: Path, *, required_mode: int | None = None) -> dict[str, Any]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | nofollow | cloexec)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise WorkflowError(f"JSON path is not a regular file: {path}")
        if required_mode is not None and stat.S_IMODE(before.st_mode) != required_mode:
            raise WorkflowError(f"JSON file must have mode {required_mode:o}: {path}")
        if before.st_size > MAX_JSON_BYTES:
            raise WorkflowError(f"JSON file exceeds 1 MiB: {path}")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_JSON_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_JSON_BYTES:
                raise WorkflowError(f"JSON file exceeds 1 MiB: {path}")
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity:
            raise WorkflowError(f"JSON file changed while being read: {path}")
        value = json.loads(b"".join(chunks).decode("utf-8"))
    except WorkflowError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise WorkflowError(f"Cannot securely read JSON file {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise WorkflowError(f"Expected a JSON object in {path}")
    return value


def _normalize_secret_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).strip().lower())


def _reject_secret_fields(value: Any, location: str = "$", depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise WorkflowError(
            f"JSON nesting exceeds {MAX_JSON_DEPTH} levels at {location}"
        )
    if isinstance(value, dict):
        for key, child in value.items():
            if _normalize_secret_key(key) in SECRET_FIELD_NAMES:
                raise WorkflowError(
                    f"secret-like field is forbidden at {location}.{key}"
                )
            _reject_secret_fields(child, f"{location}.{key}", depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_fields(child, f"{location}[{index}]", depth + 1)


def _validate_image_signature(name: str, suffix: str, header: bytes) -> None:
    matches = (
        suffix == ".png"
        and header.startswith(b"\x89PNG\r\n\x1a\n")
        or suffix in {".jpg", ".jpeg"}
        and header.startswith(b"\xff\xd8\xff")
        or suffix == ".webp"
        and header.startswith(b"RIFF")
        and header[8:12] == b"WEBP"
    )
    if not matches:
        raise WorkflowError(f"Image signature does not match extension: {name}")


def _inspect_asset(package_root: Path, raw_path: str) -> tuple[Path, str, int]:
    if "\x00" in raw_path:
        raise WorkflowError("Image path contains a NUL byte")
    relative = Path(raw_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise WorkflowError(f"Image escapes the package directory: {raw_path}")
    suffix = relative.suffix.lower()
    if suffix not in ALLOWED_IMAGE_SUFFIXES:
        raise WorkflowError(f"Unsupported image type: {suffix}")

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | nofollow | close_on_exec
    file_flags = os.O_RDONLY | nofollow | close_on_exec
    descriptors: list[int] = []
    try:
        current = os.open(package_root, directory_flags)
        descriptors.append(current)
        for part in relative.parts[:-1]:
            current = os.open(part, directory_flags, dir_fd=current)
            descriptors.append(current)
        file_descriptor = os.open(relative.parts[-1], file_flags, dir_fd=current)
        descriptors.append(file_descriptor)
        before = os.fstat(file_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise WorkflowError(f"Image is not a regular file: {raw_path}")
        header = os.read(file_descriptor, 16)
        _validate_image_signature(relative.name, suffix, header)
        digest = _hash_descriptor(file_descriptor)
        after = os.fstat(file_descriptor)
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
            raise WorkflowError(f"Image changed while being inspected: {raw_path}")
        return package_root.joinpath(*relative.parts), digest, before.st_size
    except WorkflowError:
        raise
    except OSError as exc:
        raise WorkflowError(
            f"Image path is missing, unsafe, or contains a symlink: {raw_path}"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _hash_material(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": manifest["schema_version"],
        "manifest_id": manifest["manifest_id"],
        "created_at": manifest["created_at"],
        "status": manifest["status"],
        "package_root": manifest["package_root"],
        "post": manifest["post"],
        "assets": manifest["assets"],
    }


def prepare_manifest(
    source_path: str | Path, manifest_path: str | Path
) -> dict[str, Any]:
    try:
        source = Path(source_path).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise WorkflowError(f"Cannot resolve source package: {source_path}") from exc
    if not source.is_file():
        raise WorkflowError("source must be a regular JSON file")
    package_root = source.parent
    payload = _load_json(source)
    _reject_secret_fields(payload)
    title = payload.get("title")
    content = payload.get("content")
    images = payload.get("images")
    topics = payload.get("topics", [])
    if not isinstance(title, str) or not title.strip():
        raise WorkflowError("title must be a non-empty string")
    if not isinstance(content, str) or not content.strip():
        raise WorkflowError("content must be a non-empty string")
    if not isinstance(images, list) or not images:
        raise WorkflowError("images must be a non-empty list")
    if len(images) > 9:
        raise WorkflowError("images must contain at most 9 items")
    if not isinstance(topics, list) or not all(isinstance(x, str) for x in topics):
        raise WorkflowError("topics must be a list of strings")

    assets = []
    for index, raw_path in enumerate(images, start=1):
        if not isinstance(raw_path, str):
            raise WorkflowError("every image path must be a string")
        resolved, digest, size_bytes = _inspect_asset(package_root, raw_path)
        assets.append(
            {
                "index": index,
                "path": resolved.relative_to(package_root).as_posix(),
                "sha256": digest,
                "size_bytes": size_bytes,
            }
        )

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "manifest_id": str(uuid.uuid4()),
        "created_at": _utc_now(),
        "status": "prepared",
        "package_root": str(package_root),
        "post": {
            "title": title.strip(),
            "content": content.strip(),
            "topics": [topic.strip() for topic in topics if topic.strip()],
            "visibility": payload.get("visibility", "public"),
            "publish_mode": payload.get("publish_mode", "manual"),
        },
        "assets": assets,
    }
    manifest["content_hash"] = _canonical_hash(_hash_material(manifest))
    _write_json_exclusive(Path(manifest_path), manifest, mode=0o600, label="Manifest")
    return manifest


def _validate_manifest_data(manifest: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported schema_version")
    try:
        uuid.UUID(str(manifest.get("manifest_id", "")))
    except ValueError:
        errors.append("manifest_id must be a UUID")
    if manifest.get("status") != "prepared":
        errors.append("status must be prepared")
    try:
        _parse_utc(manifest["created_at"])
    except (KeyError, WorkflowError):
        errors.append("created_at must be a timezone-aware timestamp")
    try:
        _reject_secret_fields(manifest)
    except WorkflowError as exc:
        errors.append(str(exc))

    post = manifest.get("post")
    if not isinstance(post, dict):
        errors.append("post must be an object")
    else:
        if not isinstance(post.get("title"), str) or not post["title"].strip():
            errors.append("post.title must be a non-empty string")
        if not isinstance(post.get("content"), str) or not post["content"].strip():
            errors.append("post.content must be a non-empty string")
        topics = post.get("topics")
        if not isinstance(topics, list) or not all(
            isinstance(topic, str) for topic in topics
        ):
            errors.append("post.topics must be a list of strings")
        if not isinstance(post.get("visibility"), str):
            errors.append("post.visibility must be a string")
        if not isinstance(post.get("publish_mode"), str):
            errors.append("post.publish_mode must be a string")

    package_root_value = manifest.get("package_root")
    package_root: Path | None = None
    if not isinstance(package_root_value, str) or not package_root_value:
        errors.append("package_root must be a non-empty string")
    else:
        try:
            package_root = Path(package_root_value).resolve()
        except (OSError, ValueError) as exc:
            errors.append(f"package_root is invalid: {exc}")

    assets = manifest.get("assets")
    if not isinstance(assets, list):
        errors.append("assets must be a list")
        assets = []
    elif not 1 <= len(assets) <= 9:
        errors.append("assets must contain between 1 and 9 items")

    for expected_index, asset in enumerate(assets, start=1):
        if not isinstance(asset, dict):
            errors.append(f"asset {expected_index} must be an object")
            continue
        if asset.get("index") != expected_index:
            errors.append(f"asset index mismatch at position {expected_index}")
        asset_path = asset.get("path")
        if not isinstance(asset_path, str):
            errors.append(f"asset {expected_index} path must be a string")
            continue
        expected_hash = asset.get("sha256")
        if not isinstance(expected_hash, str) or not _is_sha256(expected_hash):
            errors.append(f"asset {expected_index} sha256 is invalid")
        expected_size = asset.get("size_bytes")
        if type(expected_size) is not int or expected_size < 0:
            errors.append(f"asset {expected_index} size_bytes is invalid")
        if package_root is None:
            continue
        try:
            _, current_hash, current_size = _inspect_asset(package_root, asset_path)
        except (WorkflowError, OSError) as exc:
            errors.append(str(exc))
            continue
        if current_hash != expected_hash:
            errors.append(f"asset hash mismatch: {asset_path}")
        if current_size != expected_size:
            errors.append(f"asset size mismatch: {asset_path}")

    try:
        computed_hash = _canonical_hash(_hash_material(manifest))
    except (KeyError, TypeError, RecursionError) as exc:
        errors.append(f"malformed manifest: {exc}")
        computed_hash = ""
    stored_hash = manifest.get("content_hash")
    if not isinstance(stored_hash, str) or not _is_sha256(stored_hash):
        errors.append("content_hash must be a SHA-256 hex digest")
    if computed_hash != stored_hash:
        errors.append("content hash mismatch")
    return {
        "valid": not errors,
        "manifest_id": manifest.get("manifest_id"),
        "content_hash": computed_hash,
        "errors": errors,
    }


def validate_manifest(manifest_path: str | Path) -> dict[str, Any]:
    return _validate_manifest_data(_load_json(Path(manifest_path)))


def build_preview(manifest_path: str | Path) -> dict[str, Any]:
    manifest = _load_json(Path(manifest_path))
    report = _validate_manifest_data(manifest)
    if not report["valid"]:
        raise WorkflowError("Cannot preview an invalid manifest")
    post = manifest["post"]
    image_paths = [asset["path"] for asset in manifest["assets"]]
    warnings = []
    if not post["content"].lstrip().startswith("TL;DR"):
        warnings.append("content does not start with TL;DR")
    if len(image_paths) != 9:
        warnings.append("image count differs from the preferred nine-image package")
    return {
        "manifest_id": manifest["manifest_id"],
        "content_hash": manifest["content_hash"],
        "title": post["title"],
        "content": post["content"],
        "topics": post["topics"],
        "visibility": post["visibility"],
        "publish_mode": post["publish_mode"],
        "images": image_paths,
        "image_count": len(image_paths),
        "warnings": warnings,
    }


def approve_manifest(
    manifest_path: str | Path,
    approval_path: str | Path,
    *,
    ttl_seconds: int = 1800,
    confirmed_by: str,
    confirmed_content_hash: str,
    authorization_mode: str = "explicit_hash_confirmation",
) -> dict[str, Any]:
    if confirmed_by != "user":
        raise WorkflowError("confirmed_by must be 'user'")
    if authorization_mode not in {
        "direct_publish_command",
        "explicit_hash_confirmation",
    }:
        raise WorkflowError("authorization_mode is unsupported")
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
        raise WorkflowError("ttl_seconds must be between 1 and 86400")
    manifest = _load_json(Path(manifest_path))
    report = _validate_manifest_data(manifest)
    if not report["valid"]:
        raise WorkflowError("Cannot approve an invalid manifest")
    if confirmed_content_hash != manifest["content_hash"]:
        raise WorkflowError("confirmed content hash does not match the manifest")
    issued_at = datetime.now(timezone.utc)
    approval = {
        "schema_version": SCHEMA_VERSION,
        "approval_id": str(uuid.uuid4()),
        "manifest_id": manifest["manifest_id"],
        "content_hash": manifest["content_hash"],
        "confirmed_content_hash": confirmed_content_hash,
        "confirmed_by": confirmed_by,
        "authorization_mode": authorization_mode,
        "issued_at": _format_utc(issued_at),
        "expires_at": _format_utc(issued_at + timedelta(seconds=ttl_seconds)),
    }
    _write_json_exclusive(Path(approval_path), approval, mode=0o600, label="Approval")
    return approval


def _verify_approval_data(
    manifest: dict[str, Any],
    approval: dict[str, Any],
    *,
    allow_expired: bool = False,
) -> dict[str, Any]:
    manifest_report = _validate_manifest_data(manifest)
    errors = list(manifest_report["errors"])
    if approval.get("schema_version") != SCHEMA_VERSION:
        errors.append("approval schema_version is unsupported")
    try:
        uuid.UUID(str(approval.get("approval_id", "")))
    except ValueError:
        errors.append("approval_id must be a UUID")
    try:
        _reject_secret_fields(approval)
    except WorkflowError as exc:
        errors.append(str(exc))
    if approval.get("manifest_id") != manifest.get("manifest_id"):
        errors.append("approval manifest_id mismatch")
    if approval.get("content_hash") != manifest.get("content_hash"):
        errors.append("approval content_hash mismatch")
    if approval.get("confirmed_content_hash") != manifest.get("content_hash"):
        errors.append("approval confirmed_content_hash mismatch")
    if approval.get("confirmed_by") != "user":
        errors.append("approval is not user-confirmed")
    if approval.get("authorization_mode") not in {
        "direct_publish_command",
        "explicit_hash_confirmation",
    }:
        errors.append("approval authorization_mode is unsupported")

    now = datetime.now(timezone.utc)
    try:
        issued_at = _parse_utc(approval["issued_at"])
        expires_at = _parse_utc(approval["expires_at"])
        if issued_at > now + timedelta(minutes=5):
            errors.append("approval issued_at is in the future")
        if expires_at <= issued_at:
            errors.append("approval expires_at must be after issued_at")
        if expires_at - issued_at > timedelta(days=1):
            errors.append("approval lifetime exceeds 24 hours")
        if not allow_expired and now >= expires_at:
            errors.append("approval expired")
    except (KeyError, WorkflowError) as exc:
        errors.append(str(exc))
    return {
        "valid": not errors,
        "manifest_id": manifest.get("manifest_id"),
        "content_hash": manifest.get("content_hash"),
        "approval_id": approval.get("approval_id"),
        "issued_at": approval.get("issued_at"),
        "expires_at": approval.get("expires_at"),
        "errors": errors,
    }


def load_verified_publication_inputs(
    manifest_path: str | Path,
    approval_path: str | Path,
    *,
    allow_expired: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest = _load_json(Path(manifest_path))
    approval = _load_json(Path(approval_path), required_mode=0o600)
    report = _verify_approval_data(manifest, approval, allow_expired=allow_expired)
    return manifest, approval, report


def verify_approval(
    manifest_path: str | Path,
    approval_path: str | Path,
    *,
    allow_expired: bool = False,
) -> dict[str, Any]:
    try:
        _, _, report = load_verified_publication_inputs(
            manifest_path, approval_path, allow_expired=allow_expired
        )
        return report
    except WorkflowError as exc:
        return {
            "valid": False,
            "manifest_id": None,
            "content_hash": None,
            "approval_id": None,
            "issued_at": None,
            "expires_at": None,
            "errors": [str(exc)],
        }


def record_publication_data(
    approval_report: dict[str, Any],
    result: dict[str, Any],
    record_path: str | Path,
) -> dict[str, Any]:
    if not approval_report["valid"]:
        raise WorkflowError(
            "Cannot record publication with invalid approval: "
            + "; ".join(approval_report["errors"])
        )
    _reject_secret_fields(result)
    if result.get("platform") != "xiaohongshu":
        raise WorkflowError("result platform must be 'xiaohongshu'")
    note_id = result.get("note_id")
    if not isinstance(note_id, str) or not note_id.strip():
        raise WorkflowError("result note_id must be a non-empty string")
    url = result.get("url")
    parsed = urlparse(url) if isinstance(url, str) else None
    if (
        parsed is None
        or parsed.scheme != "https"
        or parsed.hostname not in {"www.xiaohongshu.com", "xiaohongshu.com"}
    ):
        raise WorkflowError("result url must be an HTTPS xiaohongshu.com URL")
    if parsed.username is not None or parsed.password is not None:
        raise WorkflowError("result url must not contain embedded credentials")
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if _normalize_secret_key(key) in SECRET_FIELD_NAMES:
            raise WorkflowError(
                "result url must not contain credential-like query fields"
            )
    if parsed.fragment:
        raise WorkflowError("result url must not contain a fragment")
    if parsed.path.rstrip("/").split("/")[-1] != note_id.strip():
        raise WorkflowError("result note_id must match the final URL path segment")
    published_at = result.get("published_at")
    if not isinstance(published_at, str):
        raise WorkflowError("result published_at must be a UTC timestamp")
    published_time = _parse_utc(published_at)
    normalized_published_at = _format_utc(published_time)
    now = datetime.now(timezone.utc)
    if published_time > now + timedelta(minutes=5):
        raise WorkflowError("result published_at is in the future")
    approval_issued = _parse_utc(approval_report["issued_at"])
    approval_expires = _parse_utc(approval_report["expires_at"])
    if published_time < approval_issued - timedelta(minutes=5):
        raise WorkflowError("result published_at predates the approval window")
    if published_time > approval_expires + timedelta(minutes=5):
        raise WorkflowError("result published_at is after the approval window")
    verification = result.get("verification")
    if not isinstance(verification, dict):
        raise WorkflowError("result verification must be an object")
    method = verification.get("method")
    if method not in VERIFICATION_METHODS:
        raise WorkflowError(
            "verification method must be creator_api_readback, "
            "official_creator_ui, or official_note_page"
        )
    verified_by = verification.get("verified_by")
    if verified_by not in {"user", "agent"}:
        raise WorkflowError("verification verified_by must be 'user' or 'agent'")
    verified_at = verification.get("verified_at")
    if not isinstance(verified_at, str):
        raise WorkflowError("verification verified_at must be a UTC timestamp")
    verified_time = _parse_utc(verified_at)
    normalized_verified_at = _format_utc(verified_time)
    if verified_time > now + timedelta(minutes=5):
        raise WorkflowError("verification verified_at is in the future")
    if verified_time < published_time - timedelta(minutes=5):
        raise WorkflowError("verification cannot precede publication")

    record = {
        "schema_version": SCHEMA_VERSION,
        "publication_id": str(uuid.uuid4()),
        "manifest_id": approval_report["manifest_id"],
        "content_hash": approval_report["content_hash"],
        "approval_id": approval_report["approval_id"],
        "status": "publication_recorded",
        "platform": "xiaohongshu",
        "note_id": note_id.strip(),
        "url": url,
        "published_at": normalized_published_at,
        "verification": {
            "method": method,
            "verified_by": verified_by,
            "verified_at": normalized_verified_at,
        },
        "recorded_at": _utc_now(),
    }
    destination = Path(record_path)
    _write_json_exclusive(destination, record, mode=0o600)
    return record


def record_publication(
    manifest_path: str | Path,
    approval_path: str | Path,
    result_path: str | Path,
    record_path: str | Path,
) -> dict[str, Any]:
    _, _, approval_report = load_verified_publication_inputs(
        manifest_path, approval_path
    )
    result = _load_json(Path(result_path))
    return record_publication_data(approval_report, result, record_path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare and verify user-authorized Xiaohongshu publication packages."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser(
        "prepare", help="Create a content-addressed manifest"
    )
    prepare.add_argument("--source", required=True)
    prepare.add_argument("--manifest", required=True)

    validate = subparsers.add_parser("validate", help="Revalidate manifest and assets")
    validate.add_argument("--manifest", required=True)

    preview = subparsers.add_parser("preview", help="Render exact confirmation preview")
    preview.add_argument("--manifest", required=True)

    approve = subparsers.add_parser(
        "approve", help="Record approval after explicit user confirmation"
    )
    approve.add_argument("--manifest", required=True)
    approve.add_argument("--approval", required=True)
    approve.add_argument("--ttl-seconds", type=int, default=1800)
    approve.add_argument("--confirmed-by", required=True, choices=["user"])
    approve.add_argument(
        "--authorization-mode",
        choices=["direct_publish_command", "explicit_hash_confirmation"],
        default="explicit_hash_confirmation",
    )
    approve.add_argument(
        "--confirmed-content-hash",
        required=True,
        help="Exact full content_hash bound to the selected authorization mode",
    )

    verify = subparsers.add_parser("verify", help="Verify manifest-bound approval")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--approval", required=True)

    record = subparsers.add_parser("record", help="Record a verified platform result")
    record.add_argument("--manifest", required=True)
    record.add_argument("--approval", required=True)
    record.add_argument("--result", required=True)
    record.add_argument("--record", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_manifest(args.source, args.manifest)
        elif args.command == "validate":
            result = validate_manifest(args.manifest)
        elif args.command == "preview":
            result = build_preview(args.manifest)
        elif args.command == "approve":
            result = approve_manifest(
                args.manifest,
                args.approval,
                ttl_seconds=args.ttl_seconds,
                confirmed_by=args.confirmed_by,
                confirmed_content_hash=args.confirmed_content_hash,
                authorization_mode=args.authorization_mode,
            )
        elif args.command == "verify":
            result = verify_approval(args.manifest, args.approval)
        elif args.command == "record":
            result = record_publication(
                args.manifest, args.approval, args.result, args.record
            )
        else:  # pragma: no cover - argparse constrains this branch
            parser.error(f"Unsupported command: {args.command}")
    except (WorkflowError, OSError, RecursionError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if isinstance(result, dict) and result.get("valid") is False:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
