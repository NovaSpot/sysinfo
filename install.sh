#!/usr/bin/env bash
# install.sh - installs sysinfo and its dependencies (rich, psutil).
#
#   ./install.sh              auto: use what's installed, else distro packages, else a venv
#   ./install.sh --venv       always use a private virtualenv (no root needed)
#   ./install.sh --native     only use distro packages (fail if that isn't possible)
#   ./install.sh -y           don't ask before running the package manager
#   ./install.sh --uninstall  remove what this script installed
#
# Installs to:  ~/.local/share/sysinfo/   (script, optional venv)
#               ~/.local/bin/sysinfo      (launcher)
# Package managers are only run after showing you the exact command.

set -eu

MIN_PY="3.7"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/sysinfo"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
LAUNCHER="$BIN_DIR/sysinfo"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/sysinfo.py"

MODE=auto
ASSUME_YES=0
UNINSTALL=0

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

for arg in "$@"; do
    case "$arg" in
        --venv) MODE=venv ;;
        --native) MODE=native ;;
        -y|--yes) ASSUME_YES=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage; die "unknown option: $arg" ;;
    esac
done

# --------------------------------------------------------------------------- #
# Uninstall
# --------------------------------------------------------------------------- #
if [ "$UNINSTALL" = 1 ]; then
    if [ -f "$LAUNCHER" ] && grep -q "$DATA_DIR/sysinfo.py" "$LAUNCHER" 2>/dev/null; then
        rm -f "$LAUNCHER"; say "removed $LAUNCHER"
    fi
    if [ -d "$DATA_DIR" ]; then
        rm -rf "$DATA_DIR"; say "removed $DATA_DIR"
    fi
    say "done (distro packages python3-rich / python3-psutil were left alone)"
    exit 0
fi

[ -f "$SRC" ] || die "sysinfo.py not found next to install.sh ($SRC)"
[ "$(uname -s)" = Linux ] || warn "sysinfo targets Linux; $(uname -s) is untested"

# --------------------------------------------------------------------------- #
# Python checks
# --------------------------------------------------------------------------- #
py_ok()    { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)' >/dev/null 2>&1; }
has_deps() { "$1" -c 'import rich, psutil' >/dev/null 2>&1; }

PATH_PY="$(command -v python3 || true)"
SYS_PY=""
[ -x /usr/bin/python3 ] && SYS_PY=/usr/bin/python3
[ -n "$PATH_PY" ] || [ -n "$SYS_PY" ] || \
    die "python3 not found. Install it first (e.g. 'sudo apt install python3')."

# Base interpreter for the venv: prefer whatever is on PATH.
BASE_PY="${PATH_PY:-$SYS_PY}"
py_ok "$BASE_PY" || die "Python $MIN_PY or newer is required ($("$BASE_PY" -V 2>&1) found)."

# --------------------------------------------------------------------------- #
# Distro detection
# --------------------------------------------------------------------------- #
detect_family() {
    local tokens=""
    if [ -r /etc/os-release ]; then
        tokens="$( . /etc/os-release; echo "${ID:-} ${ID_LIKE:-}" )"
    fi
    local t
    for t in $tokens; do
        case "$t" in
            debian|ubuntu|linuxmint|raspbian|pop|kali) echo debian; return ;;
            fedora)                                    echo fedora; return ;;
            rhel|centos|rocky|almalinux|ol)            echo rhel;   return ;;
            arch|manjaro|endeavouros)                  echo arch;   return ;;
            alpine)                                    echo alpine; return ;;
            opensuse*|suse|sles)                       echo suse;   return ;;
        esac
    done
    echo unknown
}

confirm() {
    [ "$ASSUME_YES" = 1 ] && return 0
    local ans=""
    if ! read -r -p "$1 [y/N] " ans </dev/tty 2>/dev/null; then
        warn "no terminal to ask on; re-run with -y to allow"
        return 1
    fi
    case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# --------------------------------------------------------------------------- #
# Strategy 1: distro packages
# --------------------------------------------------------------------------- #
try_native() {
    local fam="$1" cmd
    case "$fam" in
        debian) cmd=(apt-get install -y python3-rich python3-psutil) ;;
        fedora) cmd=(dnf install -y python3-rich python3-psutil) ;;
        arch)   cmd=(pacman -S --needed --noconfirm python-rich python-psutil) ;;
        alpine) cmd=(apk add py3-rich py3-psutil) ;;
        suse)   cmd=(zypper --non-interactive install python3-rich python3-psutil) ;;
        rhel)   warn "RHEL-family: python3-rich is usually not packaged; skipping distro packages"
                return 1 ;;
        *)      warn "unrecognised distro; skipping distro packages"; return 1 ;;
    esac
    if [ "$(id -u)" -ne 0 ]; then
        if command -v sudo >/dev/null 2>&1; then cmd=(sudo "${cmd[@]}")
        elif command -v doas >/dev/null 2>&1; then cmd=(doas "${cmd[@]}")
        else warn "not root and no sudo/doas available"; return 1; fi
    fi
    say "Will run: ${cmd[*]}"
    confirm "Proceed?" || return 1
    "${cmd[@]}" || { warn "package install failed"; return 1; }
}

