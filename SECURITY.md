# Security Policy

## Supported versions

Only the latest release receives security fixes.

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub Security Advisories](https://github.com/jakoes-wu/multi-claude/security/advisories/new)
instead of opening a public issue. Include the version, your platform and the
steps to reproduce. You should receive a reply within a week.

## Scope notes

- multi-claude never reads, writes, copies or deletes Claude Code credentials
  (keychain items or `.credentials.json`). Its only keychain call is
  `security find-generic-password -a <user> -s <service>` without `-w` or
  `-g`, which checks whether an item exists and never prints its secret.
- Credential-like environment variables (API keys, tokens, secrets,
  passwords) are rejected by `multi-claude env`, because launchers and
  `config.json` are plain files.
- Generated launchers are world-readable; proxy URLs with credentials are
  rejected for that reason.
- New account directories are created with mode `0700`.
