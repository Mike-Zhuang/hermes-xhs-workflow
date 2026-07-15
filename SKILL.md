---
name: xhs-workflow
description: Use when preparing, validating, confirming, publishing, or recording a Xiaohongshu (XHS/小红书) post from a local content package, or when performing tightly bounded read-only XHS research through an optional XhsSkills backend. Enforces content-addressed manifests, asset hashes, exact previews, explicit content-hash confirmation, expiring approvals, externally verified publication evidence, credential isolation, and a read-only API allowlist.
version: 0.1.0
author: Chengbo Zhuang and Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [xiaohongshu, xhs, publishing, workflow, safety]
    related_skills: [computer-use]
---

# Xiaohongshu Workflow

## Overview

Use this skill to turn a local sharing package into a controlled Xiaohongshu publication workflow:

```text
post.json + 1–9 local images
  -> content-addressed manifest and SHA-256 asset hashes
  -> validation and exact preview
  -> explicit user confirmation
  -> short-lived manifest-bound approval
  -> official UI/manual publication
  -> verified note URL and non-overwriting publication record at the chosen path
```

The included Python scripts use only the standard library. They do not log in, scrape, upload, or publish by themselves. `scripts/xhs_readonly_adapter.py` can optionally call a separately installed `cv-cat/XhsSkills` runtime, but only through a narrow read-only allowlist and a private temporary parameter file.

This repository does not vendor `xhs-toolkit`, `Spider_XHS`, or `XhsSkills` code. Read `references/backends.md` before enabling an unofficial backend.

## When to Use

Use this skill when:

- a paper deep-dive, article, or campaign already has a title, body, topics, and local images;
- the user wants an exact preview before any external action;
- the user wants a tamper-evident record connecting approved content to a real XHS note;
- bounded XHS search or own-account publication-list retrieval is needed for research;
- a failed or uncertain publication must be diagnosed without retrying blindly.

Do not use it for:

- bulk scraping, account farming, engagement automation, CAPTCHA bypass, anti-bot evasion, or proxy rotation;
- unattended scheduled publishing;
- likes, comments, follows, DMs, or other engagement actions;
- copying private account data into prompts, command-line arguments, logs, Git, or memory;
- claiming a post succeeded without a real platform response or a verified note URL.

## Hard Safety Contract

1. **No secrets in content artifacts.** `post.json`, request files, manifests, approvals, publication records, terminal arguments, prompts, logs, Git, and memory must not contain cookies, passwords, tokens, authorization headers, or API keys. The scripts reject common secret-like keys.
2. **Interactive login only.** If login is required, ask the user to sign in through the official UI. Never ask them to paste a password, one-time code, or complete Cookie into chat.
3. **Confirmation gate.** Never run `approve` until the user has seen the exact preview and explicitly confirmed that exact `content_hash`. A general request such as “set up the workflow” is not publication confirmation.
4. **Approval is content-bound.** Any title, body, topic, image order, or image-byte change invalidates approval. Re-run `prepare`, show the new preview, and ask again.
5. **No silent retries.** A timeout or ambiguous UI state may already have created a note. Check the creator dashboard or note URL before retrying.
6. **Read-only means read-only.** The optional adapter rejects `post_note`, `upload_media`, comments, likes, follows, messages, and every unlisted method.
7. **Platform rules still apply.** Unofficial APIs can break or trigger account restrictions. Rate-limit manually, collect only what is necessary, and stop on risk-control or verification challenges.

## Files and Schemas

Start from `templates/post.json` and keep the working package outside the Skill repository:

```text
/path/to/share-package/
├── post.json
├── 01.png
├── 02.png
└── ...
```

`post.json` fields:

| Field | Required | Meaning |
|---|---:|---|
| `title` | yes | Exact title to publish |
| `content` | yes | Exact body; paper-sharing packages should start with `TL;DR` |
| `images` | yes | Ordered list of 1–9 relative `.png`, `.jpg`, `.jpeg`, or `.webp` paths |
| `topics` | no | Ordered topic names without credentials or hidden metadata |
| `visibility` | no | Workflow metadata; defaults to `public` |
| `publish_mode` | no | Workflow metadata; defaults to `manual` |

Images must be regular files inside the package directory. Symlinks and path traversal are rejected.

## Canonical Publication Workflow

