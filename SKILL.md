---
name: xhs-workflow
description: Use when the user asks Hermes to prepare, publish, reconcile, or record a Xiaohongshu (XHS/小红书) image post. Supports one-shot unattended publication after a direct user command through an isolated XhsSkills creator backend, with content-addressed manifests, command/hash-bound approval, exclusive attempt reservation, verified asset snapshots, creator-list readback, no blind retries, credential isolation, and a separate seven-method read-only research allowlist.
version: 0.2.0
author: Chengbo Zhuang and Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [xiaohongshu, xhs, publishing, automation, workflow, safety]
    related_skills: [computer-use]
---

# Xiaohongshu Workflow

## Goal

When the user gives an unambiguous instruction such as “发布这篇到小红书”, complete the publication without asking them to fill fields or click the final button:

```text
direct user command
  -> prepare exact package
  -> bind command to manifest hash
  -> reserve one attempt
  -> upload verified snapshots and call post_note once
  -> read creator publication list
  -> return note ID/URL and save evidence
```

This is command-triggered automation. Do not create a cron job or publish on the agent's own initiative.

## Authorization semantics

Two modes exist:

- `direct_publish_command`: the user directly delegates publication of the current, unambiguous package. This command is the one-time publication authorization. Prepare and validate the package, create the approval with this mode, and continue without asking for a second hash confirmation.
- `explicit_hash_confirmation`: use when the user asks to inspect and confirm the final artifact before publication. Show the complete preview and wait for the full hash confirmation.

A request to install, test, design, or configure the Skill is not authorization to publish a real post. A direct instruction to create and publish a specified post is authorization for the resulting package, provided the target account and content are unambiguous.

Any content or image change after approval invalidates it. A direct command authorizes one attempt only.

## Supported automatic publication

- 1–9 local PNG, JPEG, or WebP images;
- immediate public post;
- exact title, body, topics, and image order from the manifest;
- separate XhsSkills `creator.post_note` backend;
- creator-list readback by exact note ID and title.

Not supported:

- video publishing;
- scheduled/cron publishing without a separate explicit request;
- likes, follows, comments, DMs, or account farming;
- CAPTCHA, verification, anti-bot, or risk-control bypass;
- blind retry after timeout or uncertain state;
- arbitrary XhsSkills method dispatch.

## Credential contract

Never put Cookies, passwords, tokens, one-time codes, session exports, or authorization headers in prompts, chat, process arguments, Git, manifests, approvals, attempt files, records, logs, or memory.

Required environment variables:

```bash
export XHS_API_TOOL='/pinned/XhsSkills/skills/xhs-apis/scripts/xhs_api_tool.py'
export XHS_API_PYTHON='/pinned/XhsSkills/.venv/bin/python'
export XHS_COOKIE_FILE="$HOME/.config/xhs-workflow/creator-cookie.txt"
chmod 600 "$XHS_COOKIE_FILE"
```

The Cookie file must be a regular non-symlink UTF-8 file with mode `0600`. It is read internally and injected into a mode-`0600` temporary JSON file; it never enters argv. Backend stdout/stderr are suppressed, and backend error messages are not returned.

Do not install third-party dependencies into Hermes's own Python. Use the isolated `XHS_API_PYTHON` venv. The reviewed XhsSkills commit is documented in `README.md` and `references/backends.md`.

## Package layout

Keep working packages outside the Skill repository:

```text
/path/to/package/
├── post.json
├── 01.png
├── ...
```

Required `post.json` shape:

```json
{
  "title": "exact title",
  "content": "exact body",
  "images": ["01.png"],
  "topics": ["topic"],
  "visibility": "public",
  "publish_mode": "immediate"
}
```

Images must be regular files below the package root. Symlinks, traversal, extension/signature mismatch, and files over the publisher's 30 MiB per-image bound are rejected.

## One-shot workflow

Set paths:

```bash
XHS_SKILL='<directory containing this SKILL.md>'
PACKAGE='/absolute/path/to/package'
```

### 1. Prepare and validate

```bash
python3 "$XHS_SKILL/scripts/xhs_workflow.py" prepare \
  --source "$PACKAGE/post.json" \
  --manifest "$PACKAGE/manifest.json"

python3 "$XHS_SKILL/scripts/xhs_workflow.py" validate \
  --manifest "$PACKAGE/manifest.json"
```

