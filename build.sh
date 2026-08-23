#!/usr/bin/env bash
# Builds a standalone single-file binary (dist/chatlistctl) for the current
# platform/architecture. PyInstaller does not cross-compile: run this script
# on the same OS/arch you intend to deploy to (e.g. run it directly on a
# Linux x86_64 server, or in a matching container, to get a Linux x86_64
# binary).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .buildenv ]; then
    python3 -m venv .buildenv
fi
source .buildenv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements-build.txt

python -m PyInstaller --onefile --name chatlistctl --clean --noconfirm \
    --hidden-import core.adapters.claude_code \
    --hidden-import core.adapters.qwen_code \
    --hidden-import core.adapters.codewhale_tui \
    cli.py

echo
echo "Built: dist/chatlistctl"
file dist/chatlistctl 2>/dev/null || true
