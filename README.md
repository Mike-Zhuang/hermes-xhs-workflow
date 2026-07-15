# Hermes XHS Workflow Skill

A Hermes Agent Skill for one-shot Xiaohongshu (XHS/小红书) image-post publishing: the user explicitly says to publish, the agent binds that command to a content-addressed manifest, publishes once through an isolated XhsSkills backend, reads the creator publication list back, and records the resulting note ID and URL.

## Intended behavior

```text
User: “发布这篇到小红书”
  -> prepare and validate title/body/topics/1–9 images
  -> bind that direct command to the exact content hash
  -> atomically reserve one publication attempt
  -> call creator.post_note once
  -> read creator.get_publish_note_info
  -> record note ID, URL, timestamps, and verification evidence
```

This is on-demand automation, not scheduled autonomous posting. The Skill does not decide when or what to publish without a direct user command.

## What it enforces

- SHA-256 for every image and the canonical publication manifest;
- two authorization modes: `direct_publish_command` and `explicit_hash_confirmation`;
- short-lived, manifest-bound approval;
- approval expiry is checked again immediately before the external write;
- a deterministic private attempt record derived from `approval_id` and created before the external write, so changing output paths cannot replay an approval;
- snapshot/preflight failures occur before attempt reservation and do not consume the approval;
- no automatic retry after timeout or ambiguous outcome;
- `reconcile` performs readback only and never calls `post_note`;
- verified image bytes are exposed only through inherited unlinked descriptors; Linux snapshots are kernel write-sealed before `post_note`;
- Cookie and backend payloads stay out of argv, stdout, stderr, Git, and publication records;
- an explicit `XHS_API_PYTHON` must resolve to a verified isolated venv; Hermes's main interpreter is rejected;
- backend responses are size/depth checked and rejected if they echo the active Cookie or return an unsafe note ID;
- the general research adapter remains restricted to seven read-only methods.

It does not implement engagement automation, CAPTCHA bypass, anti-bot evasion, proxy rotation, bulk account operation, or unattended cron publishing.

## Install the Hermes Skill

```bash
mkdir -p ~/.hermes/skills/social-media
git clone https://github.com/Mike-Zhuang/hermes-xhs-workflow.git \
  ~/.hermes/skills/social-media/xhs-workflow
```

Reload the Skill index or start a new session, then verify:

```bash
hermes skills list | grep xhs-workflow
python3 ~/.hermes/skills/social-media/xhs-workflow/scripts/xhs_publish_adapter.py --help
```

## Install the publishing backend separately

The repository does not vendor `cv-cat/XhsSkills`. Pin and isolate the reviewed upstream revision:

```bash
BACKEND="$HOME/.local/share/xhs-workflow/XhsSkills"
git clone https://github.com/cv-cat/XhsSkills.git "$BACKEND"
git -C "$BACKEND" checkout 7b9df112ef75d9e8565e8582a3bc8bd2f1af7a5c

python3 -m venv "$BACKEND/.venv"
"$BACKEND/.venv/bin/python" -m pip install \
  -r "$BACKEND/skills/xhs-apis/scripts/requirements.txt"
cd "$BACKEND/skills/xhs-apis/scripts" && npm install
```

The reviewed upstream dependency lists include PyExecJS, requests, loguru, opencv-python, numpy, crypto-js, and jsdom. Review current upstream source, license status, and account risk before changing the pinned commit.

Configure paths without placing the Cookie in shell arguments:

```bash
export XHS_API_TOOL="$BACKEND/skills/xhs-apis/scripts/xhs_api_tool.py"
export XHS_API_PYTHON="$BACKEND/.venv/bin/python"
export XHS_COOKIE_FILE="$HOME/.config/xhs-workflow/creator-cookie.txt"
chmod 600 "$XHS_COOKIE_FILE"
```

Do not paste the Cookie into chat. Obtain it from an already authenticated creator session and write it directly into the private file outside Git.

## One-shot publish

The package must use `"visibility": "public"` and `"publish_mode": "immediate"`.

```bash
SKILL="$HOME/.hermes/skills/social-media/xhs-workflow"
PACKAGE="/absolute/path/to/share-package"

python3 "$SKILL/scripts/xhs_workflow.py" prepare \
  --source "$PACKAGE/post.json" \
  --manifest "$PACKAGE/manifest.json"

CONTENT_HASH=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["content_hash"])' \
  "$PACKAGE/manifest.json")

python3 "$SKILL/scripts/xhs_workflow.py" approve \
  --manifest "$PACKAGE/manifest.json" \
  --approval "$PACKAGE/approval.json" \
  --confirmed-by user \
  --authorization-mode direct_publish_command \
  --confirmed-content-hash "$CONTENT_HASH" \
  --ttl-seconds 1800

python3 "$SKILL/scripts/xhs_publish_adapter.py" publish \
  --manifest "$PACKAGE/manifest.json" \
  --approval "$PACKAGE/approval.json" \
  --record "$PACKAGE/publication.json"
```

`manifest.json`, `approval.json`, the publication record, and the deterministic attempt record are exclusive outputs and are never overwritten. The attempt path is derived internally as `.xhs-publish-attempt-<approval-id>.json` directly under the package root; callers cannot choose an alternate path.

The agent runs this sequence after an unambiguous direct command such as “发布这篇到小红书”. It must not ask for a second hash confirmation when the direct command already delegates publication of the current package.

## Ambiguous outcome and reconciliation

If `publish` returns an error after creating its deterministic attempt record, do not rerun `publish`. Use read-only reconciliation:

```bash
python3 "$SKILL/scripts/xhs_publish_adapter.py" reconcile \
  --manifest "$PACKAGE/manifest.json" \
  --approval "$PACKAGE/approval.json" \
  --record "$PACKAGE/publication.json"
```

Reconciliation remains available after the approval expires because it is read-only. It still verifies that the original backend acceptance timestamp fell inside the authorization window. If no note ID was received before the failure, automatic reconciliation cannot safely identify the note. Stop and inspect the creator account; never blindly retry.

If a verified publication record was durably created but the final attempt-state update failed, `reconcile` validates that existing record against the manifest, approval, note ID, URL, and timestamps, then repairs the attempt without another backend call.

## Optional read-only research adapter

```bash
python3 scripts/xhs_readonly_adapter.py \
  --request templates/readonly-request.json \
  --api-tool "$XHS_API_TOOL" \
  --cookie-file "$XHS_COOKIE_FILE"
```

It allows only seven documented read-only calls and continues to reject `post_note` and arbitrary method dispatch.

## Limits

- Only image posts with 1–9 PNG/JPEG/WebP files are supported by the automatic publisher.
- Automatic publishing currently supports immediate public posts only.
- XhsSkills uses reverse-engineered creator endpoints and may break or trigger platform risk controls.
- A valid login can expire; CAPTCHA, verification, or account risk controls are not bypassed.
- Creator-list readback verifies that the account reports the same note ID and title; it is not an independent public-page availability check.

## Development

Runtime for the workflow and adapters: Python 3.10+ standard library. The separate XhsSkills backend has its own dependencies.

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
```

## Attribution and licensing

This repository is MIT licensed and contains no copied source from `xhs-toolkit`, `Spider_XHS`, or `XhsSkills`. See `THIRD_PARTY.md` and `references/backends.md`.
