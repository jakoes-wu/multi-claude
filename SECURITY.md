# Security Policy

## Supported versions

Only the latest release receives security fixes.

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub Security Advisories](https://github.com/jakoes-wu/multi-claude/security/advisories/new)
instead of opening a public issue. Include the version, your platform and the
steps to reproduce. You should receive a reply within a week.

## Scope notes

- multi-claude never copies, exports or deletes Claude Code credentials
  (keychain items or `.credentials.json`).
- One command writes a credential: when an account's access token has
  expired, `multi-claude list` renews it. It takes the locks Claude Code
  uses (`<config dir>/.oauth_refresh.lock` and `<config dir>.lock`), posts
  the refresh token to `https://platform.claude.com/v1/oauth/token`, and
  saves the new tokens where the old ones were (`security -i` with the
  content on stdin on macOS, an atomic replace of `.credentials.json`
  elsewhere), leaving every other field untouched. It writes only if the
  stored refresh token is still the one it sent. `list --no-refresh` and
  `list --offline` never write.
- One command reads a credential: `multi-claude list` reads each account's
  OAuth access token (`security find-generic-password -a <user> -w -s
  <service>` on macOS, `.credentials.json` elsewhere) and sends it in one
  `GET https://api.anthropic.com/api/oauth/profile` request to look up the
  account's plan. Tokens are kept in memory only, are never logged or
  printed, go to no host other than these two, and travel through the
  account's configured proxy; a failing proxy is never bypassed. Expired
  access tokens are not sent. `list --offline` and `list --names` read no
  token and make no request.
- One command prints conversation content: `multi-claude sessions` reads the
  start and the end of each session file (`projects/*/*.jsonl`) and prints
  each session's title, or the beginning of its first prompt when it has no
  title, to the terminal (or to stdout with `--json`). Control characters
  are removed before printing. Nothing is saved or sent anywhere; no
  credential is read.
- `multi-claude artifacts` reads the session files of every account and
  prints the title, description, URL and local source path of each
  artifact published from them. Control characters are removed before
  printing. Nothing is saved or sent anywhere; no credential is read.
- `multi-claude handoff` appends one line per copy to
  `~/.config/multi-claude/handoffs.jsonl` (mode 0600): the session id,
  the project folder name, the two account directories, and the size and
  SHA-256 hash of each copied file. No conversation content is stored.
- Every other command only checks whether a login exists
  (`security find-generic-password -a <user> -s <service>` without `-w` or
  `-g`) and makes no network connection.
- Credential-like environment variables (API keys, tokens, secrets,
  passwords) are rejected by `multi-claude env`, because launchers and
  `config.json` are plain files.
- Generated launchers are world-readable; proxy URLs with credentials are
  rejected for that reason.
- New account directories are created with mode `0700`.
