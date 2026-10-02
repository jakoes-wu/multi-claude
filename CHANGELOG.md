# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- README FAQ: why there is no command that switches the account of plain
  `claude`, and what to use instead.

## [0.6.0] - 2026-10-03

### Added

- `code NAME [PATH] [-- ARGS ...]` (experimental) opens a separate VS Code
  instance with the account's environment and its own user data directory
  (`<root>/.apps/<account directory>/vscode`), so the Claude Code extension
  uses that account.

## [0.5.0] - 2026-10-02

### Added

- `run [NAME] [-- COMMAND ...]` runs any command with the environment of an
  account's launcher (without a command: `claude` with the fixed arguments;
  without NAME: the account `claude-auto` would use).
- `path NAME` prints an account's directory.
- `restore NAME` undoes `migrate-default`: it moves the account back to
  `~/.claude` and unregisters it; the login is kept. It can be rerun after an
  interruption.
- `migrate-default` without a name uses the email address of the login in
  `~/.claude.json`.
- `add NAME --config-from OTHER` copies `settings.json` from another account
  once.

### Changed

- README: install from PyPI with `pipx install multi-claude-cli`, and a PyPI
  badge.
- README: install with Homebrew, `brew install jakoes-wu/tap/multi-claude`.

## [0.4.0] - 2026-10-02

### Added

- Every release publishes `multi-claude-<tag>.tar.gz` and `SHA256SUMS`
  (workflow `release.yml`, which also checks that `__version__` matches the
  tag). `install.sh` downloads that archive and verifies its SHA-256 before
  installing; releases without it, and branches, are installed with a note
  that they are unverified. New variables: `MULTI_CLAUDE_SHA256`,
  `MULTI_CLAUDE_REQUIRE_CHECKSUM=1`, `MULTI_CLAUDE_CODELOAD`.
- The package is published to PyPI as `multi-claude-cli` (workflow
  `pypi.yml`); the command is still `multi-claude`.
- The README opens with a short example and a demo animation, has a
  four-step quick start and an FAQ. `scripts/make-assets.py` regenerates the
  animation and the social preview image in a temporary HOME;
  `docs/assets/` is left out of release archives.

### Changed

- `MULTI_CLAUDE_REF=vX.Y.Z` fails when the release cannot be read, instead
  of falling back to an unverified download.

## [0.3.1] - 2026-10-02

### Changed

- `statusline install` and the README say that running sessions keep the
  old status line; usage is recorded from new sessions after their first
  reply.
- `add --shared NAME` (account name after the options) gets an error that
  says to put the name first, instead of "name is required".
- `docs/analysis/roadmap.md` records the v0.3.0 changes and explains that
  its v0.2/v0.3/v0.4 groups are not release numbers.

## [0.3.0] - 2026-10-02

### Added

- `set NAME ...`: change an existing account (proxy, sharing, shared-item
  opt-outs); unlike `add` it never creates one.
- `--shared` uses `~/.claude-shared` when no shared directory is set, and
  `--shared DIR` makes DIR the shared directory for every account. A note
  says when the shared directory has none of the shared items yet.
- `login NAME [ARG ...]`: sign in to an account by running `claude auth login`
  with its environment; an e-mail-like account name is passed as `--email`.
- Running `multi-claude` without arguments prints a short guide (no
  configuration yet) or the account list.
- A mistyped command gets a suggestion (`did you mean 'list'?`).
- After `add`, an account without a login gets a hint to run `login`.

### Changed

- Write commands print only the items that change, or `nothing to change`;
  `--verbose` lists everything as before, including shared items that are
  missing from the shared directory.
- A proxy value that is neither a port nor a URL gets an error that shows
  the accepted forms.
- README: the quick start begins with `add` and `login`; migrating
  `~/.claude` is described as optional. The rest is grouped into everyday
  tasks, migration and a reference section.
- `--help` lists the commands by purpose, with examples; each command's
  `--help` starts with what it does.
- `list` shows a short table (login, proxy, sharing, 5-hour and 7-day usage
  and any problem); `list --verbose` shows the previous full listing.
- When the launcher directory is not on `PATH`, `install.sh`, `add` and
  `doctor` print the exact line to add for zsh, bash or fish.

## [0.2.2] - 2026-10-01

### Changed

- Documentation only: `docs/analysis/roadmap.md` now records the upgrade
  plan and what each release delivered; design documents point to it.

## [0.2.1] - 2026-10-01

### Changed

- The statusline hook starts the original status-line command right after
  reading its input and records usage in a detached background process, so
  the status line no longer waits for the capture. The hook path loads only a
  small module before handing over. Measured overhead compared with running
  the status-line command directly: about 30 ms, down from about 120 ms.
- `install.sh` writes the interpreter behind a pyenv, asdf or mise shim into
  the launcher instead of the shim itself. Run the installer again to pick
  this up.

## [0.2.0] - 2026-10-01

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
- `rename OLD NEW`: rename an account and its launcher while keeping its
  directory (accounts gain an optional `dir` field), so the login stays;
  routes follow the new name.
- `mcp NAME [ARG ...]`: run `claude mcp` with the account's environment
  (configuration directory, proxy, extra variables) but without its fixed
  arguments.
- `statusline install|uninstall FILE`: wrap an existing `statusLine` command
  so that each redraw records the account's 5-hour and 7-day usage from the
  status-line data; the original command still produces the output. `usage`
  and `list --json` use whichever is newer, the cache or the status line, and
  report it in a new `source` field.
- `handoff TARGET [--from NAME] [--session ID] [--force]`: copy a session
  (its `.jsonl` and the folder next to it) to the same project of another
  account and print `cd … && claude-<target> --resume <id>`. Inside a
  Claude session it takes the current session; a different copy already in
  the target is refused (exit 3) unless `--force` backs it up first.
- `add NAME --shared-exclude ITEM` / `--shared-include ITEM`: keep a shared
  item out of one account (the link multi-claude made is removed) or undo
  it. Accounts gain an optional `shared_exclude`; `list` shows it.

### Changed

- Launchers unset `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
  `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_CODE_OAUTH_REFRESH_TOKEN`, so a
  credential exported in the shell no longer replaces every account's login.
  Run `multi-claude apply` after upgrading to regenerate existing launchers.
- `shared.items` and `init --shared-items` reject items that hold one
  account's own state: `.credentials.json`, `.claude.json`,
  `settings.local.json`, `projects`, `history.jsonl`, `file-history`,
  `sessions`, `session-env`, `shell-snapshots` and `todos`. Remove such an
  item from `config.json` if an older version accepted it.

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

[Unreleased]: https://github.com/jakoes-wu/multi-claude/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/jakoes-wu/multi-claude/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/jakoes-wu/multi-claude/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/jakoes-wu/multi-claude/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/jakoes-wu/multi-claude/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/jakoes-wu/multi-claude/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/jakoes-wu/multi-claude/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/jakoes-wu/multi-claude/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jakoes-wu/multi-claude/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jakoes-wu/multi-claude/releases/tag/v0.1.0
