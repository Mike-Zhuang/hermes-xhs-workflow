# Backend boundaries

## Default backend: official UI or manual handoff

The core workflow never needs an XHS credential. It prepares and verifies artifacts, then lets an already authenticated official creator UI perform the external action. This keeps the final title, body, topics, image order, visibility, and any platform notices observable before publishing.

The current Hermes browser tool may not expose local file upload in every environment. If upload is unavailable, hand the user the exact copy and ordered absolute image paths. A handoff is not a successful publication; record it only after a real note URL is verified.

## `cv-cat/XhsSkills` / `cv-cat/Spider_XHS`

The optional read-only adapter interoperates with a separately installed XhsSkills CLI through its `--params-file` option. No upstream source is included here.

Why it is optional:

- it calls reverse-engineered, non-official endpoints;
- endpoint signatures and risk-control behavior can change without notice;
- the examined repositories did not expose a clear reusable license file at review time, despite an MIT badge in one README and a non-commercial-use statement;
- upstream examples may place Cookies in command-line JSON;
- issue reports include breakage and account restrictions.

Controls implemented here:

- seven explicit read-only methods;
- no arbitrary method dispatch;
- method-specific parameter allowlists;
- bounded `page` and `require_num` values;
- HTTPS XHS host validation for note URLs;
- mode-`0600`, non-symlink Cookie file opened once with `O_NOFOLLOW` where available and validated through `fstat`;
- mode-`0600` temporary parameter file;
- subprocess argument vector without a shell;
- suppressed upstream stdout/stderr;
- normalized secret-key redaction plus exact Cookie-value removal from returned strings;
- 1 MiB request, 10 MiB response, and 32-level JSON nesting limits;
- temporary-directory deletion after each call.

These controls reduce accidental exposure. They do not make unofficial access authorized or immune to platform enforcement.

## `aki66938/xhs-toolkit`

`xhs-toolkit` offers FastMCP and Selenium-based creator workflows and is structurally compatible with a Hermes stdio MCP connection. It is not bundled or enabled because:

- upstream states that development has stopped;
- current open issues report broken selectors, uploads, mode switching, task execution, and false-positive publication success;
- its Cookie store is plaintext JSON;
- SSE can bind to `0.0.0.0` without an obvious authentication boundary;
- raw publishing tools do not enforce this repository's manifest-bound confirmation gate.

If a user explicitly chooses to experiment:

1. pin a reviewed commit in an isolated environment;
2. use stdio, not a publicly reachable SSE service;
3. protect any Cookie file with mode `0600` and keep it outside Git;
4. wrap or remove raw write tools;
5. map the exact manifest into the browser task;
6. call it only after `verify` returns `valid: true`;
7. verify the resulting note URL instead of trusting the task status.

Do not treat the upstream MCP as a drop-in production publisher.

## Why direct creator-API publishing is excluded

Direct `creator.post_note` publishing could technically consume the manifest, but it removes the final official-UI inspection and carries higher endpoint/account risk. Adding such a backend would require a separate explicit design review, tests against a non-primary account, an upstream licensing decision, and a new user confirmation specific to that backend.
