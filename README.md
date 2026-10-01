# multi-claude

**English** | [简体中文](README.zh-CN.md)

Run several [Claude Code](https://code.claude.com/docs) accounts side by side on one machine.

Claude Code keeps its settings, login and history in the directory named by `CLAUDE_CONFIG_DIR` (default `~/.claude`). The official documentation suggests one directory per account. multi-claude manages those directories for you and gives every account its own launcher command, optionally with its own proxy, environment variables and fixed arguments:

```text
claude-work       -> CLAUDE_CONFIG_DIR=~/.cc/work      HTTPS_PROXY=http://127.0.0.1:7901
claude-personal   -> CLAUDE_CONFIG_DIR=~/.cc/personal  (inherits your shell's proxy settings)
claude-main       -> your original ~/.claude account, still logged in
claude            -> unchanged
```

## Features

- **Migrate the default account** — move your existing `~/.claude` into the account root without logging out. `claude` keeps working exactly as before.
- **Add accounts** — create an account directory and a `claude-<name>` launcher, or adopt a directory you already have.
- **Per-account proxy** — a local port, an HTTP(S) proxy URL, `off`, or `inherit`.
- **Extra environment variables and fixed arguments** — global defaults plus per-account values, for example `--settings` or `--permission-mode`.
- **One-step deployment** — `install.sh --config accounts.json` installs the tool and creates every account in the file.
- **Idempotent** — every command can be re-run safely. Unchanged state is reported as `unchanged`; conflicts with files multi-claude does not own are reported without changing anything; an interrupted migration resumes where it stopped.
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

Alternatively: `pipx install git+https://github.com/jakoes-wu/multi-claude`.

Run `./install.sh --help` for all options.

## Quick start

```sh
# Turn the existing ~/.claude into an account named "main" (run from a plain terminal, see below)
multi-claude migrate-default main

# Add a second account that goes through a local proxy on port 7901
multi-claude add work --proxy 7901

# Start it and log in once with /login
claude-work

multi-claude list
```

## Commands

| Command | What it does |
| ---- | ---- |
| `multi-claude init [--root DIR] [--bin-dir DIR] [--shared-dir DIR] [--shared-items A,B]` | Create or change global settings. |
| `multi-claude migrate-default NAME [--copy] [--keep-backup] [--proxy P] [--skip-process-check]` | Turn `~/.claude` into an account. |
| `multi-claude add NAME [--proxy P] [--shared \| --no-shared] [--adopt]` | Add an account, adopt an existing directory, or change its options. |
| `multi-claude proxy NAME PORT\|URL\|off\|inherit` | Set an account's proxy. |
| `multi-claude env (NAME \| --defaults) [K=V ...] [--unset K ...]` | Set or remove extra environment variables. |
| `multi-claude args (NAME \| --defaults) -- [ARG ...]` | Replace the fixed arguments; `--` with nothing after it clears them. |
| `multi-claude remove NAME` | Unregister an account and delete its launcher. **The directory and the login are kept.** |
| `multi-claude apply [-f FILE]` | Converge everything to the configuration (or to `FILE`). |
| `multi-claude list [--verbose]` | Show accounts, launchers and login state. |

Every write command accepts `--dry-run`. `env`, `args` and `proxy` return 1 for an account that is not registered.

### Default locations

| Item | Default |
| ---- | ---- |
| Account directories | `~/.cc/<name>` |
| Launchers | `~/.local/bin/claude-<name>` |
| Configuration | `~/.config/multi-claude/config.json` (honours `XDG_CONFIG_HOME`) |

Account names may contain letters, digits and `._@+-`, must start with a letter or digit, and are case-insensitive (`Work` and `work` are the same account). An email address works as a name. Entries starting with `.` in the root directory are never treated as accounts.

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

`identity` is internal state and only `migrate-default` changes it. For a registered account, `identity` in the file is ignored. An account that is not registered yet but has `"identity": "default"` is skipped with a message: run `multi-claude migrate-default <name>` on that machine, then set its proxy, environment variables, arguments and sharing again.

## Migrating `~/.claude`

The default account is made of three parts that Claude Code finds only when `CLAUDE_CONFIG_DIR` is **not** set: the directory `~/.claude`, the file `~/.claude.json`, and (on macOS) the keychain item `Claude Code-credentials`. On macOS, the keychain item of every other account is tied to the exact path string in `CLAUDE_CONFIG_DIR`, so the default account cannot simply be pointed at a new path.

`multi-claude migrate-default main` therefore:

1. moves `~/.claude` to `~/.cc/main` (rename on the same file system; otherwise copy, verify every file by SHA-256, and park the original as `~/.claude.multi-claude-bak.<timestamp>`);
2. leaves the link `~/.claude -> ~/.cc/main`;
3. registers `main` with the *default identity*: its launcher does **not** set `CLAUDE_CONFIG_DIR`, so `claude-main` and plain `claude` use the same directory, the same `~/.claude.json` and the same login as before.

`~/.claude.json` stays where it is, so the default account's data lives in two places; `list` shows where. Use only `claude` or `claude-main` for this account: `CLAUDE_CONFIG_DIR=~/.cc/main claude` would look for a different login and a different `.claude.json`, and behave like a new account.

**Before you migrate, close every Claude Code session of every account.** All accounts write to `~/.claude` (IDE locks, bridge and state files), whatever their `CLAUDE_CONFIG_DIR`. Run the command from a plain terminal, not from a Claude Code session. multi-claude refuses to start (exit code 4) when it finds:

- a process with files, its working directory or its executable inside `~/.claude`;
- a Claude Code process running under your home directory, including background sessions (the error lists a `claude daemon stop --any` command for each configuration directory);
- a process whose `CLAUDE_CONFIG_DIR` points to `~/.claude`;
- a live IDE extension lock in `~/.claude/ide/`;
- on macOS, the background service `~/Library/LaunchAgents/com.anthropic.claude-daemon.plist` (run `claude daemon uninstall` first and install it again afterwards);
- `CLAUDE_CODE_CHILD_SESSION` in its own environment, which means it runs inside a Claude Code session. If you are sure it does not (the variable can leak through screen, tmux or programs started by Claude Code), use `env -u CLAUDE_CODE_CHILD_SESSION multi-claude ...`.

Quit the Claude desktop app as well. On macOS, Apple's own programs (for example the system shells) hide their environment from other processes, so a shell that exports `CLAUDE_CONFIG_DIR` cannot be detected; the migration prints how many processes could not be checked. `--skip-process-check` skips all these checks at your own risk. It does not skip one precheck: if `CLAUDE_CONFIG_DIR` in the shell running multi-claude points to `~/.claude`, run `unset CLAUDE_CONFIG_DIR` first.

Progress is recorded in `~/.config/multi-claude/migrate-journal.json`. If the migration is interrupted, run the same command again and it continues from the actual state on disk. While a migration is unfinished, other write commands refuse to run.

Sockets and FIFOs are not copied in copy mode. On macOS, copy mode does not preserve extended attributes.

### Undoing a migration by hand

```sh
rm ~/.claude                     # remove the link (only the link)
mv ~/.cc/main ~/.claude          # move the data back
multi-claude remove main         # unregister the account
```

Nothing else needs to change: the login and `~/.claude.json` were never touched. With `--keep-backup` in copy mode, the original directory stays at `~/.claude.multi-claude-bak.<timestamp>`.

If a migration stops with an error and you want to abandon it: the error message says where the complete data is; move it back to `~/.claude` and delete `~/.config/multi-claude/migrate-journal.json`.

## Logins and account paths

On macOS, the login of an account created with `add` is stored in the keychain under a name derived from the account path (`list --verbose` shows it). Therefore multi-claude never changes the path of a registered account:

- changing the root directory while accounts exist is a conflict;
- renaming an account, even only its case, is a conflict;
- if an existing launcher would get a different account path (for example because `HOME` changed while the configuration and launcher directories are absolute paths), that is a conflict;
- on a case-insensitive file system, adding `work` when the directory on disk is `Work` is a conflict; register it as `Work`.

When you register an existing directory with `add`, multi-claude checks (read-only) whether a login exists for exactly that path and warns if not. If you logged in earlier with a different spelling of the path (for example through a link), keep using that spelling.

### Moving an account directory by hand

If you move an account directory yourself, its launcher reports that the directory does not exist. On macOS you then need to log in again with `/login` at the new path. The old keychain item can be removed with `security delete-generic-password -s 'Claude Code-credentials-<old suffix>'` (take the name from `list --verbose` before moving). On Linux the login is the `.credentials.json` file inside the directory and moves with it.

## Shared resources

```sh
multi-claude init --shared-dir ~/.claude-shared
multi-claude add work --shared
```

The default items are `agents`, `commands`, `skills` and `CLAUDE.md`; only the items listed in `shared.items` are linked. multi-claude creates the missing links and remembers which links it created. Turning sharing off removes only those links; links you made yourself are left alone. A real file or directory at a link location is a conflict and is never overwritten.

If you already linked an account to the shared directory by hand, `multi-claude add NAME --shared --adopt` takes those links over without recreating them.

**`skills/synced/`.** Claude Code stores skills synced from claude.ai in `skills/synced/`, one bucket per organization and account. If an account's `skills` is a real directory, turning sharing on is a conflict and multi-claude does not move anything. You may move the account's `skills/synced/<bucket>` into the shared directory's `skills/synced/` yourself: buckets of different accounts have different names and do not collide, but from then on that account's synced skills are written to the shared directory.

## Exit codes

| Code | Meaning |
| ---- | ---- |
| 0 | Success, or already in the desired state |
| 1 | Runtime error (I/O, invalid configuration file, failed verification, lock held by another command, account not registered) |
| 2 | Invalid command-line arguments |
| 3 | Conflict with files multi-claude does not own, or a change that would break a login; nothing was changed |
| 4 | The migration source is in use |

## Uninstalling

```sh
./install.sh --uninstall
```

This removes the tool only. Your configuration, account directories, logins and `claude-<name>` launchers stay; the launchers keep working because they do not depend on multi-claude.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Run the tests with:

```sh
python3 -m unittest discover -s tests -t tests
```

## License

[MIT](LICENSE)
