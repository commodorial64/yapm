#!/bin/bash

# yapm installer

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "Installing yapm..."

# Must run as root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Error: install.sh must be run with sudo.${NC}"
    echo "  Try: sudo ./install.sh"
    exit 1
fi

# Check dependencies
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: python3 is required but not installed.${NC}"
    exit 1
fi

SRC_DIR="$(dirname "$0")"
INSTALL_BIN="/usr/local/bin/yapm"
SRC_FILE="$SRC_DIR/yapm.py"
SRC_CORE="$SRC_DIR/core"

if [ ! -f "$SRC_FILE" ]; then
    echo -e "${RED}Error: yapm.py not found in $SRC_DIR${NC}"
    exit 1
fi

if [ ! -d "$SRC_CORE" ]; then
    echo -e "${RED}Error: core/ package not found in $SRC_DIR${NC}"
    exit 1
fi

# Create system data dirs
mkdir -p /etc/yapm
mkdir -p /var/lib/yapm/packages
mkdir -p /var/lib/yapm/cache

# Copy yapm.py and the core/ package (remove stale core/ from old installs)
rm -rf /usr/local/bin/core
cp "$SRC_FILE" "$INSTALL_BIN"
cp -r "$SRC_CORE" /usr/local/bin/core
chmod +x "$INSTALL_BIN"

# Sanity check: entry point must import
if ! python3 -c "import sys; sys.path.insert(0, '/usr/local/bin'); import yapm" 2>/dev/null; then
    echo -e "${RED}Error: installed yapm failed to import.${NC}"
    rm -f "$INSTALL_BIN"
    rm -rf /usr/local/bin/core
    exit 1
fi

echo -e "${GREEN}Successfully installed yapm to $INSTALL_BIN${NC}"

# Shadow check
CURRENT_YAPM=$(which yapm 2>/dev/null || echo "")
if [ -n "$CURRENT_YAPM" ] && [ "$CURRENT_YAPM" != "$INSTALL_BIN" ]; then
    echo -e "\n${YELLOW}Warning: another yapm was found at $CURRENT_YAPM${NC}"
    echo "It may shadow the newly installed version."
    echo "You might want to remove it: sudo rm $CURRENT_YAPM"
fi

# Run first-time setup (completions + fetch-count)
"$INSTALL_BIN" setup 2>/dev/null || true

echo -e "\nRun 'sudo yapm version' to verify installation."
