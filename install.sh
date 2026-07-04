#!/bin/bash
# ============================================================
# Lori Stream — installer
# macOS 13+, arm64 / x86_64, Python 3.11–3.13
# ============================================================
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
err()  { echo -e "${RED}✗${NC}  $*"; exit 1; }
step() { echo -e "\n${YELLOW}▶${NC} $*"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── 1. Python ────────────────────────────────────────────────
step "Looking for Python 3..."

PYTHON_BIN=""
PYTHON_APP=""

for ver in 3.13 3.12 3.11; do
    candidate="/Library/Frameworks/Python.framework/Versions/$ver/bin/python3"
    app_candidate="/Library/Frameworks/Python.framework/Versions/$ver/Resources/Python.app/Contents/MacOS/Python"
    if [ -x "$candidate" ]; then
        PYTHON_BIN="$candidate"
        PYTHON_APP="$app_candidate"
        ok "Python $ver: $PYTHON_BIN"
        break
    fi
done

# Homebrew Python won't work: TCC binds to Python.app bundle ID,
# which Homebrew Python doesn't have — microphone access will fail.
if [ -z "$PYTHON_BIN" ]; then
    for hb_py in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        if [ -x "$hb_py" ]; then
            err "Only Homebrew Python found ($hb_py) — microphone access won't work (no bundle ID for TCC).
   Install Python from python.org/downloads/ (version 3.11–3.13) and run install.sh again."
        fi
    done
fi

[ -z "$PYTHON_BIN" ] && err "Python 3 not found. Install from python.org/downloads/ and run install.sh again."

# ── 2. Install directory ──────────────────────────────────────
step "Where to install?"
DEFAULT_INSTALL="$HOME/.lori-stream"
echo    "  Default: $DEFAULT_INSTALL"
echo -n "  Enter path (Enter = default): "
read INSTALL_DIR
INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_INSTALL}"
mkdir -p "$INSTALL_DIR"
ok "Directory: $INSTALL_DIR"

# ── 3. Python dependencies ────────────────────────────────────
step "Installing Python dependencies..."
"$PYTHON_BIN" -m pip install --quiet --upgrade pip
"$PYTHON_BIN" -m pip install --quiet \
    sounddevice \
    numpy \
    pyobjc-framework-AVFoundation \
    pyobjc-framework-Cocoa \
    pyobjc-framework-Quartz \
    pyobjc-framework-UserNotifications \
    soundfile \
    mlx-whisper
ok "Dependencies installed."

# ── 4. mlx-whisper model ──────────────────────────────────────
step "mlx-whisper model..."
if [ -d "$HOME/.lori/models" ]; then
    ok "Stable Lori found — its model cache (~/.lori/models) will be reused, no new download."
else
    echo "   mlx-community/whisper-medium-mlx (~1.4 GB) will download automatically"
    echo "   on first run and be cached in models/ (HF_HOME) — only once."
    ok "Ready for first run."
fi

# ── 5. Copy files ─────────────────────────────────────────────
step "Copying files to $INSTALL_DIR..."
cp "$SCRIPT_DIR/lori_stream.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/toggle-stream.sh" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/toggle-stream.sh"

# config.json — copy only if it doesn't exist (don't overwrite user settings)
if [ ! -f "$INSTALL_DIR/config.json" ]; then
    cp "$SCRIPT_DIR/config.json" "$INSTALL_DIR/"
    ok "config.json created (adjust as needed)."
else
    ok "config.json already exists — leaving it as is."
fi

ok "Files copied."

# ── 6. launchd plist ─────────────────────────────────────────
step "Creating launchd agent..."

PLIST_DEST="$HOME/Library/LaunchAgents/com.ri.lori-stream.agent.plist"

# Use Python.app if available (for correct TCC)
if [ -n "$PYTHON_APP" ] && [ -x "$PYTHON_APP" ]; then
    LAUNCHD_PYTHON="$PYTHON_APP"
else
    LAUNCHD_PYTHON="$PYTHON_BIN"
fi

cat > "$PLIST_DEST" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ri.lori-stream.agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>${LAUNCHD_PYTHON}</string>
        <string>${INSTALL_DIR}/lori_stream.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$(dirname "$PYTHON_BIN"):/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
PLIST_EOF

ok "plist created: $PLIST_DEST"

# ── 7. Load agent ─────────────────────────────────────────────
step "Loading launchd agent..."
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load   "$PLIST_DEST"
sleep 2

if launchctl list | grep -q "com.ri.lori-stream.agent"; then
    ok "Agent running."
else
    warn "Agent not found in launchctl list — check logs: $INSTALL_DIR/lori-stream.log"
fi

# ── 8. Done ──────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════════"
echo -e "${GREEN}✅ Lori Stream installed!${NC}"
echo "══════════════════════════════════════════════════════"
echo ""
echo "One-time manual steps required:"
echo ""
echo "  1. macOS permissions (System Settings → Privacy & Security):"
echo "     • Accessibility  → add Python.app"
echo "       Path: $(find /Library/Frameworks/Python.framework -name "Python.app" -maxdepth 5 2>/dev/null | head -1)/Contents/MacOS/Python"
echo "     • Microphone     → add Python.app"
echo "     • Notifications  → allow notifications for Python"
echo "     (already done if the stable Lori is set up on this Mac)"
echo ""
echo "  2. Keyboard shortcut (macOS Shortcuts app):"
echo "     • Open Shortcuts.app"
echo "     • Create a new shortcut: add action 'Run Shell Script'"
echo "       Script: bash $INSTALL_DIR/toggle-stream.sh"
echo "     • Assign a key (e.g. ⌃⌥S) — pick one that doesn't clash"
echo "       with the stable Lori's hotkey if you run both"
echo ""
echo "  3. If notifications don't break through Do Not Disturb:"
echo "     System Settings → Focus → Sleep → Allowed Notifications → Apps → add Python"
echo ""
echo "Logs: tail -f $INSTALL_DIR/lori-stream.log"
echo "Restart agent:"
echo "  launchctl kill SIGTERM gui/\$(id -u)/com.ri.lori-stream.agent"
echo ""
echo "See README.md for details."
echo ""
