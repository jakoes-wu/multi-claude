#!/bin/sh
# multi-claude installer (macOS / Linux).
#
# Prerequisites:
#   - python3 >= 3.8 and tar on PATH
#   - for remote installs: curl or wget, and network access to GitHub
#   - no root privileges needed; everything goes under --prefix (default ~/.local)
#
# Run it either from a cloned repository (./install.sh) or remotely:
#   curl -fsSL https://raw.githubusercontent.com/jakoes-wu/multi-claude/main/install.sh | sh -s -- [options]
#
# Re-running is safe: an identical installation is reported as unchanged.
set -eu

REPO="${MULTI_CLAUDE_REPO:-jakoes-wu/multi-claude}"
PREFIX="${HOME}/.local"
CONFIG_FILE=""
UNINSTALL=0
MARKER="# managed-by: multi-claude-installer"

usage() {
  cat <<EOF
Usage: install.sh [--prefix DIR] [--config FILE] [--uninstall] [-h|--help]

Install multi-claude, a manager for multiple Claude Code accounts.

Prerequisites:
  python3 >= 3.8 and tar; curl or wget for remote installs.

Options:
  --prefix DIR     install under DIR (default: ~/.local)
                   files: DIR/share/multi-claude and DIR/bin/multi-claude
  --config FILE    after installing, run 'multi-claude apply -f FILE'
                   to create every account described in FILE
  --uninstall      remove multi-claude itself; configuration, account
                   directories and generated claude-<name> launchers are kept
  -h, --help       show this help

Environment:
  MULTI_CLAUDE_REF      tag or branch to download (default: latest release)
  MULTI_CLAUDE_TARBALL  full URL of the source tarball (overrides the above;
                       file:// URLs work, which is how the tests use it)
  MULTI_CLAUDE_REPO     GitHub repository (default: ${REPO})
  MULTI_CLAUDE_API      GitHub API base URL (default: https://api.github.com)
  MULTI_CLAUDE_CODELOAD source archive base URL for branches and old releases
                       (default: https://codeload.github.com)
  MULTI_CLAUDE_SHA256   expected SHA-256 of MULTI_CLAUDE_TARBALL
  MULTI_CLAUDE_REQUIRE_CHECKSUM=1
                       refuse to install anything whose checksum cannot be verified

Releases from v0.4.0 on publish multi-claude-<tag>.tar.gz and SHA256SUMS; the
installer downloads that archive and checks it before installing. Branches and
older releases are installed unverified (a note says so).

Examples:
  ./install.sh                                  # from a cloned repository
  ./install.sh --prefix /opt/tools              # custom location
  ./install.sh --config ~/my-accounts.json      # install, then create accounts
  ./install.sh --uninstall                      # remove the tool only
  curl -fsSL https://raw.githubusercontent.com/${REPO}/main/install.sh | sh
  curl -fsSL https://raw.githubusercontent.com/${REPO}/main/install.sh | sh -s -- --config accounts.json
  MULTI_CLAUDE_REF=main sh install.sh            # install the main branch
EOF
}

log() { printf '[multi-claude-install] %s\n' "$*"; }
die() { printf '[multi-claude-install] error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) [ $# -ge 2 ] || die "--prefix needs a value (see -h)"; PREFIX="$2"; shift 2 ;;
    --config) [ $# -ge 2 ] || die "--config needs a value (see -h)"; CONFIG_FILE="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf '[multi-claude-install] error: unknown option: %s (see -h)\n' "$1" >&2; exit 2 ;;
  esac
done

case "$PREFIX" in
  *"'"*) die "--prefix must not contain a single quote" ;;
esac
SHARE_DIR="${PREFIX}/share/multi-claude"
BIN_DIR="${PREFIX}/bin"
WRAPPER="${BIN_DIR}/multi-claude"

if [ "$UNINSTALL" -eq 1 ]; then
  removed=0
  if [ -d "$SHARE_DIR" ]; then rm -rf "$SHARE_DIR"; removed=1; fi
  if [ -d "${SHARE_DIR}.old" ]; then rm -rf "${SHARE_DIR}.old"; removed=1; fi
  for leftover in "${PREFIX}/share"/.multi-claude-stage.*; do
    if [ -d "$leftover" ]; then rm -rf "$leftover"; removed=1; fi
  done
  if [ -f "$WRAPPER" ] && sed -n 2p "$WRAPPER" | grep -qF "$MARKER"; then rm -f "$WRAPPER"; removed=1; fi
  if [ "$removed" -eq 1 ]; then
    log "removed multi-claude from ${PREFIX}; configuration, accounts and launchers are kept"
  else
    log "unchanged: multi-claude is not installed under ${PREFIX}"
  fi
  exit 0
fi

PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die "python3 not found; install Python 3.8 or newer"
case "$PYTHON" in
  *"'"*) die "the path of python3 (${PYTHON}) contains a single quote, which the launcher cannot quote" ;;
esac
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || die "python3 at ${PYTHON} is older than 3.8"
case "$PYTHON" in
  */shims/*)
    # pyenv / asdf / mise 的 shim 是一段脚本，每次启动多花几十毫秒；状态栏钩子每次刷新都要启动一次，
    # 所以写入它背后的真实解释器。代价：以后卸载了这个 Python 版本，需要重新运行本脚本。
    REAL_PYTHON="$("$PYTHON" -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
    case "$REAL_PYTHON" in
      *"'"*) REAL_PYTHON="" ;;
    esac
    if [ -n "$REAL_PYTHON" ] && [ -x "$REAL_PYTHON" ]; then
      log "using ${REAL_PYTHON} instead of the shim ${PYTHON}"
      PYTHON="$REAL_PYTHON"
    fi
    ;;
esac

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/multi-claude-install.XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Source: the directory next to this script when run from a clone, otherwise a download.
# With `curl ... | sh`, $0 is the shell itself, so only trust $0 when it names this script;
# otherwise a clone in the current directory would be installed by mistake.
SCRIPT_DIR=""
case "$0" in
  *install.sh) SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)" || SCRIPT_DIR="" ;;
esac
if [ -n "$SCRIPT_DIR" ] && [ -f "${SCRIPT_DIR}/src/multi_claude/__init__.py" ] && [ -z "${MULTI_CLAUDE_TARBALL:-}" ]; then
  SRC_PKG="${SCRIPT_DIR}/src/multi_claude"
else
  if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
    die "curl or wget is required to download multi-claude"
  fi
  fetch() {
    if command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o "$2"
    else wget -q "$1" -O "$2"; fi
  }
  TARBALL="${MULTI_CLAUDE_TARBALL:-}"
  EXPECTED_SHA="${MULTI_CLAUDE_SHA256:-}"
  API="${MULTI_CLAUDE_API:-https://api.github.com}"
  CODELOAD="${MULTI_CLAUDE_CODELOAD:-https://codeload.github.com}"
  if [ -z "$TARBALL" ]; then
    REF="${MULTI_CLAUDE_REF:-}"
    RELEASE_JSON=""
    if [ -z "$REF" ]; then
      fetch "${API}/repos/${REPO}/releases/latest" "${WORK_DIR}/release.json" 2>/dev/null \
        || die "no release of ${REPO} found; set MULTI_CLAUDE_REF=main to install the main branch"
      RELEASE_JSON="${WORK_DIR}/release.json"
    elif "$PYTHON" -c 'import re, sys; sys.exit(0 if re.fullmatch(r"v[0-9]+[.][0-9]+[.][0-9]+([-+].*)?", sys.argv[1]) else 1)' "$REF"; then
      # A version tag: its checksum must be checked, so failing to read the release is an error,
      # not a reason to fall back to an unverified download.
      fetch "${API}/repos/${REPO}/releases/tags/${REF}" "${WORK_DIR}/release.json" 2>/dev/null \
        || die "cannot read release ${REF} of ${REPO} (network error, API rate limit or no such tag); set MULTI_CLAUDE_TARBALL to install anyway"
      RELEASE_JSON="${WORK_DIR}/release.json"
    fi
    ASSET=""
    SUMS=""
    if [ -n "$RELEASE_JSON" ]; then
      # Three lines: the tag, then the URLs of multi-claude-<tag>.tar.gz and SHA256SUMS (empty when not published).
      RELEASE_INFO="$("$PYTHON" -c '
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
tag = data.get("tag_name") or ""
urls = {asset.get("name"): asset.get("browser_download_url") or "" for asset in data.get("assets") or []}
print(tag)
print(urls.get("multi-claude-" + tag + ".tar.gz", ""))
print(urls.get("SHA256SUMS", ""))
' "$RELEASE_JSON")" || die "could not read the release information of ${REPO}"
      REF="$(printf '%s\n' "$RELEASE_INFO" | sed -n 1p)"
      ASSET="$(printf '%s\n' "$RELEASE_INFO" | sed -n 2p)"
      SUMS="$(printf '%s\n' "$RELEASE_INFO" | sed -n 3p)"
      [ -n "$REF" ] || die "could not read the latest release tag; set MULTI_CLAUDE_REF to a tag or branch"
    fi
    if [ -n "$ASSET" ] && [ -n "$SUMS" ]; then
      TARBALL="$ASSET"
      fetch "$SUMS" "${WORK_DIR}/SHA256SUMS" || die "download failed: ${SUMS}"
      EXPECTED_SHA="$("$PYTHON" -c '
import sys
wanted = sys.argv[2]
for line in open(sys.argv[1], encoding="utf-8"):
    parts = line.split(None, 1)
    # sha256sum writes "<hash>  <name>", or "<hash> *<name>" in binary mode.
    if len(parts) == 2 and parts[1].strip().lstrip("*") == wanted:
        print(parts[0].lower())
        sys.exit(0)
sys.exit(1)
' "${WORK_DIR}/SHA256SUMS" "multi-claude-${REF}.tar.gz")" \
        || die "SHA256SUMS of ${REF} has no entry for multi-claude-${REF}.tar.gz"
    else
      TARBALL="${CODELOAD}/${REPO}/tar.gz/${REF}"
    fi
  fi
  log "downloading ${TARBALL}"
  fetch "$TARBALL" "${WORK_DIR}/src.tar.gz" || die "download failed: ${TARBALL}"
  # Verify before extracting: a mismatch stops here, before anything installed is touched.
  if [ -n "$EXPECTED_SHA" ]; then
    EXPECTED_SHA="$(printf '%s' "$EXPECTED_SHA" | tr 'ABCDEF' 'abcdef')"
    ACTUAL_SHA="$("$PYTHON" -c '
import hashlib, sys
digest = hashlib.sha256()
with open(sys.argv[1], "rb") as handle:
    for block in iter(lambda: handle.read(1 << 20), b""):
        digest.update(block)
print(digest.hexdigest())
' "${WORK_DIR}/src.tar.gz")" || die "could not compute the checksum of ${TARBALL}"
    [ "$ACTUAL_SHA" = "$EXPECTED_SHA" ] \
      || die "checksum mismatch for ${TARBALL}: expected ${EXPECTED_SHA}, got ${ACTUAL_SHA}"
    log "verified sha256 ${ACTUAL_SHA}"
  elif [ "${MULTI_CLAUDE_REQUIRE_CHECKSUM:-}" = "1" ]; then
    die "${TARBALL} has no published checksum and MULTI_CLAUDE_REQUIRE_CHECKSUM=1 is set"
  else
    log "note: ${TARBALL} is not verified (no checksum published)"
  fi
  mkdir "${WORK_DIR}/src"
  tar -xzf "${WORK_DIR}/src.tar.gz" -C "${WORK_DIR}/src" || die "cannot extract ${TARBALL}"
  SRC_PKG="$(find "${WORK_DIR}/src" -type d -path '*/src/multi_claude' | head -n 1)"
  [ -n "$SRC_PKG" ] || die "the downloaded archive does not contain src/multi_claude"
fi

# Stage the package, then compare by content hash with what is installed.
STAGE_PARENT="$(dirname "$SHARE_DIR")"
mkdir -p "$STAGE_PARENT" "$BIN_DIR"
tree_hash() {
  "$PYTHON" - "$1" <<'PY'
import hashlib, os, sys
root = sys.argv[1]
digest = hashlib.sha256()
if os.path.isdir(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if name.endswith(".pyc"):
                continue
            path = os.path.join(dirpath, name)
            digest.update(os.path.relpath(path, root).encode())
            with open(path, "rb") as handle:
                digest.update(hashlib.sha256(handle.read()).digest())
print(digest.hexdigest())
PY
}

# Recover from an interrupted previous install (step 2 done, step 3 not) and drop stale staging dirs.
for leftover in "$STAGE_PARENT"/.multi-claude-stage.*; do
  if [ -d "$leftover" ]; then rm -rf "$leftover"; fi
done
OLD_DIR="${SHARE_DIR}.old"
if [ ! -d "$SHARE_DIR" ] && [ -d "$OLD_DIR" ]; then
  mv "$OLD_DIR" "$SHARE_DIR"
elif [ -d "$OLD_DIR" ]; then
  rm -rf "$OLD_DIR"
fi

NEW_HASH="$(tree_hash "$SRC_PKG")"
OLD_HASH="$(tree_hash "${SHARE_DIR}/multi_claude")"
if [ "$NEW_HASH" = "$OLD_HASH" ]; then
  log "unchanged: ${SHARE_DIR}"
else
  STAGE="$(mktemp -d "${STAGE_PARENT}/.multi-claude-stage.XXXXXX")"
  cp -R "$SRC_PKG" "${STAGE}/multi_claude"
  find "${STAGE}" -name '__pycache__' -type d -prune -exec rm -rf {} +
  # Swap in three renames so an interruption leaves either the old or the new version usable.
  if [ -d "$SHARE_DIR" ]; then mv "$SHARE_DIR" "$OLD_DIR"; fi
  # Test hook: simulate being killed between the two renames; the next run recovers via step 0.
  # It only works together with MULTI_CLAUDE_TEST_MODE=1, so a stray variable cannot break a real install.
  if [ "${MULTI_CLAUDE_TEST_MODE:-}" = 1 ] && [ -n "${MULTI_CLAUDE_TEST_CRASH_SWAP:-}" ]; then rm -rf "$STAGE"; exit 137; fi
  mv "$STAGE" "$SHARE_DIR"
  rm -rf "$OLD_DIR"
  log "installed ${SHARE_DIR}"
fi

WRAPPER_CONTENT="#!/bin/sh
${MARKER}
PYTHONPATH='${SHARE_DIR}'\${PYTHONPATH:+:\$PYTHONPATH} exec '${PYTHON}' -m multi_claude \"\$@\""
if [ -f "$WRAPPER" ] && [ "$(cat "$WRAPPER")" = "$WRAPPER_CONTENT" ]; then
  log "unchanged: ${WRAPPER}"
else
  if [ -e "$WRAPPER" ] && ! sed -n 2p "$WRAPPER" | grep -qF "$MARKER"; then
    die "${WRAPPER} exists and was not created by this installer; remove it or use --prefix"
  fi
  printf '%s\n' "$WRAPPER_CONTENT" > "${WRAPPER}.tmp"
  chmod 755 "${WRAPPER}.tmp"
  mv "${WRAPPER}.tmp" "$WRAPPER"
  log "installed ${WRAPPER}"
fi

# 与 src/multi_claude/shellpath.py 同一张表：按当前 shell 给出可直接粘贴的一行命令，不替用户改配置文件。
path_command() {
  case "$BIN_DIR" in *"'"*) return 1 ;; esac
  case "$(basename "${SHELL:-}")" in
    zsh) printf "echo 'export PATH=\"%s:\$PATH\"' >> ~/.zshrc" "$BIN_DIR" ;;
    bash)
      if [ "$(uname -s)" = "Darwin" ]; then profile=".bash_profile"; else profile=".bashrc"; fi
      printf "echo 'export PATH=\"%s:\$PATH\"' >> ~/%s" "$BIN_DIR" "$profile" ;;
    fish) printf "fish_add_path '%s'" "$BIN_DIR" ;;
    *) return 1 ;;
  esac
}
case ":${PATH}:" in
  *":${BIN_DIR}:"*) ;;
  *)
    if command_text="$(path_command)"; then
      log "note: ${BIN_DIR} is not on PATH; run: ${command_text}, then open a new terminal"
    else
      log "note: ${BIN_DIR} is not on PATH; add it in your shell profile, e.g. export PATH=\"${BIN_DIR}:\$PATH\""
    fi
    ;;
esac

if [ -n "$CONFIG_FILE" ]; then
  log "applying ${CONFIG_FILE}"
  "$WRAPPER" apply -f "$CONFIG_FILE"
fi
