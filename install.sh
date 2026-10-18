#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

echo "=============================================================================="
echo "                   MULTIGRAVITY INSTALLER & SETUP"
echo "=============================================================================="

# 1. Dependency Checks
echo "[1/6] Checking system prerequisites..."
MISSING_PKGS=()

command -v python3 >/dev/null 2>&1 || MISSING_PKGS+=("python")
command -v dbus-run-session >/dev/null 2>&1 || MISSING_PKGS+=("dbus")
command -v gnome-keyring-daemon >/dev/null 2>&1 || MISSING_PKGS+=("gnome-keyring")
command -v secret-tool >/dev/null 2>&1 || MISSING_PKGS+=("libsecret")
command -v xdg-open >/dev/null 2>&1 || MISSING_PKGS+=("xdg-utils")

if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    echo "[!] Missing required system packages: ${MISSING_PKGS[*]}"
    echo "    On Arch Linux, install them with:"
    echo "        sudo pacman -S ${MISSING_PKGS[*]}"
    exit 1
fi

AGY_BIN="${AGY_BIN:-/usr/sbin/agy}"
if [ ! -x "$AGY_BIN" ]; then
    AGY_BIN="$(which agy 2>/dev/null || true)"
fi

if [ -z "$AGY_BIN" ] || [ ! -x "$AGY_BIN" ]; then
    echo "[!] Antigravity CLI ('agy') not found in PATH or /usr/sbin/agy."
    echo "    Please install or configure Antigravity CLI first."
    exit 1
fi
echo "✓ System dependencies and agy binary verified."

# 2. Local Configuration
echo "[2/6] Checking configuration..."
if [ ! -f "config.local.toml" ]; then
    if [ -f "config.example.toml" ]; then
        echo "Creating config.local.toml from config.example.toml..."
        cp config.example.toml config.local.toml
        chmod 0600 config.local.toml
        echo "✓ Created config.local.toml."
    else
        echo "[!] Neither config.local.toml nor config.example.toml was found."
        exit 1
    fi
else
    echo "✓ config.local.toml is present."
fi

# 3. Permissions & Scripts
echo "[3/6] Setting file permissions..."
chmod +x scripts/mgy scripts/multigravity-browser-helper
echo "✓ Scripts marked executable."

# 4. MCP Server Registration
echo "[4/6] Verifying MCP server registration..."
USER_UID="$(id -u)"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$USER_UID}"

if ! "$AGY_BIN" mcp list 2>/dev/null | grep -q "multigravity"; then
    echo "Registering 'multigravity' MCP server in Antigravity..."
    "$AGY_BIN" mcp add multigravity stdio "env PYTHONPATH=$REPO_ROOT XDG_RUNTIME_DIR=$RUNTIME_DIR python3 -m multigravity.mcp_shim"
    echo "✓ multigravity MCP server registered."
else
    echo "✓ multigravity MCP server is already registered."
fi

# Sync fleet instructions
python3 -c "
from multigravity.launcher import update_mcp_instructions
from multigravity.prompts import generate_main_agent_prompt
update_mcp_instructions(generate_main_agent_prompt())
" 2>/dev/null || true
echo "✓ Fleet instructions synchronized."

# Pre-approve MCP tool permissions in settings.json
python3 -c "
from multigravity.launcher import ensure_mcp_permissions_preapproved
ensure_mcp_permissions_preapproved()
" 2>/dev/null || true
echo "✓ Multigravity MCP tool permissions pre-approved in settings.json."


# 5. Remote Control Setup
echo "[5/6] Ensuring Remote Control daemon is active..."
if "$AGY_BIN" remote-control status 2>/dev/null | grep -q "inactive"; then
    echo "Starting Antigravity Remote Control daemon..."
    "$AGY_BIN" remote-control start 2>/dev/null || true
    echo "✓ Remote Control daemon started."
else
    echo "✓ Remote Control daemon is active."
fi

# 6. Shell Alias & Symlink
echo "[6/6] Configuring 'mgy' command and alias..."
MGY_SCRIPT="$REPO_ROOT/scripts/mgy"

# Symlink to ~/.local/bin/mgy if directory exists
if [ -d "$HOME/.local/bin" ] && [ -w "$HOME/.local/bin" ]; then
    ln -sf "$MGY_SCRIPT" "$HOME/.local/bin/mgy"
    echo "✓ Symlinked mgy -> $HOME/.local/bin/mgy"
fi

# Add alias to ~/.bashrc if not present
ALIAS_LINE="alias mgy=\"$MGY_SCRIPT\""
if [ -f "$HOME/.bashrc" ]; then
    if ! grep -q "alias mgy=" "$HOME/.bashrc"; then
        echo "" >> "$HOME/.bashrc"
        echo "# Multigravity CLI launcher" >> "$HOME/.bashrc"
        echo "$ALIAS_LINE" >> "$HOME/.bashrc"
        echo "✓ Added 'mgy' alias to $HOME/.bashrc"
    else
        echo "✓ 'mgy' alias already present in $HOME/.bashrc"
    fi
fi

# Add alias to ~/.zshrc if present
if [ -f "$HOME/.zshrc" ]; then
    if ! grep -q "alias mgy=" "$HOME/.zshrc"; then
        echo "" >> "$HOME/.zshrc"
        echo "# Multigravity CLI launcher" >> "$HOME/.zshrc"
        echo "$ALIAS_LINE" >> "$HOME/.zshrc"
        echo "✓ Added 'mgy' alias to $HOME/.zshrc"
    else
        echo "✓ 'mgy' alias already present in $HOME/.zshrc"
    fi
fi

echo ""
echo "=============================================================================="
echo "                 MULTIGRAVITY INSTALLATION COMPLETE! ✓"
echo "=============================================================================="
echo "You can now run 'mgy' from any terminal:"
echo ""
echo "  mgy                  # Launch interactive agy with fleet instructions"
echo "  mgy \"prompt\"        # Launch with initial prompt + fleet instructions"
echo "  mgy status           # Inspect background worker accounts"
echo "  mgy doctor           # Run live system diagnostics"
echo "  mgy stop             # Cleanly stop background core daemon"
echo ""
echo "Note: 'mgy' auto-approves all tool calls and shell commands without prompts."
echo ""
echo "If your current shell does not see 'mgy' yet, run:"
echo "    source ~/.bashrc   # or use ~/.local/bin/mgy directly"
echo "=============================================================================="