Set a convenience variable to the installed Skill directory. Replace `<skill-dir>` with the directory containing this `SKILL.md`:

```bash
XHS_SKILL='<skill-dir>'
PACKAGE='/absolute/path/to/share-package'
```

### 1. Prepare the content-addressed manifest

```bash
python3 "$XHS_SKILL/scripts/xhs_workflow.py" prepare \
  --source "$PACKAGE/post.json" \
  --manifest "$PACKAGE/manifest.json"
```

Completion criterion: exit code `0`; output contains `status: prepared`, a `manifest_id`, a 64-character `content_hash`, and one SHA-256 record for each image.

### 2. Validate assets and render the exact preview

```bash
python3 "$XHS_SKILL/scripts/xhs_workflow.py" validate \
  --manifest "$PACKAGE/manifest.json"

python3 "$XHS_SKILL/scripts/xhs_workflow.py" preview \
  --manifest "$PACKAGE/manifest.json"
```

Show the user all of the following, without abbreviating the body:

- exact title;
- exact body;
- topics and visibility;
- image filenames in order and image count;
- every warning;
- full `content_hash`.

Completion criterion: validation reports `valid: true`, and the user can identify exactly what will be published.

### 3. Wait for explicit confirmation

Ask for confirmation of the displayed content hash. Do not interpret earlier implementation approval as publication approval.

Valid confirmation example:

```text
确认发布 content_hash=<full hash>
```

If the user requests any edit, update `post.json` or assets, re-run `prepare`, and return to step 2. Never reuse the old approval.

Completion criterion: the user explicitly confirms the current full hash in the active conversation.

### 4. Create a short-lived approval

Only after step 3:

```bash
python3 "$XHS_SKILL/scripts/xhs_workflow.py" approve \
  --manifest "$PACKAGE/manifest.json" \
  --approval "$PACKAGE/approval.json" \
  --confirmed-by user \
  --confirmed-content-hash '<full hash copied from the confirmed preview>' \
  --ttl-seconds 1800
```

The approval file is written with mode `0600`, stores the exact separately supplied confirmed hash, and expires after 30 minutes by default. The CLI equality check strengthens the gate but cannot prove who typed the value; the agent must still obtain explicit confirmation in the active conversation.

Immediately verify it:

```bash
python3 "$XHS_SKILL/scripts/xhs_workflow.py" verify \
  --manifest "$PACKAGE/manifest.json" \
  --approval "$PACKAGE/approval.json"
```

Completion criterion: verification reports `valid: true` immediately before publication.

### 5. Publish through the safest available path

Preferred order:

1. **Official creator UI in an already authenticated browser.** Upload images in manifest order, enter the exact title and body, add topics, compare the final page with the preview, and publish only while approval remains valid.
2. **Manual handoff.** If the available browser tool cannot upload local files, give the user the ordered absolute paths and exact copy. Do not report publication until the user supplies the resulting note URL or the creator dashboard verifies it.
3. **`xhs-toolkit` only as an explicitly configured experimental backend.** Use stdio rather than unauthenticated SSE; keep its browser and Cookie storage isolated; verify current selectors; never expose its raw `smart_publish_note` to unattended agents. See `references/backends.md`.

Direct `Spider_XHS/XhsSkills` publishing is intentionally unsupported by this Skill. Its non-official creator API has a larger account-risk surface and bypasses the observable final UI review.

Completion criterion: obtain a real XHS note ID and HTTPS `xiaohongshu.com` URL, or clearly report that publication remains unverified.

### 6. Record externally verified evidence

Create a result file from `templates/publication-result.json` only after the official note page or creator UI has been checked. Do not include Cookies, response headers, or raw session data. `verification.method` must be `official_note_page` or `official_creator_ui`; `verified_by` identifies who performed that external check.

```bash
python3 "$XHS_SKILL/scripts/xhs_workflow.py" record \
  --manifest "$PACKAGE/manifest.json" \
  --approval "$PACKAGE/approval.json" \
  --result "$PACKAGE/result.json" \
  --record "$PACKAGE/publication.json"
```

The command requires a valid, unexpired approval, accepts only HTTPS XHS URLs whose final path segment matches the note ID, and requires externally obtained verification metadata. It refuses to overwrite an existing publication record. It validates and records the supplied evidence but does not contact XHS or independently prove existence.

