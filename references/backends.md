# Backend boundaries

## Automatic backend: pinned XhsSkills creator API

The one-shot publisher interoperates with a separately installed `cv-cat/XhsSkills` CLI. No upstream source is copied into this repository.

Reviewed upstream revision:

```text
cv-cat/XhsSkills
7b9df112ef75d9e8565e8582a3bc8bd2f1af7a5c
```

Relevant contract at that revision:

```text
creator.post_note(noteInfo, cookies_str)
  -> [success, message, response_json]

creator.get_publish_note_info(page, cookies_str)
  -> [success, message, response_json]
```

`noteInfo` uses `title`, `desc`, `postTime`, `location`, `type`, `media_type`, `topics`, and `images`. XhsSkills reads image paths into bytes before invoking its vendored Spider_XHS runtime.

Risks:

- the creator endpoint and signing implementation are reverse-engineered and non-official;
- signatures, payloads, response fields, and risk-control behavior can change without notice;
- an account may be challenged, restricted, or suspended;
- the GitHub API did not detect a reusable license file in XhsSkills or Spider_XHS at review time;
- upstream examples place Cookies in inline CLI JSON;
- upstream dependencies include Python and Node packages plus vendored signer/runtime code.

Controls in this repository:

- only `creator.post_note` is exposed as an external write;
- only image posts with immediate public visibility are accepted;
- a current manifest-bound approval is mandatory;
- approval expiry is checked again immediately before `creator.post_note`;
- `authorization_mode` distinguishes a direct publish command from explicit hash confirmation;
- source images are securely reopened and rehashed, copied into unlinked descriptors, and exposed to the child only through inherited descriptor paths; Linux adds kernel write seals before execution;
- after snapshots and the final expiry check, a fixed exclusive attempt path is derived from `approval_id` immediately before the write call, so alternate record paths cannot replay authorization;
- the write call is never automatically repeated;
- verification uses only `creator.get_publish_note_info` and requires exact note-ID/title match;
- `reconcile` cannot invoke `post_note`;
- `reconcile` can repair an attempt from a strictly matching existing publication record without calling the backend;
- Cookie-bearing params use private temporary files, not argv;
- backend stdout/stderr and upstream message strings are not returned;
- successful responses that echo the active Cookie or return unsafe note IDs are rejected before persistence;
- mandatory `XHS_API_PYTHON` is probed to confirm a separate venv while preserving the venv launcher path;
- request/response sizes and JSON depth are bounded by the adapters.

These controls reduce accidental publication, duplication, and disclosure. They do not make unofficial access stable, officially authorized, or safe from platform enforcement.

## Read-only adapter

`scripts/xhs_readonly_adapter.py` has a separate seven-method allowlist:

- `pc.search_note`
- `pc.search_some_note`
- `pc.get_note_info`
- `pc.search_user`
- `pc.search_some_user`
- `pc.get_user_info`
- `creator.get_publish_note_info`

It rejects `post_note`, uploads, engagement actions, unbounded list retrieval, unknown parameters, and arbitrary methods. It opens the mode-`0600` Cookie file once with `O_NOFOLLOW` where available and validates it with `fstat`.

## Official creator UI

Official UI automation remains a possible future backend. It has the benefit of visible platform notices and avoids directly maintaining reverse-engineered request signatures. It is not the active automatic backend here because the current server environment does not provide a persistent user-owned browser profile and the managed browser interface does not expose a reliable local-file upload contract.

If implemented later, it must retain the same manifest, attempt, no-retry, and external verification invariants. CAPTCHA and risk controls must stop the flow rather than be bypassed.

## `aki66938/xhs-toolkit`

`xhs-toolkit` offers FastMCP and Selenium-based creator workflows, but upstream currently states that development has stopped. It is not bundled because its browser selectors and task paths are volatile, issue reports include false-positive success and broken uploads, its Cookie storage is plaintext JSON, and raw publishing tools do not enforce this repository's one-attempt contract.

Do not treat it as a drop-in production publisher.
