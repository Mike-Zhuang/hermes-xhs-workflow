# Security policy

## Supported scope

This repository protects the local integrity and one-shot execution flow of an XHS image-post package against accidental mutation, stale authorization, duplicate retries, malformed inputs, unsafe file paths, and accidental credential disclosure.

The automatic publisher can perform a real external write through a separately installed XhsSkills runtime. It uses a reverse-engineered creator endpoint; this repository does not make that endpoint official, stable, policy-compliant, or immune to account enforcement.

Approval and attempt files are workflow evidence, not cryptographic identity credentials. A malicious process running as the same OS user can forge, replace, delete, or redirect local artifacts.

## External effect

`xhs_publish_adapter.py publish` uploads media and attempts to create a public XHS post. Before calling it, the agent must have a direct user publication command or an explicit hash confirmation for the exact current package.

A direct command authorizes one attempt. Local snapshot/preflight failures happen before reservation. After preflight, the publisher rechecks expiry, derives a fixed attempt path from `approval_id`, and creates it immediately before invoking `creator.post_note`; choosing a different record path cannot replay the approval. If a timeout or ambiguous response occurs, the post may already exist. Do not delete or bypass the attempt file and do not retry `publish`; use the read-only `reconcile` command. If record creation succeeded but final attempt persistence failed, reconciliation strictly validates the existing record and repairs the attempt without another backend call.

## Credential rules

- Never commit credentials.
- Never include Cookies, passwords, tokens, API keys, authorization headers, one-time codes, or raw session exports in issues, pull requests, shell arguments, prompts, chat, logs, test fixtures, manifests, approvals, attempt files, publication records, or memory.
- Keep `XHS_COOKIE_FILE` outside Git as a regular non-symlink UTF-8 file with mode `0600`.
- Keep the third-party backend in a separate pinned directory and venv selected by `XHS_API_PYTHON`.
- Revoke or rotate any credential that may have been exposed.

The adapter suppresses third-party stdout/stderr and does not return upstream error messages because they may contain credentials. Cookie-bearing parameter files live in a private temporary directory. Verified media bytes are exposed only through inherited, unlinked descriptors during `post_note`; Linux uses kernel write seals, while the macOS fallback uses an unlinked mode-`0400` inode opened read-only. Successful responses are rejected if any nested string contains the active Cookie.

## Threat model

In scope:

- accidental secret insertion into workflow JSON;
- title/body/topic/image mutation after authorization;
- image path traversal, symlink substitution, extension spoofing, and check/use replacement;
- expired, malformed, or content-mismatched approvals;
- distinguishing direct-command authorization from explicit hash confirmation;
- accidental duplicate publication after timeout or readback failure;
- arbitrary method dispatch through either adapter;
- shell injection through backend invocation;
- accidental manifest, approval, attempt, or publication-record overwrite;
- Cookie exposure through argv, backend stdout/stderr, backend error messages, or successful response fields;
- malformed, oversized, or deeply nested JSON responses;
- running the third-party backend in a separate Python environment.

Out of scope:

- a malicious process running as the same OS user or with write access to package, approval, attempt, backend, or credential files;
- a caller deliberately deleting or forging artifacts with the same OS-user permissions;
- transformed, encoded, fragmented, or newly generated credentials hidden under arbitrary benign backend fields;
- malicious or compromised code in the separately installed XhsSkills runtime or its dependencies;
- compromise of the host, XHS account, browser profile, or upstream supply chain;
- XHS endpoint/signature changes, moderation, CAPTCHA, verification, risk controls, account suspension, or policy enforcement;
- legal or platform-terms determinations;
- public availability of a note after creator-list readback.

## Verification semantics

- `creator_api_readback`: the creator publication-list endpoint returned the same note ID and exact title.
- `official_creator_ui`: an official creator UI was checked externally.
- `official_note_page`: the public note page was checked externally.

A successful `post_note` response alone is not recorded as verified. Creator-list readback is stronger than accepting the write response, but it is not an independent public-page check.

## Reporting a vulnerability

Do not open a public issue containing exploit details or real account data. Contact the repository owner privately through the security-reporting mechanism configured on GitHub. Use synthetic fixtures and redact all platform identifiers not needed to reproduce the problem.
