# multi-claude

**English** | [简体中文](README.zh-CN.md)

[![Release](https://img.shields.io/github/v/release/jakoes-wu/multi-claude)](https://github.com/jakoes-wu/multi-claude/releases)
[![CI](https://github.com/jakoes-wu/multi-claude/actions/workflows/ci.yml/badge.svg)](https://github.com/jakoes-wu/multi-claude/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/multi-claude-cli)](https://pypi.org/project/multi-claude-cli/)
![Python](https://img.shields.io/badge/python-3.8%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Use several [Claude Code](https://code.claude.com/docs) accounts on one machine, at the same time. Each account keeps its own login, settings and history and, if you like, its own proxy. No more logging out and in again.

```sh
claude-work        # Claude Code with your work account
claude-personal    # Claude Code with your personal account, in another terminal
multi-claude       # your accounts, their logins and usage
```

![multi-claude demo: add two accounts and list them](https://raw.githubusercontent.com/jakoes-wu/multi-claude/main/docs/assets/demo.gif)

<sub>The accounts in the demo are examples.</sub>

## How it works

Claude Code keeps its settings, login and history in the directory named by `CLAUDE_CONFIG_DIR` (default `~/.claude`). The official documentation suggests one directory per account. multi-claude manages those directories for you and gives every account its own launcher command, optionally with its own proxy, environment variables and fixed arguments:

```text
claude-work       -> CLAUDE_CONFIG_DIR=~/.cc/work      HTTPS_PROXY=http://127.0.0.1:7901
claude-personal   -> CLAUDE_CONFIG_DIR=~/.cc/personal  (inherits your shell's proxy settings)
claude-main       -> your original ~/.claude account, still logged in
claude            -> unchanged
```

The launchers are plain shell scripts. They keep working even if you uninstall multi-claude.

## Features

- **Migrate the default account** — move your existing `~/.claude` into the account root without logging out. `claude` keeps working exactly as before.
- **Add accounts** — create an account directory and a `claude-<name>` launcher, or adopt a directory you already have.
- **Per-account proxy** — a local port, an HTTP(S) proxy URL, `off`, or `inherit`.
- **Extra environment variables and fixed arguments** — global defaults plus per-account values, for example `--settings` or `--permission-mode`.
- **One-step deployment** — `install.sh --config accounts.json` installs the tool and creates every account in the file.
- **Idempotent** — every command can be re-run safely. Write commands print only what changes (`--verbose` lists everything); conflicts with files multi-claude does not own are reported without changing anything; an interrupted migration resumes where it stopped.
- **Optional shared resources** — link `CLAUDE.md`, `skills`, `agents` and `commands` from one shared directory into selected accounts.
- **Hands off your credentials** — multi-claude never reads, copies or deletes logins. Its only keychain call checks whether an item exists.

## Requirements

- macOS or Linux (Windows is planned; inside WSL, use the Linux instructions)
- Python 3.8 or newer (standard library only)
- Claude Code (`claude`) on your `PATH`

## Installation

From a clone:

```sh
git clone https://github.com/jakoes-wu/multi-claude.git
cd multi-claude
./install.sh
```

Or directly:

```sh
curl -fsSL https://raw.githubusercontent.com/jakoes-wu/multi-claude/main/install.sh | sh
```

The tool goes to `~/.local/share/multi-claude` and the `multi-claude` command to `~/.local/bin`. Use `--prefix DIR` to install somewhere else. Make sure `~/.local/bin` is on your `PATH`; the installer only prints a hint and never edits your shell profile.

The `multi-claude` command runs with the `python3` found on `PATH` at install time. If that is a version-manager shim (pyenv, asdf, mise), the installer writes the interpreter behind it instead: a shim adds tens of milliseconds to every start, and with the statusline hook installed that happens on every status-line redraw. Run the installer again after removing that Python version.

Alternatively, from PyPI: `pipx install multi-claude-cli` (upgrade with `pipx upgrade multi-claude-cli`). The command is still `multi-claude`. Use either pipx or the installer, not both: they put the same command in `~/.local/bin`.

With Homebrew: `brew install jakoes-wu/tap/multi-claude` (upgrade with `brew upgrade multi-claude`). It installs into Homebrew's own prefix, so remove other installations first to avoid two `multi-claude` commands on your `PATH`.

**Verified downloads.** From v0.4.0 on, every release publishes `multi-claude-<tag>.tar.gz` and `SHA256SUMS`. The remote installer downloads that archive and checks its SHA-256 before installing anything; a mismatch stops the installation. Branches and older releases are installed unverified (the installer says so); set `MULTI_CLAUDE_REQUIRE_CHECKSUM=1` to refuse them. The checksum is published next to the archive, so it protects against a damaged or altered download, not against a compromised GitHub account.

Run `./install.sh --help` for all options.

## Quick start

### 1. Create one account per login

```sh
multi-claude add work --proxy 7901   # an account "work" with its own launcher, through a local proxy on port 7901
multi-claude add personal            # another one that uses your shell's proxy settings
```

### 2. Log in once per account

```sh
multi-claude login work              # runs `claude auth login` for that account
multi-claude login personal
```

### 3. Use the launchers instead of `claude`

```sh
claude-work                          # in one terminal
claude-personal                      # in another, at the same time
```

### 4. Check that everything is right

```sh
multi-claude                         # accounts, logins, usage and any problem
multi-claude doctor                  # a full check, with a fix for each problem
```

### Already using Claude Code?

Your existing `~/.claude` is not touched and keeps working as plain `claude`. To manage it as an account too, see [Migrating `~/.claude`](#migrating-claude) (optional; run it from a plain terminal with all Claude sessions closed).

## Everyday tasks

Each section below stands on its own; read the ones you need. The [command reference](#commands) lists every option.

### Proxy values

| Value | Effect in the launcher |
| ---- | ---- |
| `inherit` (default) | Leaves proxy variables exactly as they are in your shell. |
| `off` | Unsets `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`, `NO_PROXY` and their lowercase forms. |
| `7901` | Same as `http://127.0.0.1:7901`. |
| `http://host:port`, `https://host:port` | Sets `HTTPS_PROXY`/`HTTP_PROXY` (both cases), removes an inherited `ALL_PROXY` (Claude Code falls back to it for its own git calls), and appends `localhost,127.0.0.1,::1` to your existing `NO_PROXY`. |

SOCKS URLs are rejected: [Claude Code does not support SOCKS proxies](https://code.claude.com/docs/en/network-config). Proxy URLs must not contain a user name or password, because launchers are plain, world-readable files.

**Settings files win.** If an account's `settings.json`, its `.claude.json`, or a file passed with `--settings` sets a proxy variable in its `env` block, Claude Code uses that value instead of the launcher's. multi-claude warns about this (it reads only the variable names, never the values). Managed settings deployed by an administrator are not checked.

**Background sessions.** The background supervisor of an account inherits the environment of the shell that first starts it. If you need background sessions to use a proxy reliably, put the proxy in that account's own `settings.json` `env` block and set the account's proxy in multi-claude to `inherit`, so the two settings do not fight each other.

`CLAUDE_CODE_HTTP_PROXY` and `CLAUDE_CODE_HTTPS_PROXY` are not changed by launchers, not even with `off`; `list` mentions them when they are set.

### Environment variables and arguments

```sh
multi-claude args --defaults -- --ide --permission-mode acceptEdits \
  --allowedTools "Bash(git status),Read" \
  --settings ~/.claude-shared/settings.json
multi-claude env --defaults CLAUDE_CODE_PLUGIN_CACHE_DIR=~/.claude-shared/plugins
multi-claude env work MAX_THINKING_TOKENS=8000 --unset OLD_VAR
```

- Account values override defaults with the same name; arguments are the defaults followed by the account's own, and whatever you type after `claude-<name>` always comes last.
- A value that is exactly `~` or starts with `~/` is expanded to an absolute path. Other values are written literally (no `$` expansion). `--settings=~/x` is **not** expanded; write `--settings ~/x` instead.
- Variables that decide which login is used (`CLAUDE_CONFIG_DIR`, `CLAUDE_SECURESTORAGE_CONFIG_DIR`, `CLAUDE_CODE_CUSTOM_OAUTH_URL`, `HOME`, `USER`), the proxy variables (use `proxy`), and credentials (API keys, tokens, secrets, passwords) are rejected. Put credentials in the account's `settings.json` `env` block or use `apiKeyHelper`.
- `args` needs an explicit `--`. Options that take several values, such as `--allowedTools` or `--add-dir`, swallow every following argument that does not start with `-`. If one of them is the last option in the fixed arguments, it would eat the prompt you pass to the launcher; multi-claude warns about this. Put another option after it (as `--settings` above).
- Launchers always unset `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_CODE_OAUTH_REFRESH_TOKEN`: exported in a shell, any of them makes every account use that one credential instead of its own login. Running `claude` directly is not affected.

### Shared resources

```sh
multi-claude add work --shared            # share from ~/.claude-shared (or the shared directory already set)
multi-claude set work --shared ~/claude-common  # make ~/claude-common the shared directory for every account
```

Put the `agents`, `commands`, `skills` or `CLAUDE.md` you want every account to see into the shared directory; multi-claude does not create or fill it, and tells you when it has none of them yet. Changing the directory with `--shared DIR` (or `init --shared-dir DIR`) moves the links of every shared account.

The default items are `agents`, `commands`, `skills` and `CLAUDE.md`; only the items listed in `shared.items` are linked. multi-claude creates the missing links and remembers which links it created. Turning sharing off removes only those links; links you made yourself are left alone. A real file or directory at a link location is a conflict and is never overwritten.

If you already linked an account to the shared directory by hand, `multi-claude add NAME --shared --adopt` takes those links over without recreating them.

To keep one item out of a single account, for example so that it has its own `skills`:

```sh
multi-claude set work --shared-exclude skills    # removes the skills link multi-claude created
multi-claude set work --shared-include skills    # links it again
```

The other items stay shared, and the shared directory is not touched. A directory or link you put there yourself is left alone. `list` shows `yes (not: skills)`. With `apply -f`, an account without `shared_exclude` in the file shares every item again.

Items that hold one account's own state cannot be shared: `.credentials.json`, `.claude.json`, `settings.local.json`, `projects`, `history.jsonl`, `file-history`, `sessions`, `session-env`, `shell-snapshots` and `todos` (in any letter case). A configuration that lists one of them in `shared.items` is rejected.

**`skills/synced/`.** Claude Code stores skills synced from claude.ai in `skills/synced/`, one bucket per organization and account. If an account's `skills` is a real directory, turning sharing on is a conflict and multi-claude does not move anything. You may move the account's `skills/synced/<bucket>` into the shared directory's `skills/synced/` yourself: buckets of different accounts have different names and do not collide, but from then on that account's synced skills are written to the shared directory.

### Choosing an account by directory

```sh
multi-claude route ~/work work           # anything under ~/work uses claude-work
multi-claude route ~/work/client-a ca    # the longest matching directory wins
multi-claude route --default main        # optional: used when no route matches
claude-auto                              # start Claude Code with the account for this directory
multi-claude which                       # show the choice without starting anything
```

Routes live in `config.json` and generate `~/.local/bin/claude-auto`. It compares physical paths (symbolic links are resolved), so `~/work` does not match `~/workshop`, and a route whose directory does not exist is ignored. When no route matches and no default is set, `claude-auto` runs plain `claude`. Arguments are passed through: `claude-auto -p "hello"`.

Because `claude-auto` is a launcher name, an account cannot be called `auto` while routes exist, and an account cannot be removed while a route still uses it (both are conflicts, exit code 3). `claude-auto` does not change your shell; add `alias claude=claude-auto` yourself if you want plain `claude` to follow the routes.

Downgrading to a version without routes keeps working with the configuration, but its next write drops the `routes` section and leaves `claude-auto` behind; remove the routes (or delete `claude-auto`) before downgrading.

### Usage

```sh
multi-claude usage
```

```text
NAME   5H         7D       UPDATED
work   23% 14:00  41% Fri  12m ago
home   -          -        no data (start claude-home once)
```

The numbers are the last values Claude Code itself cached in the account's `.claude.json` (for the default identity, `~/.claude.json`). multi-claude never reads credentials and never calls the network, so the data can be old: `UPDATED` shows its age, and values older than an hour are marked `stale`. Claude Code writes this cache itself; when it refreshes it is not documented. A window whose reset time has passed shows `reset`.

For fresher numbers, let your status line record them. Claude Code passes the 5-hour and 7-day usage to the `statusLine` command each time it redraws the status line (Pro and Max plans, after the first response of a session). Wrap the `statusLine` command of the settings file that is actually in effect:

```sh
multi-claude statusline install ~/.claude/settings.json
```

The command becomes `<path of multi-claude> statusline-hook '<original command>'`. The hook saves the usage of the account it runs under (recognised by `CLAUDE_CONFIG_DIR`) in `~/.config/multi-claude/usage/` and then runs the original command with `/bin/sh -c`, so the status line looks and behaves the same. Only the percentages and reset times are saved, never the rest of the status-line data. `usage` then shows whichever is newer, the cache or the status line, and marks the latter `(statusline)`; `--json` has a `source` field.

- Only a file with an existing `"type": "command"` status line can be wrapped; the file is backed up next to itself (`*.multi-claude-bak.*`) before every change, and only `statusLine.command` changes. A symlinked file is changed at its target.
- Claude sessions that are already running keep the old status line. Usage is recorded from sessions started after `install`, once they have had their first reply.
- If several settings files define `statusLine`, wrap the one that wins (for example a file passed with `--settings` in the launcher arguments).
- Run `multi-claude statusline uninstall FILE` before uninstalling multi-claude; otherwise the status line stays empty because the wrapped command cannot be found. Running `install` again after multi-claude moves updates the path.

### Handing a session over to another account

Each account keeps its own sessions, so `claude --resume` in one account cannot see the sessions of another. `handoff` copies one session (its `.jsonl` file and the folder next to it with subagent transcripts and saved tool output) into the same project folder of another account:

```sh
# inside a Claude session of account work:
! multi-claude handoff home
# [multi-claude] continue with: cd '/path/to/project' && claude-home --resume 0f3c…
```

- Inside a Claude session (run with `!`), it copies the current session of the current account. In an ordinary terminal, name the account with `--from`; it then copies the latest session of the current directory. `--session ID` picks another session.
- Nothing is moved or deleted, and the source account is only read. A line that is still being written is left out. Later messages in the source account are not synchronised.
- If the target already has the same session with different content, `handoff` stops with exit code 3. `--force` renames the existing copy to `*.multi-claude-bak.*` and copies again; close that session in the target account first.
- `/rewind` snapshots (`file-history`) and project memory are not copied.

### Renaming accounts and managing MCP servers

```sh
multi-claude rename work client-a          # claude-work becomes claude-client-a; ~/.cc/work stays
multi-claude mcp client-a add --scope user github -- npx -y @modelcontextprotocol/server-github
multi-claude mcp client-a list
```

`rename` keeps the account's directory, so the login is not affected; routes follow the new name. Update your own aliases and scripts that call the old launcher name. Downgrading to a version without `rename` while a renamed account exists is unsafe to write with: the older version reports a conflict instead of changing the launcher.

`mcp` runs `claude mcp ...` with the account's configuration directory, proxy and extra environment variables, but without its fixed arguments (options such as `--allowedTools` would otherwise swallow the `mcp` subcommand). Claude Code itself writes the server configuration. Use `--scope user` for a server that should be available in every project of that account.

### Running other commands as an account

```sh
multi-claude run work -- claude --version    # any command, with exactly the environment of claude-work
multi-claude run work                        # same as claude-work
multi-claude run -- env                      # without NAME: the account claude-auto would use here
cd "$(multi-claude path work)"               # the account's directory
multi-claude add client-b --config-from work # start client-b with a copy of work's settings.json
```

`run` sets the same `CLAUDE_CONFIG_DIR`, proxy and extra variables as the launcher; everything after the first `--` is run unchanged and its exit code is returned. Without a command it runs `claude` with the account's fixed arguments. Without NAME it follows the routes of `claude-auto` and prints the chosen account on stderr; with no route and no default it asks for NAME. `path` exits with 1 if the directory is missing.

`--config-from` copies `settings.json` once; afterwards the two files are independent. A different existing `settings.json` in the new account, or a `settings.json` that the account shares, is a conflict and nothing is changed. Use [shared resources](#shared-resources) for settings that should stay the same in every account.

### Opening VS Code for an account (experimental)

```sh
multi-claude code work ~/projects/app             # a separate VS Code window for work
multi-claude code personal . -- --disable-gpu     # arguments after -- go to VS Code
```

The Claude Code extension uses the `CLAUDE_CONFIG_DIR` of the environment VS Code was started with, so a VS Code opened from the Dock always uses `~/.claude`. `code` starts VS Code with the account's environment (the same as `run`) and a separate user data directory, `<root>/.apps/<account directory>/vscode`. A separate user data directory is required: otherwise VS Code hands the request to the instance that is already running, with its own environment. Each account's instance starts with default VS Code settings; extensions are shared because they live in `~/.vscode/extensions`.

Notes:

- If an instance for the account is already running, the request goes to it; changes to the proxy or environment variables apply after that instance quits.
- Setting `CLAUDE_CONFIG_DIR` in the extension's `claudeCode.environmentVariables` inside that instance overrides the account.
- On macOS the `code` command passes the whole environment to `open --env`, so the values are briefly visible in the process list.
- Verified with VS Code 1.139.1 and Claude Code extension 2.1.286 on macOS; `code` prints a warning because it relies on undocumented behaviour. It needs the `code` command on `PATH` (in VS Code: "Shell Command: Install 'code' command in PATH").

### Diagnostics and completion

`multi-claude doctor` checks, without changing anything: the configuration and any unfinished migration, whether `claude` and the launcher directory are on `PATH`, whether each launcher is up to date and not hidden by another file of the same name, whether each account directory exists and is private (`0700`), the default account's link, broken shared links, whether a login exists, and variables such as `ANTHROPIC_API_KEY` that override the accounts' logins. Every problem comes with the command that fixes it. It exits with 1 when there is an error; `--json` prints the results for scripts.

```sh
multi-claude completion bash > ~/.local/share/bash-completion/completions/multi-claude
multi-claude completion zsh > "${fpath[1]}/_multi-claude"   # or any directory on your fpath
multi-claude completion fish > ~/.config/fish/completions/multi-claude.fish
```

## Migrating `~/.claude`

The default account is made of three parts that Claude Code finds only when `CLAUDE_CONFIG_DIR` is **not** set: the directory `~/.claude`, the file `~/.claude.json`, and (on macOS) the keychain item `Claude Code-credentials`. On macOS, the keychain item of every other account is tied to the exact path string in `CLAUDE_CONFIG_DIR`, so the default account cannot simply be pointed at a new path.

`multi-claude migrate-default main` therefore (without a name it uses the email address of the login in `~/.claude.json`):

1. moves `~/.claude` to `~/.cc/main` (rename on the same file system; otherwise copy, verify every file by SHA-256, and park the original as `~/.claude.multi-claude-bak.<timestamp>`);
2. leaves the link `~/.claude -> ~/.cc/main`;
3. registers `main` with the *default identity*: its launcher does **not** set `CLAUDE_CONFIG_DIR`, so `claude-main` and plain `claude` use the same directory, the same `~/.claude.json` and the same login as before.

`~/.claude.json` stays where it is, so the default account's data lives in two places; `list` shows where. Use only `claude` or `claude-main` for this account: `CLAUDE_CONFIG_DIR=~/.cc/main claude` would look for a different login and a different `.claude.json`, and behave like a new account.

**Before you migrate, close every Claude Code session of every account.** All accounts write to `~/.claude` (IDE locks, bridge and state files), whatever their `CLAUDE_CONFIG_DIR`. Run the command from a plain terminal, not from a Claude Code session. multi-claude refuses to start (exit code 4) when it finds:

- a process with files, its working directory or its executable inside `~/.claude`;
- a Claude Code process running under your home directory, including background sessions (the error lists a `claude daemon stop --any` command for each configuration directory);
- a process whose `CLAUDE_CONFIG_DIR` points to `~/.claude`;
- a live IDE extension lock in `~/.claude/ide/`;
- the installed background service: on macOS `~/Library/LaunchAgents/com.anthropic.claude-daemon.plist`, on Linux `${XDG_CONFIG_HOME:-~/.config}/systemd/user/com.anthropic.claude-daemon.service` (run `claude daemon uninstall` first and install it again afterwards);
- `CLAUDE_CODE_CHILD_SESSION` in its own environment, which means it runs inside a Claude Code session. If you are sure it does not (the variable can leak through screen, tmux or programs started by Claude Code), use `env -u CLAUDE_CODE_CHILD_SESSION multi-claude ...`.

Quit the Claude desktop app as well. On macOS, Apple's own programs (for example the system shells) hide their environment from other processes, so a shell that exports `CLAUDE_CONFIG_DIR` cannot be detected; the migration prints how many processes could not be checked. `--skip-process-check` skips all these checks at your own risk. It does not skip one precheck: if `CLAUDE_CONFIG_DIR` in the shell running multi-claude points to `~/.claude`, run `unset CLAUDE_CONFIG_DIR` first.

Progress is recorded in `~/.config/multi-claude/migrate-journal.json`. If the migration is interrupted, run the same command again and it continues from the actual state on disk. While a migration is unfinished, other write commands refuse to run.

Sockets and FIFOs are not copied in copy mode. On macOS, copy mode does not preserve extended attributes.

### Undoing a migration by hand

`multi-claude restore main` undoes the migration: it removes the link, moves `~/.cc/main` back to `~/.claude` and unregisters the account (its launcher is deleted; shared links inside the directory stay). It checks for running Claude Code sessions like `migrate-default` (`--skip-process-check` skips that) and can be run again if it is interrupted. It refuses, without changing anything, when a route still uses the account, when `~/.claude` points elsewhere, or when the two directories are on different file systems. By hand, the same steps are:

```sh
rm ~/.claude                     # remove the link (only the link)
mv ~/.cc/main ~/.claude          # move the data back
multi-claude remove main         # unregister the account
```

Nothing else needs to change: the login and `~/.claude.json` were never touched. With `--keep-backup` in copy mode, the original directory stays at `~/.claude.multi-claude-bak.<timestamp>`.

If a migration stops with an error and you want to abandon it: the error message says where the complete data is; move it back to `~/.claude` and delete `~/.config/multi-claude/migrate-journal.json`.

## FAQ

### How do I upgrade multi-claude?

With pipx, run `pipx upgrade multi-claude-cli`; with Homebrew, `brew upgrade multi-claude`. Otherwise run the installer again: `curl -fsSL https://raw.githubusercontent.com/jakoes-wu/multi-claude/main/install.sh | sh`, or `./install.sh` from an updated clone. It replaces only the tool; your configuration, accounts and launchers stay. When a release changes what launchers contain, `multi-claude doctor` reports them as stale and `multi-claude apply` rewrites them.

### Which account does plain `claude` use?

The one in `~/.claude`, as before: multi-claude does not change it. After [migrating `~/.claude`](#migrating-claude) it is the migrated account. `claude-auto` picks an account by directory instead; see [Choosing an account by directory](#choosing-an-account-by-directory).

### How do I continue a conversation with another account?

Inside the Claude session, run `! multi-claude handoff OTHER`, then run the command it prints in a new terminal. See [Handing a session over to another account](#handing-a-session-over-to-another-account).

### Why does `usage` show old numbers?

By default the numbers come from the cache that Claude Code writes in `.claude.json`, which can be hours old. `multi-claude statusline install FILE` records fresh numbers from your status line in every new session; see [Usage](#usage).

### How do I share skills and `CLAUDE.md` between accounts?

`multi-claude set NAME --shared` links them from `~/.claude-shared`, or from the shared directory you have set. See [Shared resources](#shared-resources).

### Does multi-claude read or copy my login?

No. It never reads, copies or deletes credentials; its only keychain call checks whether an item exists. On macOS a login is tied to the account directory's path, which is why `rename` keeps the directory.

## Reference

### Commands

| Command | What it does |
| ---- | ---- |
| `multi-claude init [--root DIR] [--bin-dir DIR] [--shared-dir DIR] [--shared-items A,B]` | Create or change global settings. |
| `multi-claude migrate-default [NAME] [--copy] [--keep-backup] [--proxy P] [--skip-process-check]` | Turn `~/.claude` into an account. Without NAME, the email address of its login is used. |
| `multi-claude restore NAME [--skip-process-check] [--dry-run]` | Undo `migrate-default`: move the account back to `~/.claude` and unregister it. The login is kept. |
| `multi-claude add NAME [--proxy P] [--shared [DIR] \| --no-shared] [--adopt] [--shared-exclude ITEM] [--shared-include ITEM] [--config-from OTHER]` | Add an account, adopt an existing directory, or change its options. `--config-from` copies `settings.json` from OTHER once. `--shared DIR` also makes DIR the shared directory for every account (default `~/.claude-shared`). `--shared-exclude` keeps one shared item out of this account; `--shared-include` undoes it. |
| `multi-claude set NAME [--proxy P] [--shared [DIR] \| --no-shared] [--shared-exclude ITEM] [--shared-include ITEM]` | Change an existing account; same options as `add` without `--adopt`. Returns 1 for an account that is not registered. |
| `multi-claude proxy NAME PORT\|URL\|off\|inherit` | Set an account's proxy. |
| `multi-claude env (NAME \| --defaults) [K=V ...] [--unset K ...]` | Set or remove extra environment variables. |
| `multi-claude args (NAME \| --defaults) -- [ARG ...]` | Replace the fixed arguments; `--` with nothing after it clears them. |
| `multi-claude remove NAME` | Unregister an account and delete its launcher. **The directory and the login are kept.** |
| `multi-claude apply [-f FILE]` | Converge everything to the configuration (or to `FILE`). |
| `multi-claude list [--verbose \| --json \| --names]` | Show accounts, launchers and login state. `--json` adds usage and is meant for scripts; `--names` prints only the names. |
| `multi-claude usage [NAME] [--json]` | Show the last known 5-hour and 7-day usage of each account. |
| `multi-claude doctor [--json] [--verbose]` | Check the configuration, launchers, account directories, shared links, logins and environment. |
| `multi-claude route DIR NAME`, `route DIR --remove`, `route --default NAME`, `route --no-default` | Choose an account by directory for `claude-auto`. |
| `multi-claude which [DIR]` | Show which account `claude-auto` would use in `DIR` (default: the current directory). |
| `multi-claude rename OLD NEW` | Rename an account and its launcher. The directory and the login stay. |
| `multi-claude login NAME [ARG ...]` | Sign in to that account (runs `claude auth login ARG ...` as it; an e-mail-like name is pre-filled with `--email`). |
| `multi-claude mcp NAME [ARG ...]` | Run `claude mcp ARG ...` as that account. |
| `multi-claude run [NAME] [-- COMMAND ...]` | Run a command (default: `claude` with the fixed arguments) with the environment of the account's launcher. Without NAME, the account `claude-auto` would use. |
| `multi-claude path NAME` | Print the account's directory. |
| `multi-claude code NAME [PATH] [-- ARGS ...]` | Open a separate VS Code instance with the account's environment (experimental). |
| `multi-claude handoff TARGET [--from NAME] [--session ID] [--force]` | Copy a session to another account and print the command that resumes it there. |
| `multi-claude statusline install\|uninstall FILE` | Wrap the statusLine command in a settings file so that `usage` gets fresh numbers, or restore it. |
| `multi-claude completion bash\|zsh\|fish` | Print a shell completion script. |

Every write command accepts `--dry-run`, and prints only what changes; add `--verbose` to also list items that are already up to date. Run `multi-claude` without arguments for a short guide or your account list. `env`, `args`, `proxy` and `usage NAME` return 1 for an account that is not registered.

### Default locations

| Item | Default |
| ---- | ---- |
| Account directories | `~/.cc/<name>` |
| Launchers | `~/.local/bin/claude-<name>` |
| Configuration | `~/.config/multi-claude/config.json` (honours `XDG_CONFIG_HOME`) |

Account names may contain letters, digits and `._@+-`, must start with a letter or digit, and are case-insensitive (`Work` and `work` are the same account). An email address works as a name. Entries starting with `.` in the root directory are never treated as accounts.

### Declarative setup with `apply`

See [`examples/config.example.json`](examples/config.example.json):

```json
{
  "version": 1,
  "root": "~/.cc",
  "bin_dir": "~/.local/bin",
  "shared": {"dir": "~/.claude-shared", "items": ["agents", "commands", "skills", "CLAUDE.md"]},
  "defaults": {"env": {}, "args": []},
  "accounts": {
    "main": {"identity": "default", "proxy": "inherit"},
    "work": {"proxy": "http://127.0.0.1:7901", "shared": true}
  }
}
```

```sh
multi-claude apply -f accounts.json
# or, on a new machine, in one step:
./install.sh --config accounts.json
```

`apply -f` replaces the configuration with the file. Accounts missing from the file are unregistered (their directories are kept). If anything conflicts, nothing is written at all.

`identity` is internal state and only `migrate-default` changes it. For a registered account, `identity` in the file is ignored. An account that is not registered yet but has `"identity": "default"` is skipped with a message: run `multi-claude migrate-default <name>` on that machine, then set its proxy, environment variables, arguments and sharing again. Routes that use the skipped account (including the default route) are skipped as well, each with a message; set them again with `multi-claude route`.

### Logins and account paths

On macOS, the login of an account created with `add` is stored in the keychain under a name derived from the account path (`list --verbose` shows it). Therefore multi-claude never changes the path of a registered account:

- changing the root directory while accounts exist is a conflict;
- `rename OLD NEW` changes only the account name and the launcher name; the directory stays, so the login stays (`list --verbose` shows the directory). Renaming that only changes the letter case is not supported, and an account name cannot be the directory of another account;
- if an existing launcher would get a different account path (for example because `HOME` changed while the configuration and launcher directories are absolute paths), that is a conflict;
- on a case-insensitive file system, adding `work` when the directory on disk is `Work` is a conflict; register it as `Work`.

When you register an existing directory with `add`, multi-claude checks (read-only) whether a login exists for exactly that path and warns if not. If you logged in earlier with a different spelling of the path (for example through a link), keep using that spelling.

#### Moving an account directory by hand

If you move an account directory yourself, its launcher reports that the directory does not exist. On macOS you then need to log in again with `/login` at the new path. The old keychain item can be removed with `security delete-generic-password -s 'Claude Code-credentials-<old suffix>'` (take the name from `list --verbose` before moving). On Linux the login is the `.credentials.json` file inside the directory and moves with it.

### Exit codes

| Code | Meaning |
| ---- | ---- |
| 0 | Success, or already in the desired state |
| 1 | Runtime error (I/O, invalid configuration file, failed verification, lock held by another command, account not registered) |
| 2 | Invalid command-line arguments |
| 3 | Conflict with files multi-claude does not own, or a change that would break a login; nothing was changed |
| 4 | The directory to migrate or restore is in use |

## Uninstalling

```sh
./install.sh --uninstall
```

This removes the tool only. Your configuration, account directories (including the VS Code data in `<root>/.apps` created by `code`), logins and `claude-<name>` launchers stay; the launchers keep working because they do not depend on multi-claude. Run `multi-claude statusline uninstall FILE` first if you wrapped a status line.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Run the tests with:

```sh
python3 -m unittest discover -s tests -t tests
```

## License

[MIT](LICENSE)
