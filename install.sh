#!/usr/bin/env bash
set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

QANOT_HOME="${QANOT_HOME:-$HOME/.qanot}"

echo ""
echo -e "${BOLD}  🪶 Qanot AI Installer${NC}"
echo -e "  ${DIM}The AI agent that flies on its own.${NC}"
echo ""

# Check Python 3.11+
if ! command -v python3 &>/dev/null; then
    echo -e "  ${RED}✗ Python 3 not found.${NC}"
    echo "    Install: https://python.org or 'brew install python'"
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.minor}")')
if [ "$PY_VERSION" -lt 11 ]; then
    echo -e "  ${RED}✗ Python 3.11+ required (found 3.${PY_VERSION})${NC}"
    echo "    Upgrade: brew install python or pyenv install 3.12"
    exit 1
fi
echo -e "  ${GREEN}✓${NC} Python 3.${PY_VERSION}"

# Create venv
if [ -d "$QANOT_HOME" ]; then
    echo -e "  ${DIM}Updating existing installation...${NC}"
else
    echo -e "  ${DIM}Installing to ${QANOT_HOME}...${NC}"
fi

python3 -m venv "$QANOT_HOME/venv"
echo -e "  ${GREEN}✓${NC} Virtual environment"

# Install qanot
"$QANOT_HOME/venv/bin/pip" install --upgrade pip -q
"$QANOT_HOME/venv/bin/pip" install --upgrade qanot -q
echo -e "  ${GREEN}✓${NC} Qanot installed"

# Get version
VERSION=$("$QANOT_HOME/venv/bin/python" -c "from qanot import __version__; print(__version__)")

# Create wrapper script
mkdir -p "$QANOT_HOME/bin"
cat > "$QANOT_HOME/bin/qanot" << WRAPPER
#!/usr/bin/env bash
exec "$QANOT_HOME/venv/bin/qanot" "\$@"
WRAPPER
chmod +x "$QANOT_HOME/bin/qanot"

# Add to PATH
SHELL_NAME=$(basename "$SHELL")
SHELL_RC="$HOME/.bashrc"
if [ "$SHELL_NAME" = "zsh" ]; then
    SHELL_RC="$HOME/.zshrc"
elif [ "$SHELL_NAME" = "fish" ]; then
    SHELL_RC="$HOME/.config/fish/config.fish"
fi

PATH_LINE='export PATH="$HOME/.qanot/bin:$PATH"'
if [ "$SHELL_NAME" = "fish" ]; then
    PATH_LINE='set -gx PATH $HOME/.qanot/bin $PATH'
fi

if ! grep -q ".qanot/bin" "$SHELL_RC" 2>/dev/null; then
    echo "" >> "$SHELL_RC"
    echo "# Qanot AI" >> "$SHELL_RC"
    echo "$PATH_LINE" >> "$SHELL_RC"
    ADDED_PATH=true
else
    ADDED_PATH=false
fi
echo -e "  ${GREEN}✓${NC} CLI ready"

# Check for Ollama
OLLAMA_MSG=""
if command -v ollama &>/dev/null; then
    OLLAMA_MSG="  ${GREEN}✓${NC} Ollama detected — local AI available"
fi

# Done
echo ""
echo -e "  ${GREEN}${BOLD}Qanot AI v${VERSION} installed!${NC}"
if [ -n "$OLLAMA_MSG" ]; then
    echo -e "$OLLAMA_MSG"
fi
echo ""
if [ "$ADDED_PATH" = true ]; then
    echo -e "  Run: ${CYAN}source ${SHELL_RC}${NC}  ${DIM}(one time only)${NC}"
    echo -e "  Then: ${CYAN}qanot init${NC}"
else
    echo -e "  Run: ${CYAN}qanot init${NC}"
fi
echo ""
