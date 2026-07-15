# Security policy

## Supported scope

This repository protects the local integrity and confirmation flow of an XHS sharing package against accidental mutation, stale approvals, malformed inputs, and unsafe adapter invocation. Approval files are workflow evidence, not cryptographic identity credentials: a malicious process running as the same OS user can forge or replace local artifacts. The scripts do not independently contact XHS to prove that a note exists; `record` preserves externally obtained verification metadata. The project does not claim to secure third-party runtimes, the XHS website, an XHS account, or an upstream reverse-engineered API.

## Credential rules

- Never commit credentials.
- Never include Cookies, passwords, tokens, API keys, authorization headers, one-time codes, or raw session exports in an issue, pull request, terminal argument, prompt, or test fixture.
- The optional Cookie file must be outside the repository, a regular non-symlink file, and mode `0600`.
- Revoke or rotate any credential that may have been exposed.

## Reporting a vulnerability

Do not open a public issue containing exploit details or real account data. Contact the repository owner privately through the security-reporting mechanism configured on GitHub. Use synthetic fixtures and redact all platform identifiers not needed to reproduce the problem.

## Threat model

In scope:

- accidental secret insertion into workflow JSON;
- title/body/topic/image mutation after review;
- image path traversal and symlink substitution;
- approval replay after expiration or content change;
- arbitrary method dispatch through the optional adapter;
- shell injection through backend invocation;
- accidental publication-record overwrite;
- secret-like fields returned by the optional backend.

Out of scope:

- a malicious process running as the same OS user or with write access to package artifacts;
- transformed, encoded, fragmented, or newly generated credentials hidden under arbitrary benign backend fields;
- malicious code in a separately installed upstream runtime;
- compromise of the host, browser profile, or XHS account;
- XHS endpoint changes, risk controls, or account enforcement;
- platform terms or legal compliance determinations;
- UI automation reliability.
