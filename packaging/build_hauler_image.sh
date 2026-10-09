#!/usr/bin/env bash
# packaging/build_hauler_image.sh
#
# Builds the Hauler image (packaging/Containerfile.hauler): the only
# container image shipped next to the platform-installer binary. The
# Ansible execution image and everything else travel inside the haul.
#
# Output:
#   dist/platform-hauler-<VERSION>-container.tar.gz
#   dist/platform-hauler-<VERSION>-container.tar.gz.sha256
#
# Transfer both files to the admin server, alongside the compiled binary.
# The binary auto-loads this image on first run if it isn't already loaded
# in the local podman/docker store (see LocalServices.ensure_running()).
#
# Usage:
#   ./packaging/build_hauler_image.sh --hauler-binary /path/to/hauler
#   ./packaging/build_hauler_image.sh --hauler-binary ./hauler --version 2025.1.0 --runtime docker
#
# Requirements on build host:
#   podman (preferred) or docker
#   A linux/amd64 hauler binary (from the hauler-dev/hauler GitHub releases),
#   checksum-verified by whoever staged it

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Defaults ──────────────────────────────────────────────────────────────────
VERSION=""
RUNTIME=""
DIST_DIR="${REPO_ROOT}/dist"
HAULER_BINARY=""
IMAGE_NAME="platform-hauler"

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)        VERSION="$2";        shift 2 ;;
    --runtime)        RUNTIME="$2";        shift 2 ;;
    --dist-dir)       DIST_DIR="$2";       shift 2 ;;
    --hauler-binary)  HAULER_BINARY="$2";  shift 2 ;;
    --image-name)     IMAGE_NAME="$2";     shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

[[ -n "${HAULER_BINARY}" ]] || { echo "ERROR: --hauler-binary is required"; exit 1; }
[[ -f "${HAULER_BINARY}" ]] || { echo "ERROR: ${HAULER_BINARY} not found"; exit 1; }

# ── Detect runtime ────────────────────────────────────────────────────────────
if [[ -z "${RUNTIME}" ]]; then
  if command -v podman &>/dev/null; then RUNTIME="podman"
  elif command -v docker &>/dev/null; then RUNTIME="docker"
  else echo "ERROR: Neither podman nor docker found"; exit 1
  fi
fi
echo "==> Container runtime: ${RUNTIME} ($(${RUNTIME} --version | head -1))"

# ── Detect version ────────────────────────────────────────────────────────────
if [[ -z "${VERSION}" ]]; then
  VERSION="$(grep '^version' "${REPO_ROOT}/pyproject.toml" | head -1 \
             | sed 's/.*= *"\(.*\)"/\1/')"
fi
[[ -z "${VERSION}" ]] && { echo "ERROR: Could not detect version"; exit 1; }

IMAGE_TAG="${IMAGE_NAME}:${VERSION}"
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
GIT_COMMIT="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo 'unknown')"
CONTAINER_TARBALL="${DIST_DIR}/${IMAGE_NAME}-${VERSION}-container.tar.gz"

mkdir -p "${DIST_DIR}"

# ── Stage the binary into the build context ──────────────────────────────────
STAGE_DIR="${REPO_ROOT}/.build-hauler"
rm -rf "${STAGE_DIR}"
mkdir -p "${STAGE_DIR}"
cp "${HAULER_BINARY}" "${STAGE_DIR}/hauler"
trap 'rm -rf "${STAGE_DIR}"' EXIT

# ── Build the image ───────────────────────────────────────────────────────────
echo "==> Building ${IMAGE_TAG}"
${RUNTIME} build \
  --file       "${REPO_ROOT}/packaging/Containerfile.hauler" \
  --tag        "${IMAGE_TAG}" \
  --build-arg  "VERSION=${VERSION}"       \
  --build-arg  "BUILD_DATE=${BUILD_DATE}" \
  --build-arg  "GIT_COMMIT=${GIT_COMMIT}" \
  "${REPO_ROOT}"

# ── Smoke test ────────────────────────────────────────────────────────────────
${RUNTIME} run --rm "${IMAGE_TAG}" hauler version > /dev/null \
  && echo "    ✔ hauler runs" \
  || { echo "    ✘ hauler smoke test FAILED"; exit 1; }

# ── Save image tarball ────────────────────────────────────────────────────────
echo "==> Saving image tarball ..."
${RUNTIME} save "${IMAGE_TAG}" | gzip > "${CONTAINER_TARBALL}"
sha256sum "${CONTAINER_TARBALL}" > "${CONTAINER_TARBALL}.sha256"
echo "    ✔ ${CONTAINER_TARBALL} ($(du -sh "${CONTAINER_TARBALL}" | cut -f1))"
echo "    ✔ ${CONTAINER_TARBALL}.sha256"
echo ""
echo "  Transfer alongside dist/platform-installer-${VERSION} to the admin"
echo "  server, same directory. The Ansible execution image goes into the"
echo "  haul instead (see packaging/build_ansible_image.sh)."
