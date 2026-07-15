# Hermes XHS Workflow Skill

A safety-gated Hermes Agent Skill for preparing and recording Xiaohongshu (XHS/小红书) publication packages, with an optional least-privilege read-only adapter for `cv-cat/XhsSkills`.

## What it does

- builds an immutable manifest from a title, body, topics, and 1–9 local images;
- hashes every asset and the canonical publication payload;
- renders the exact preview an agent must show before confirmation;
- creates short-lived approval only when the caller supplies the exact content hash the user confirmed;
- rejects post-approval changes, malformed approvals, and expired approvals;
- records a syntactically validated XHS note ID/URL together with externally obtained verification metadata;
- optionally invokes seven allowlisted read-only XhsSkills methods without placing Cookies in process arguments.

It intentionally does **not** contain an autonomous publisher, engagement automation, CAPTCHA bypass, proxy rotation, or upstream reverse-engineered source code. The `record` command validates the supplied evidence schema and preserves it; it does not contact XHS or independently prove that a note exists.

## Install as a Hermes Skill

Clone the complete repository so scripts and templates are installed with `SKILL.md`:

```bash
mkdir -p ~/.hermes/skills/social-media
git clone https://github.com/Mike-Zhuang/hermes-xhs-workflow.git \
  ~/.hermes/skills/social-media/xhs-workflow
```

Start a new Hermes session so the skill index reloads, then verify:

```bash
hermes skills list | grep xhs-workflow
python3 ~/.hermes/skills/social-media/xhs-workflow/scripts/xhs_workflow.py --help
```

## Quick start

```bash
cp -R ~/.hermes/skills/social-media/xhs-workflow/templates /tmp/xhs-package
# Replace the sample post and image names with real local assets.

python3 ~/.hermes/skills/social-media/xhs-workflow/scripts/xhs_workflow.py prepare \
  --source /tmp/xhs-package/post.json \
  --manifest /tmp/xhs-package/manifest.json

python3 ~/.hermes/skills/social-media/xhs-workflow/scripts/xhs_workflow.py preview \
  --manifest /tmp/xhs-package/manifest.json
```

Read `SKILL.md` for the mandatory confirmation and publication sequence.

## Optional read-only backend

The adapter can call a separately installed `cv-cat/XhsSkills` runtime. It does not vendor that project and does not enable its write methods.

```bash
chmod 600 /private/path/xhs-cookie.txt
export XHS_API_TOOL='/path/to/XhsSkills/skills/xhs-apis/scripts/xhs_api_tool.py'
export XHS_COOKIE_FILE='/private/path/xhs-cookie.txt'

python3 scripts/xhs_readonly_adapter.py \
  --request templates/readonly-request.json
```

Do not paste Cookies into chat, JSON requests, shell arguments, issues, or Git. Review `references/backends.md`, `SECURITY.md`, and the current upstream terms before enabling unofficial API access.

## Development

Runtime dependency: Python 3.10+ standard library.

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
```

## Attribution and licensing

This repository is MIT licensed. It contains no copied source from `xhs-toolkit`, `Spider_XHS`, or `XhsSkills`; see `THIRD_PARTY.md` for interoperability notices.