# --------------------------------------------------------------------------- #
# Strategy 2: private virtualenv
# --------------------------------------------------------------------------- #
setup_venv() {
    local venv="$DATA_DIR/venv"
    mkdir -p "$DATA_DIR"
    rm -rf "$venv"
    say "Creating virtualenv in $venv"
    if ! "$BASE_PY" -m venv "$venv" >/dev/null 2>&1; then
        rm -rf "$venv"
        warn "could not create a venv. On Debian/Ubuntu: sudo apt install python3-venv"
        return 1
    fi
    say "Installing rich and psutil with pip"
    if ! "$venv/bin/python" -m pip install --quiet rich psutil; then
        rm -rf "$venv"
        warn "pip failed. Check your network; if psutil has to compile you need a C compiler"
        warn "and python headers (e.g. build-essential python3-dev / gcc python3-devel)."
        return 1
    fi
}

# --------------------------------------------------------------------------- #
# Decide how to get dependencies
# --------------------------------------------------------------------------- #
RUN_PY=""
FAMILY="$(detect_family)"
say "Detected distro family: $FAMILY"

pick_existing() {
    local p
    for p in "$PATH_PY" "$SYS_PY"; do
        if [ -n "$p" ] && py_ok "$p" && has_deps "$p"; then RUN_PY="$p"; return 0; fi
    done
    return 1
}

case "$MODE" in
    venv)
        setup_venv || die "venv setup failed"
        RUN_PY="$DATA_DIR/venv/bin/python"
        ;;
    native)
        pick_existing || {
            try_native "$FAMILY" || die "distro package install not possible here"
            pick_existing || die "packages installed but python3 still can't import rich/psutil"
        }
        ;;
    auto)
        if pick_existing; then
            say "rich and psutil already available for $RUN_PY"
        elif try_native "$FAMILY" && pick_existing; then
            say "Installed distro packages"
        else
            warn "falling back to a private virtualenv"
            setup_venv || die "could not install dependencies; see messages above"
            RUN_PY="$DATA_DIR/venv/bin/python"
        fi
        ;;
esac

# --------------------------------------------------------------------------- #
# Install script + launcher
# --------------------------------------------------------------------------- #
mkdir -p "$DATA_DIR" "$BIN_DIR"
install -m 0755 "$SRC" "$DATA_DIR/sysinfo.py"
cat > "$LAUNCHER" <<EOF
#!/bin/sh
exec "$RUN_PY" "$DATA_DIR/sysinfo.py" "\$@"
EOF
chmod 0755 "$LAUNCHER"
say "Installed $LAUNCHER (using $RUN_PY)"

# Smoke test
if "$LAUNCHER" --once >/dev/null 2>&1; then
    say "Smoke test passed"
else
    warn "smoke test failed; run '$LAUNCHER --once' to see the error"
fi

# --------------------------------------------------------------------------- #
# Make sure `sysinfo` is on PATH (asks first; never edits twice)
# --------------------------------------------------------------------------- #
add_to_path() {
    local rc line shell_name
    shell_name="$(basename "${SHELL:-sh}")"
    case "$shell_name" in
        bash) rc="$HOME/.bashrc";                 line="export PATH=\"$BIN_DIR:\$PATH\"" ;;
        zsh)  rc="${ZDOTDIR:-$HOME}/.zshrc";      line="export PATH=\"$BIN_DIR:\$PATH\"" ;;
        fish) rc="$HOME/.config/fish/config.fish"; line="fish_add_path \"$BIN_DIR\"" ;;
        *)    rc="$HOME/.profile";                line="export PATH=\"$BIN_DIR:\$PATH\"" ;;
    esac

    if [ -f "$rc" ] && grep -qF "$BIN_DIR" "$rc" 2>/dev/null; then
        warn "$BIN_DIR is already mentioned in $rc but not active in this shell."
        warn "Open a new terminal (or run: source $rc) and try 'sysinfo'."
        return 0
    fi

    warn "$BIN_DIR is not on your PATH, so 'sysinfo' won't be found yet."
    say "Will append to $rc:  $line"
    if confirm "Add it?"; then
        mkdir -p "$(dirname "$rc")"
        printf '\n# added by sysinfo install.sh\n%s\n' "$line" >> "$rc"
        say "Updated $rc. Open a new terminal (or run: source $rc) to use 'sysinfo'."
    else
        warn "Skipped. Run it as $LAUNCHER, or add this yourself:"
        echo "     $line"
    fi
}

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) add_to_path ;;
esac

say "Done. Run: sysinfo"
