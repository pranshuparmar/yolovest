# Security Policy

YoloVest is self-hosted software that holds broker API credentials and can place
real trades. Security issues are taken seriously. Please help keep users safe by
reporting responsibly.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Instead, report privately using **[GitHub's private vulnerability reporting](https://github.com/pranshuparmar/yolovest/security/advisories/new)**
("Report a vulnerability" under the repository's *Security* tab). If that is
unavailable, email **pranshu.parmar@gmail.com** with the details.

Please include:

- A description of the vulnerability and its impact.
- Steps to reproduce, or a proof of concept.
- Affected component(s) and version/commit.
- Any suggested remediation, if you have one.

You can expect an initial acknowledgement within a reasonable time for a
personal open-source project. As this is a single-maintainer project run on a
best-effort basis, please be patient with response and fix timelines.

## Supported Versions

This is a pre-1.0, single-maintainer project. Only the latest `main` branch
receives security fixes. There is no backporting to older commits or tags.

## Scope and Hardening Notes

YoloVest already implements several security controls. When assessing or
reporting, the following are known design points:

- **Dashboard authentication** — the dashboard ships with a default password
  (`yolovest`) that the application **refuses to run with in practice**; you are
  required to change it. Exposing the dashboard to the internet without changing
  the default, or without a strong password, is operator misconfiguration rather
  than a code vulnerability — but reports about the auth mechanism itself are
  welcome.
- **Model artifact signing** — uploaded/imported model `.pkl` files are
  HMAC-signed via `MODEL_SIGNING_KEY` to close the malicious-pickle RCE on the
  model-upload boundary. Set this key if you use model upload/import. Reports of
  ways to bypass signature verification are in scope.
- **Secrets** — secrets (broker keys, Gemini key, Telegram token, signing key)
  are supplied via environment variables / `.env` and are never committed. If
  you find a secret committed to the repository or its history, report it.
- **Known dependency exception** — `autobahn==19.11.2` is hard-pinned
  transitively by `kiteconnect` and carries an unpatchable advisory
  (PYSEC-2020-25, redirect-header injection). It is used only on the
  authenticated Kite ticker WebSocket, not on any public web surface, and is
  explicitly ignored in CI's dependency audit. New findings beyond this
  documented exception are in scope.

## Operator Responsibilities

Because YoloVest is self-hosted, several aspects of security are the operator's
responsibility:

- Changing the default dashboard password and serving the dashboard over HTTPS.
- Keeping broker/API credentials confidential and rotating them if exposed.
- Restricting network access to the host and dashboard.
- Reviewing configuration before enabling **live** trading mode.
