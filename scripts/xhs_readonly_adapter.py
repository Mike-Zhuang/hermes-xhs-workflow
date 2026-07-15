#!/usr/bin/env python3
"""Least-privilege adapter for selected read-only XhsSkills methods.

The adapter keeps cookies out of command-line arguments by injecting them into a
mode-0600 temporary JSON file consumed through XhsSkills' --params-file option.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_JSON_DEPTH = 32
SECRET_KEYS = {
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
METHOD_PARAMETERS = {
    ("pc", "search_note"): {
        "query",
        "page",
        "sort_type_choice",
        "note_type",
        "note_time",
        "note_range",
        "pos_distance",
        "geo",
    },
    ("pc", "search_some_note"): {
        "query",
        "require_num",
        "sort_type_choice",
        "note_type",
        "note_time",
        "note_range",
        "pos_distance",
        "geo",
    },
    ("pc", "get_note_info"): {"url"},
    ("pc", "search_user"): {"query", "page"},
    ("pc", "search_some_user"): {"query", "require_num"},
    ("pc", "get_user_info"): {"user_id"},
    ("creator", "get_publish_note_info"): {"page"},
}
METHOD_REQUIRED = {
    ("pc", "search_note"): {"query"},
    ("pc", "search_some_note"): {"query", "require_num"},
    ("pc", "get_note_info"): {"url"},
    ("pc", "search_user"): {"query"},
    ("pc", "search_some_user"): {"query", "require_num"},
    ("pc", "get_user_info"): {"user_id"},
    ("creator", "get_publish_note_info"): {"page"},
}
INTEGER_RANGES = {
    "sort_type_choice": (0, 4),
    "note_type": (0, 2),
    "note_time": (0, 3),
    "note_range": (0, 3),
    "pos_distance": (0, 2),
}


class AdapterError(ValueError):
    """Raised when an adapter request violates the safety contract."""


def _load_object(path: Path, *, max_bytes: int = MAX_REQUEST_BYTES) -> dict[str, Any]:
    try:
        if path.stat().st_size > max_bytes:
            raise AdapterError(f"JSON file exceeds {max_bytes} bytes: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except AdapterError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise AdapterError(f"Cannot read JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AdapterError(f"Expected a JSON object in {path}")
    return value


def _normalize_secret_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).strip().lower())


def _reject_secrets(value: Any, location: str = "$", depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise AdapterError(
            f"JSON nesting exceeds {MAX_JSON_DEPTH} levels at {location}"
        )
    if isinstance(value, dict):
        for key, child in value.items():
            if _normalize_secret_key(key) in SECRET_KEYS:
                raise AdapterError(
                    f"secret-like field is forbidden at {location}.{key}"
                )
            _reject_secrets(child, f"{location}.{key}", depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secrets(child, f"{location}[{index}]", depth + 1)


def _redact(value: Any, *, secrets: tuple[str, ...], depth: int = 0) -> Any:
    if depth > MAX_JSON_DEPTH:
        return "[REDACTED: excessive nesting]"
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if _normalize_secret_key(key) in SECRET_KEYS
            else _redact(child, secrets=secrets, depth=depth + 1)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact(child, secrets=secrets, depth=depth + 1) for child in value]
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            if secret:
                redacted = redacted.replace(secret, "[REDACTED]")
        return redacted
    return value


def _read_cookie(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError) or getattr(exc, "errno", None) in {
            errno.ELOOP,
            errno.ENOENT,
        }:
            raise AdapterError(
                "cookie file must be a regular, non-symlink file"
            ) from exc
        raise AdapterError(f"cannot open cookie file: {exc}") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise AdapterError("cookie file must be a regular, non-symlink file")
        if stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise AdapterError("cookie file must have mode 600")
        if file_stat.st_size > 65536:
            raise AdapterError("cookie file exceeds 64 KiB")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            cookie = handle.read(65537).strip()
    except UnicodeError as exc:
        raise AdapterError("cookie file must contain UTF-8 text") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(cookie.encode("utf-8")) > 65536:
        raise AdapterError("cookie file exceeds 64 KiB")
    if not cookie:
        raise AdapterError("cookie file is empty")
    return cookie


def _validate_params(namespace: str, method: str, params: Any) -> dict[str, Any]:
    allowed = METHOD_PARAMETERS.get((namespace, method))
    if allowed is None:
        raise AdapterError(f"method is not allowlisted: {namespace}.{method}")
    if not isinstance(params, dict):
        raise AdapterError("params must be a JSON object")
    unexpected = set(params) - allowed
    if unexpected:
        raise AdapterError("unexpected params: " + ", ".join(sorted(unexpected)))
    missing = METHOD_REQUIRED[(namespace, method)] - set(params)
    if missing:
        raise AdapterError("missing required params: " + ", ".join(sorted(missing)))

    normalized = dict(params)
    if "page" in normalized:
        if type(normalized["page"]) is not int or not 1 <= normalized["page"] <= 10:
            raise AdapterError("page must be an integer from 1 to 10")
    if "require_num" in normalized:
        if (
            type(normalized["require_num"]) is not int
            or not 1 <= normalized["require_num"] <= 50
        ):
            raise AdapterError("require_num must be an integer from 1 to 50")
    for name, (minimum, maximum) in INTEGER_RANGES.items():
        if name in normalized and (
            type(normalized[name]) is not int
            or not minimum <= normalized[name] <= maximum
        ):
            raise AdapterError(f"{name} must be an integer from {minimum} to {maximum}")
    if "query" in normalized:
        if not isinstance(normalized["query"], str) or not normalized["query"].strip():
            raise AdapterError("query must be a non-empty string")
        if len(normalized["query"]) > 200:
            raise AdapterError("query must be at most 200 characters")
    if "user_id" in normalized:
        if (
            not isinstance(normalized["user_id"], str)
            or not normalized["user_id"].strip()
        ):
            raise AdapterError("user_id must be a non-empty string")
        if len(normalized["user_id"]) > 128:
            raise AdapterError("user_id must be at most 128 characters")
    if "geo" in normalized and (
        not isinstance(normalized["geo"], str) or len(normalized["geo"]) > 200
    ):
        raise AdapterError("geo must be a string of at most 200 characters")
    if method == "get_note_info" and "url" in normalized:
        if not isinstance(normalized["url"], str):
            raise AdapterError("url must be a string")
        parsed = urlparse(normalized["url"])
        if parsed.scheme != "https" or parsed.hostname not in {
            "www.xiaohongshu.com",
            "xiaohongshu.com",
        }:
            raise AdapterError("url must be an HTTPS xiaohongshu.com URL")
        if parsed.username is not None or parsed.password is not None:
            raise AdapterError("url must not contain embedded credentials")
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
            if _normalize_secret_key(key) in SECRET_KEYS:
                raise AdapterError("url must not contain credential-like query fields")
        if parsed.fragment:
            raise AdapterError("url must not contain a fragment")
    return normalized


def run_request(
    request_path: str | Path,
    *,
    api_tool: str | Path,
    cookie_file: str | Path,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    request = _load_object(Path(request_path))
    _reject_secrets(request)
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
        raise AdapterError("timeout_seconds must be an integer from 1 to 300")
    namespace = request.get("namespace")
    method = request.get("method")
    if not isinstance(namespace, str) or not isinstance(method, str):
        raise AdapterError("namespace and method must be strings")
    params = _validate_params(namespace, method, request.get("params", {}))

    cookie = _read_cookie(Path(cookie_file))
    try:
        tool = Path(api_tool).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise AdapterError(f"Cannot resolve XhsSkills api tool: {api_tool}") from exc
    if not tool.is_file():
        raise AdapterError("api tool must be a regular file")
    payload = dict(params)
    payload["cookies_str"] = cookie

    with tempfile.TemporaryDirectory(prefix="xhs-readonly-") as temporary:
        temp_root = Path(temporary)
        params_path = temp_root / "params.json"
        output_path = temp_root / "output.json"
        params_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        os.chmod(params_path, 0o600)
        try:
            completed = subprocess.run(  # noqa: S603 -- argv-only, user-selected backend
                [
                    sys.executable,
                    str(tool),
                    "call",
                    namespace,
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
            raise AdapterError("XhsSkills backend timed out") from exc
        if completed.returncode != 0:
            raise AdapterError(
                f"XhsSkills backend failed with exit code {completed.returncode}"
            )
        response = _load_object(output_path, max_bytes=MAX_RESPONSE_BYTES)
    return _redact(response, secrets=(cookie,))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument(
        "--api-tool",
        default=os.environ.get("XHS_API_TOOL"),
        help="Path to XhsSkills xhs_api_tool.py (or set XHS_API_TOOL)",
    )
    parser.add_argument(
        "--cookie-file",
        default=os.environ.get("XHS_COOKIE_FILE"),
        help="Path to a mode-0600 cookie file (or set XHS_COOKIE_FILE)",
    )
    parser.add_argument("--timeout-seconds", type=int, default=60)
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
        response = run_request(
            args.request,
            api_tool=args.api_tool,
            cookie_file=args.cookie_file,
            timeout_seconds=args.timeout_seconds,
        )
    except (AdapterError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
