# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `usage`: the last known 5-hour and 7-day usage of each account, read from
  Claude Code's own cache in `.claude.json`, with the data's age. Never reads
  credentials or calls the network.
- `doctor`: read-only checks of the configuration, unfinished migrations,
  `PATH`, launchers, account directories, shared links, logins and
  login-overriding variables, with a fix for each problem; `--json` output.
- `completion bash|zsh|fish`.
- `list --json` (includes usage) and `list --names`.
- `route` and `which`: choose an account by directory. Routes generate
  `claude-auto`, which starts the account of the longest matching directory
  (physical paths), falls back to an optional default account and then to
  plain `claude`. `list`, `doctor` and completion cover routes.

### Changed

- Launchers unset `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
  `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_CODE_OAUTH_REFRESH_TOKEN`, so a
  credential exported in the shell no longer replaces every account's login.
  Run `multi-claude apply` after upgrading to regenerate existing launchers.

## [0.1.0] - 2026-09-30

First release (macOS and Linux).

### Added

- `migrate-default`: turn the default `~/.claude` into a named account without
  losing its login. The account keeps the "default" identity: its launcher
  does not set `CLAUDE_CONFIG_DIR`, so Claude Code still uses `~/.claude` (now a
  link), `~/.claude.json` and the unsuffixed keychain item. The migration has a
  resumable journal, rename or copy-and-verify modes, and refuses to run while
  any Claude Code process, IDE lock or background service could write to
  `~/.claude`.
- `init`, `add`, `proxy`, `env`, `args`, `remove`, `apply`, `list` commands;
  every write command is idempotent and supports `--dry-run`.
- Per-account proxy settings (`inherit`, `off`, port or HTTP(S) URL). SOCKS
  proxies are rejected because Claude Code does not support them.
- Extra environment variables and fixed arguments for launchers, as global
  defaults and per account. Credential-like variables are rejected.
- Warnings when settings files override a launcher's proxy, and when a fixed
  argument would swallow the arguments passed to the launcher.
- `list` shows each account's identity and whether a login exists (read-only
  keychain or credentials-file check).
- Optional shared resources linked from one directory into selected accounts,
  including `--adopt` for links made by hand.
- `install.sh` with `--config`, `--prefix` and `--uninstall`.

### Fixed (before release)

- The busy check recognizes processes started by the npm-installed native
  program (`.../@anthropic-ai/claude-code/bin/claude.exe`) and the Linux
  systemd user service of the background supervisor.

[Unreleased]: https://github.com/jakoes-wu/multi-claude/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jakoes-wu/multi-claude/releases/tag/v0.1.0
