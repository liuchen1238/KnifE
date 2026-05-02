#!/usr/bin/env bash
# knife — bootstrap script (macOS / Linux).
#
# Picks the best Python on the system, creates a venv, installs knife.
# Defends against three real-world macOS pitfalls we have hit:
#   1. Apple's /usr/bin/python3 links to the broken system Tk on macOS 26+
#      (Tcl_Panic in TkpInit -> SIGABRT).
#   2. Homebrew python@3.14 has a broken pyexpat bottle (missing
#      _XML_SetAllocTrackerActivationThreshold) that breaks pip itself.
#   3. Homebrew tcl-tk 9.0.3 hard-fails with "macOS 26 required" on systems
#      that report as macOS 16.x or older.
#
# Strategy:
#   - Score each candidate by importing a panel of stdlib modules pip
#     depends on (catches pitfall #2).
#   - Run a real `tkinter.Tk()` smoke test in a subshell (catches #1, #3).
#   - Build the venv with `--without-pip` and bootstrap pip ourselves
#     (extra resilience against ensurepip bottle bugs).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"

# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
# Score a Python interpreter:
#   nonzero exit  — unusable (missing or stdlib broken)
#   1             — stdlib OK but Tk runtime fails
#   2             — stdlib OK and Tk runtime works
score_python() {
    local py="$1"
    command -v "$py" >/dev/null 2>&1 || return 1
    # Comprehensive stdlib smoke test — modules pip + knife actually use.
    # This catches broken bottles like the python@3.14 pyexpat symbol issue.
    "$py" - <<'PY' >/dev/null 2>&1 || return 1
import ensurepip                # pip bootstrap
from xml.parsers import expat   # broken on python@3.14 bottle
import ssl                      # network for pip
import sqlite3                  # bundled wheels
import ctypes                   # native deps
PY
    # Tk runtime — isolated subshell so a SIGABRT cannot kill setup.sh.
    if (TK_SILENCE_DEPRECATION=1 "$py" -c \
            "import tkinter; r = tkinter.Tk(); r.destroy()") \
            >/dev/null 2>&1; then
        echo 2
    else
        echo 1
    fi
}

bootstrap_pip() {
    local venv="$1"
    if "$venv/bin/python" -m ensurepip --upgrade --default-pip >/dev/null 2>&1; then
        return 0
    fi
    echo "    (ensurepip unavailable — falling back to get-pip.py)"
    local tmp="/tmp/knife-get-pip.$$.py"
    if command -v curl >/dev/null 2>&1; then
        curl -sSL https://bootstrap.pypa.io/get-pip.py -o "$tmp"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$tmp" https://bootstrap.pypa.io/get-pip.py
    else
        echo "❌ Neither curl nor wget found; cannot bootstrap pip." >&2
        return 1
    fi
    "$venv/bin/python" "$tmp" --quiet
    rm -f "$tmp"
}

# ---------------------------------------------------------------
# Pick the best Python
# ---------------------------------------------------------------
# Order of preference, best (most likely to ship a self-contained Tk) first:
#   1. python.org framework install (bundles its own Tcl/Tk — no brew/system deps)
#   2. Homebrew python with python-tk installed (Apple Silicon then Intel)
#   3. PATH lookups
#   4. Apple's /usr/bin/python3 (last resort — Tk often broken on macOS 26)
CANDIDATES=(
    # python.org installer (most reliable on macOS — bundled Tk)
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3
    /Library/Frameworks/Python.framework/Versions/3.10/bin/python3
    # Homebrew Apple Silicon
    /opt/homebrew/opt/python@3.13/bin/python3.13
    /opt/homebrew/opt/python@3.12/bin/python3.12
    /opt/homebrew/opt/python@3.11/bin/python3.11
    /opt/homebrew/opt/python@3.14/bin/python3.14
    # Homebrew Intel
    /usr/local/opt/python@3.13/bin/python3.13
    /usr/local/opt/python@3.12/bin/python3.12
    /usr/local/opt/python@3.11/bin/python3.11
    /usr/local/opt/python@3.14/bin/python3.14
    # PATH lookups
    python3.13 python3.12 python3.11 python3.10 python3.14
    # Apple system Python (often broken Tk on macOS 26)
    /usr/bin/python3
    python3
)

PYTHON=""
PYTHON_SCORE=0
echo "==> Probing available Python interpreters…"
for cmd in "${CANDIDATES[@]}"; do
    s="$(score_python "$cmd" 2>/dev/null || true)"
    [[ -z "$s" ]] && continue
    echo "    $cmd → score=$s"
    if (( s > PYTHON_SCORE )); then
        PYTHON="$cmd"
        PYTHON_SCORE="$s"
        if (( s >= 2 )); then break; fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    cat >&2 <<EOF

❌ No usable Python found on this system.

The most reliable fix on macOS is to install the official Python
from python.org — it bundles its own Tcl/Tk and has no dependency
on Homebrew or system frameworks:

    https://www.python.org/downloads/macos/

After installing (use Python 3.12 or 3.13), run this script again.
EOF
    exit 1
fi

echo
echo "==> Using Python: $PYTHON"
"$PYTHON" --version
if (( PYTHON_SCORE < 2 )); then
    echo "    ⚠️  Tk runtime test failed — the GUI ('knife gui') will not work."
    echo "       The CLI will still work fine."
fi

# ---------------------------------------------------------------
# Build venv (skip ensurepip — we bootstrap manually for resilience)
# ---------------------------------------------------------------
echo "==> Creating virtual environment at $VENV"
rm -rf "$VENV"
"$PYTHON" -m venv --without-pip "$VENV"

echo "==> Bootstrapping pip"
bootstrap_pip "$VENV"

echo "==> Installing knife (editable mode) and dependencies"
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install -e "$HERE" --quiet

# ---------------------------------------------------------------
# Final Tk verification (in the new venv this time)
# ---------------------------------------------------------------
HAS_TK=0
if (TK_SILENCE_DEPRECATION=1 "$VENV/bin/python" \
        -c "import tkinter; r = tkinter.Tk(); r.destroy()") >/dev/null 2>&1; then
    HAS_TK=1
fi

echo
echo "✅ knife installed."
echo
if [[ $HAS_TK == 0 ]]; then
    cat <<EOF
⚠️  Tkinter does not run cleanly in this Python — 'knife gui' will crash.
    Most reliable fix:
      1. Download Python 3.13 from https://www.python.org/downloads/macos/
      2. Install the .pkg file
      3. rm -rf .venv && ./setup.sh
    The CLI works fine without this.

EOF
fi
echo "Quick start:"
echo "    $VENV/bin/knife list --top 10"
echo "    $VENV/bin/knife watch"
[[ $HAS_TK == 1 ]] && echo "    $VENV/bin/knife gui"
echo "    $VENV/bin/knife daemon"
echo
echo "Optional — add an alias to ~/.zshrc so you can just type 'knife':"
echo "    echo \"alias knife='$VENV/bin/knife'\" >> ~/.zshrc"
echo "    source ~/.zshrc"
