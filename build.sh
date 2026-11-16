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

# Build from the checked-in spec, not from CLI flags: PyInstaller rewrites
# <name>.spec on every flag-based run, which would silently discard the
# hand-maintained `_SYSTEM_LIB_EXCLUDES` fix in that spec (older-glibc servers
# can't load the build machine's newer libz/libbz2). The spec already carries
# every adapter as a hidden import, so it is the single source of truth.
python -m PyInstaller --clean --noconfirm chatlistctl.spec

echo
echo "Built: dist/chatlistctl"
file dist/chatlistctl 2>/dev/null || true
