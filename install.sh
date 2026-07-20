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

INSTALL_BIN="/usr/local/bin/yapm"
SRC_FILE="$(dirname "$0")/yapm.py"

if [ ! -f "$SRC_FILE" ]; then
    echo -e "${RED}Error: yapm.py not found in $(dirname "$0")${NC}"
    exit 1
fi

# Create system data dirs
mkdir -p /etc/yapm
mkdir -p /var/lib/yapm/packages
mkdir -p /var/lib/yapm/cache

# Copy and make executable
cp "$SRC_FILE" "$INSTALL_BIN"
chmod +x "$INSTALL_BIN"

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
