#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
local_node="$repo_root/.tools/node-v24.18.0-linux-x64/bin"

if [[ -x "$local_node/node" && -x "$local_node/npm" ]]; then
  export PATH="$local_node:$PATH"
elif command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  :
else
  echo "Node.js 24.18.0 is required. Install it or restore $local_node." >&2
  exit 1
fi

exec npm --prefix "$repo_root/frontend" "$@"
