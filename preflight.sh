#!/usr/bin/env bash
# Thin entrypoint over the product CLI; all logic lives in
# `python3 -m gideon`.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
exec python3 -m gideon preflight "$@"
