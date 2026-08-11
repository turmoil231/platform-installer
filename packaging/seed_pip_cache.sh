#!/usr/bin/env bash
# packaging/seed_pip_cache.sh
#
# Run this ONCE on a connected build host to download all Python packages
# into packaging/pip-cache/ as wheel files.
#
# build_bundle.sh then uses `pip install --no-index --find-links pip-cache/`
# to build the venv without any network access.  This means the build itself
# is also disconnected-safe after this cache is seeded.
#
# Usage:
#   ./packaging/seed_pip_cache.sh
#   ./packaging/seed_pip_cache.sh --python /usr/bin/python3.11 --extra-index-url <url>
#
# The pip-cache/ directory should be committed to git (or stored in Artifactory).
# It is excluded from the final bundle tarball (only the venv built from it ships).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CACHE_DIR="${SCRIPT_DIR}/pip-cache"
PYTHON_BIN="python3"
EXTRA_INDEX_URL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)           PYTHON_BIN="$2";         shift 2 ;;
    --extra-index-url)  EXTRA_INDEX_URL="$2";    shift 2 ;;
    --cache-dir)        CACHE_DIR="$2";          shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

mkdir -p "${CACHE_DIR}"

echo "==> Seeding pip cache: ${CACHE_DIR}"
echo "    Python: $(${PYTHON_BIN} --version)"
echo ""

EXTRA_ARGS=()
[[ -n "$EXTRA_INDEX_URL" ]] && EXTRA_ARGS+=("--extra-index-url" "$EXTRA_INDEX_URL")

# Download the installer package + all declared dependencies as wheels
# --no-deps on the installer itself first so we get exactly what pyproject.toml declares
# then download all transitive deps

echo "==> Downloading installer package and all dependencies ..."
${PYTHON_BIN} -m pip download \
  --dest "${CACHE_DIR}" \
  --prefer-binary \
  "${EXTRA_ARGS[@]}" \
  "${REPO_ROOT}"

# Also download test dependencies (not included in bundle, but needed for CI)
echo "==> Downloading test dependencies ..."
${PYTHON_BIN} -m pip download \
  --dest "${CACHE_DIR}" \
  --prefer-binary \
  pytest pytest-mock

echo ""
echo "==> Pip cache seeded: $(ls "${CACHE_DIR}" | wc -l) packages"
echo "    Location: ${CACHE_DIR}"
echo ""
echo "  Commit pip-cache/ to git or upload to Artifactory."
echo "  build_bundle.sh will use it with --no-index --find-links."