Completion criterion: `publication.json` has `status: publication_recorded` and binds the note ID, URL, verification metadata, manifest ID, approval ID, and content hash.

## Optional Read-Only Research Backend

This branch is optional. The core publishing workflow needs no Cookie.

Prerequisites:

- separately install `cv-cat/XhsSkills` after reviewing its current source and terms;
- point `XHS_API_TOOL` to its `xhs_api_tool.py`;
- store the XHS Cookie in a local file outside Git and set mode `0600`;
- never paste the Cookie into chat or a JSON request.

Example setup:

```bash
chmod 600 /private/path/xhs-cookie.txt
export XHS_API_TOOL='/path/to/XhsSkills/skills/xhs-apis/scripts/xhs_api_tool.py'
export XHS_COOKIE_FILE='/private/path/xhs-cookie.txt'
```

Create a request from `templates/readonly-request.json`, then run:

```bash
python3 "$XHS_SKILL/scripts/xhs_readonly_adapter.py" \
  --request /path/to/request.json
```

Allowed methods:

- `pc.search_note`
- `pc.search_some_note` (`require_num` capped at 50)
- `pc.get_note_info` (HTTPS XHS URLs only)
- `pc.search_user`
- `pc.search_some_user`
- `pc.get_user_info`
- `creator.get_publish_note_info`

The adapter opens the Cookie once with no-follow semantics, validates it through the same file descriptor, injects it into a mode-`0600` temporary parameter file, invokes the upstream CLI without a shell, suppresses backend stdout/stderr, deletes the temporary directory, redacts normalized secret-like keys, and removes the exact Cookie value wherever it appears in returned strings. Requests are limited to 1 MiB, responses to 10 MiB, and JSON nesting to 32 levels. It cannot recognize transformed or encoded credentials under arbitrary benign fields, so raw upstream output remains untrusted. It does not make the upstream API official or stable.

Completion criterion: the adapter exits `0`, returns only the requested bounded result, and the exact Cookie does not appear in terminal output, request files, or process arguments.

## Failure Handling

| Failure | Required response |
|---|---|
| Manifest validation fails | Stop; fix the package and prepare a new manifest |
| Image changed after approval | Stop; regenerate preview and obtain new confirmation |
| Approval expired | Revalidate, show the same hash again, ask for fresh confirmation |
| Login/QR/2FA challenge | Stop and ask the user to complete it in the official UI |
| Publish button result is ambiguous | Inspect dashboard/note URL before any retry |
| Backend timeout or risk-control response | Stop; do not rotate proxies or bypass controls |
| Read-only adapter rejects a method | Do not widen the allowlist ad hoc; review code and threat model first |
| XHS returns unexpected private fields | Do not save raw output; redact and minimize before use |

## Common Pitfalls

1. **Treating setup approval as post approval.** Building or configuring this Skill does not authorize publishing a specific note.
2. **Passing Cookies with `--params`.** This exposes credentials through process arguments and logs. Use the private Cookie file and adapter.
3. **Editing `manifest.json` directly.** Edit `post.json` or assets and run `prepare` again.
4. **Assuming a success toast proves publication.** Verify the note in the creator dashboard or by its URL.
5. **Blindly retrying after timeout.** This can create duplicate notes.
6. **Installing upstream code into the Skill repo.** Keep third-party runtimes separate so licensing, updates, and removal remain auditable.
7. **Opening an unauthenticated MCP/SSE port.** Prefer stdio and local process boundaries.
8. **Using a primary account for experimental reverse-engineered calls.** Account restrictions are possible; choose the official UI when practical.

## Verification Checklist

- [ ] Source package contains no secret-like fields
- [ ] All images are regular files under the package root
- [ ] `validate` returns `valid: true`
- [ ] Exact title, full body, topics, ordered images, warnings, and full hash were shown
- [ ] User explicitly confirmed the current content hash
- [ ] Approval was generated after confirmation and is still valid
- [ ] Final UI or manual copy matches the approved preview
- [ ] No blind retry occurred after an ambiguous result
- [ ] Real note ID and HTTPS XHS URL were verified
- [ ] Publication record was created and not overwritten
- [ ] No Cookie, password, token, or authorization data entered chat, logs, Git, or memory
