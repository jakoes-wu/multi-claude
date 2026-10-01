# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
