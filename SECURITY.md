# Security Policy

## Supported versions

Only the latest release receives security fixes.

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub Security Advisories](https://github.com/jakoes-wu/multi-claude/security/advisories/new)
instead of opening a public issue. Include the version, your platform and the
steps to reproduce. You should receive a reply within a week.

## Scope notes

- multi-claude never writes, copies, refreshes or deletes Claude Code
  credentials (keychain items or `.credentials.json`).
- One command reads a credential: `multi-claude list` reads each account's
  OAuth access token (`security find-generic-password -a <user> -w -s
  <service>` on macOS, `.credentials.json` elsewhere) and sends it in one
  `GET https://api.anthropic.com/api/oauth/profile` request to look up the
  account's plan. The token is kept in memory only, is never logged or
  printed, goes to no other host, and travels through the account's
  configured proxy; a failing proxy is never bypassed. Expired tokens are not
  sent. `list --offline` and `list --names` read no token and make no
  request.
- Every other command only checks whether a login exists
  (`security find-generic-password -a <user> -s <service>` without `-w` or
  `-g`) and makes no network connection.
- Credential-like environment variables (API keys, tokens, secrets,
  passwords) are rejected by `multi-claude env`, because launchers and
  `config.json` are plain files.
- Generated launchers are world-readable; proxy URLs with credentials are
  rejected for that reason.
- New account directories are created with mode `0700`.
