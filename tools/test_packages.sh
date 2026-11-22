#!/usr/bin/env bash
# Runs package tests inside a checkout of the core (Kriflow/kriflow), as they ran there before
# the move: every package folder of this repository replaces the core's `connectors/<name>`, so
# the defaults the core installs in a test are this repository's, then pytest runs from the core.
#
#   tools/test_packages.sh CORE [all | none | <name>...]   (none: only this repository's tests/)
#
# CORE must be synced (`uv sync --all-packages` and `pnpm install`, as the core's CI does).
set -euo pipefail
core=$(cd "$1" && pwd)
shift
here=$(cd "$(dirname "$0")/.." && pwd)
names=()
for manifest in "$here"/*/swarmify-connector.toml; do
  name=$(basename "$(dirname "$manifest")")
  rm -rf "$core/connectors/$name"
  cp -R "$here/$name" "$core/connectors/$name"
  names+=("$name")
done
if [ "$#" -eq 0 ] || [ "$1" = all ]; then set -- "${names[@]}"; fi
# This repository's own tests (check tool, index against the core's review), in place.
(cd "$here" && "$core/.venv/bin/python" -m pytest -q -p no:cacheprovider --no-cov tests)
[ "${1:-}" = none ] && exit 0
cd "$core"
# --no-cov: the core's coverage floor is for the core's own suite.
uv run pytest --no-cov "${@/#/connectors/}"