Require exit code `0` and `valid: true`. The manifest binds title, body, topics, visibility, publish mode, image order, image sizes, and SHA-256 hashes.

### 2. Choose authorization mode

If the current user message directly says to publish the current package, use `direct_publish_command`. Do not ask the user to repeat a hash.

If publication was not directly delegated, stop. If the user requested preview-first confirmation, render:

```bash
python3 "$XHS_SKILL/scripts/xhs_workflow.py" preview \
  --manifest "$PACKAGE/manifest.json"
```

Show the complete title, body, topics, ordered images, warnings, and full hash; wait for explicit hash confirmation.

### 3. Create short-lived approval

Read the current manifest hash locally and pass it as a separate argument. It binds the authorization mode to the exact artifact; it does not claim that a direct-command user manually typed the hash.

```bash
CONTENT_HASH=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["content_hash"])' \
  "$PACKAGE/manifest.json")

python3 "$XHS_SKILL/scripts/xhs_workflow.py" approve \
  --manifest "$PACKAGE/manifest.json" \
  --approval "$PACKAGE/approval.json" \
  --confirmed-by user \
  --authorization-mode direct_publish_command \
  --confirmed-content-hash "$CONTENT_HASH" \
  --ttl-seconds 1800
```

For preview-first mode, replace the authorization mode with `explicit_hash_confirmation` and use only the hash actually confirmed by the user.

Immediately verify:

```bash
python3 "$XHS_SKILL/scripts/xhs_workflow.py" verify \
  --manifest "$PACKAGE/manifest.json" \
  --approval "$PACKAGE/approval.json"
```

### 4. Publish once

```bash
python3 "$XHS_SKILL/scripts/xhs_publish_adapter.py" publish \
  --manifest "$PACKAGE/manifest.json" \
  --approval "$PACKAGE/approval.json" \
  --attempt "$PACKAGE/publish-attempt.json" \
  --record "$PACKAGE/publication.json"
```

The publisher:

1. verifies approval and current asset hashes;
2. creates private immutable-by-path snapshots of the approved bytes;
3. atomically creates `publish-attempt.json` before external write;
4. calls only `creator.post_note` once;
5. extracts the returned note ID;
6. polls only `creator.get_publish_note_info`;
7. accepts readback only when note ID and title both match;
8. writes `publication.json` and marks the attempt `verified`.

Success criterion: exit code `0`, record status `publication_recorded`, and a note ID/URL. Report that creator readback verified the note; do not overstate this as independent public-page availability.

### 5. Reconcile uncertain outcomes

If `publish-attempt.json` exists and `publish` failed, never call `publish` again with another path. Use:

```bash
python3 "$XHS_SKILL/scripts/xhs_publish_adapter.py" reconcile \
  --manifest "$PACKAGE/manifest.json" \
  --approval "$PACKAGE/approval.json" \
  --attempt "$PACKAGE/publish-attempt.json" \
  --record "$PACKAGE/publication.json"
```

`reconcile` invokes only `creator.get_publish_note_info`; it cannot publish. If the attempt contains no note ID or readback cannot find the exact note, stop and report `outcome_unknown`. Do not bypass this by deleting/renaming the attempt or choosing a different attempt path.

## Login and risk controls

The backend requires an existing valid creator Cookie. If authentication expires, ask the user to log into the official creator site and refresh the private Cookie file outside chat. Never request the Cookie value in conversation.

If XHS returns CAPTCHA, verification, account-risk, or access-control challenges, stop and report the blocker. Never bypass them. “Unattended” means no manual action in the normal valid-session path; it cannot guarantee operation through platform-enforced human verification.

## Read-only research adapter

`scripts/xhs_readonly_adapter.py` remains separate from the publisher. It exposes only:

- `pc.search_note`
- `pc.search_some_note`
- `pc.get_note_info`
- `pc.search_user`
- `pc.search_some_user`
- `pc.get_user_info`
- `creator.get_publish_note_info`

It rejects `post_note`, upload methods, engagement methods, unbounded publication-list retrieval, unknown parameters, and arbitrary dispatch.

## Verification and records

`creator_api_readback` means the creator account listing returned the same note ID and title. `official_creator_ui` and `official_note_page` remain valid evidence methods for manual verification.

Approval files and attempt files are local workflow evidence, not cryptographic proof of user identity. A malicious process with the same OS-user permissions is outside the threat model. Read `SECURITY.md` and `references/backends.md` before using a primary account.
