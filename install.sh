#!/bin/bash

# yapm installer

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m' # No Color

echo "Installing yapm..."

# Check dependencies
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: python3 is required but not installed.${NC}"
    exit 1
fi

# Paths
if [ "$EUID" -eq 0 ]; then
    INSTALL_BIN="/usr/local/bin/yapm"
    echo "Running as root, installing globally to $INSTALL_BIN"
else
    INSTALL_BIN="$HOME/.local/bin/yapm"
fi
SRC_FILE="$(dirname "$0")/yapm.py"

if [ ! -f "$SRC_FILE" ]; then
    echo -e "${RED}Error: yapm.py not found in $(dirname "$0")${NC}"
    exit 1
fi

# Create dirs
if [ "$EUID" -ne 0 ]; then
    mkdir -p "$HOME/.local/bin"
fi
mkdir -p "$HOME/.config/yapm"
mkdir -p "$HOME/.local/share/yapm/packages"

# Copy and make executable
cp "$SRC_FILE" "$INSTALL_BIN"
chmod +x "$INSTALL_BIN"

echo -e "${GREEN}Successfully installed yapm to $INSTALL_BIN${NC}"

# Shadow check
CURRENT_YAPM=$(which yapm 2>/dev/null || echo "")
if [ -n "$CURRENT_YAPM" ] && [ "$CURRENT_YAPM" != "$INSTALL_BIN" ]; then
    echo -e "\n${RED}Warning: yapm is shadowed by $CURRENT_YAPM${NC}"
    echo "The version at $CURRENT_YAPM will be run instead of the new one."
    echo "You might want to remove it: sudo rm $CURRENT_YAPM"
fi

# PATH check
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo -e "\n${RED}Warning: $HOME/.local/bin is not in your PATH.${NC}"
    echo "Add this to your .bashrc or .zshrc:"
    echo 'export PATH="$HOME/.local/bin:$PATH"'
fi

echo -e "\nRun 'yapm version' to verify installation."
