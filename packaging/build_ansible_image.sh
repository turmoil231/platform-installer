#!/usr/bin/env bash
# packaging/build_ansible_image.sh
#
# Builds the Ansible execution image: ansible-core plus every collection
# required by ansible/playbooks/, preloaded at build time. This is the image
# platform-installer's containerized Ansible execution (installer/runner/ansible.py)
# launches per playbook run via ansible-runner's container executor.
#
# The compiled platform-installer binary is NOT built or packaged by this
# script — see packaging/build_binary.sh for that.
#
# Output:
#   dist/platform-ansible-exec-<VERSION>-container.tar.gz
#   dist/platform-ansible-exec-<VERSION>-container.tar.gz.sha256
#
# This image travels inside the haul, not next to the binary: push it
# (--push-to) to the registry your haul is built from and list it in the
# Hauler manifest. At deploy time the installer pulls it from Hauler's
# registry under the reference given by --ansible-image (default
# platform-ansible-exec:<version>), i.e. the haul reference without its
# registry host (see LocalServices in installer/runner/services.py).
#
# Usage:
#   ./packaging/build_ansible_image.sh
#   ./packaging/build_ansible_image.sh --version 2025.1.0
#   ./packaging/build_ansible_image.sh --runtime docker
#   ./packaging/build_ansible_image.sh --push-to registry.example.internal:5000/platform
#   ./packaging/build_ansible_image.sh --skip-collections   (use existing collections/ dir)
#
# Requirements on build host:
#   podman (preferred) or docker
#   scripts/stage_collections.sh already run  (collections/ dir populated)
#     OR pass --skip-collections if collections/ is already up to date

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Defaults ──────────────────────────────────────────────────────────────────
VERSION=""
RUNTIME=""
DIST_DIR="${REPO_ROOT}/dist"
PUSH_TO=""
SKIP_COLLECTIONS=false
IMAGE_NAME="platform-ansible-exec"

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)           VERSION="$2";           shift 2 ;;
    --runtime)           RUNTIME="$2";           shift 2 ;;
    --dist-dir)          DIST_DIR="$2";          shift 2 ;;
    --push-to)           PUSH_TO="$2";           shift 2 ;;
    --skip-collections)  SKIP_COLLECTIONS=true;  shift   ;;
    --image-name)        IMAGE_NAME="$2";        shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

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
IMAGE_TAG_LATEST="${IMAGE_NAME}:latest"
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
GIT_COMMIT="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo 'unknown')"

CONTAINER_TARBALL="${DIST_DIR}/${IMAGE_NAME}-${VERSION}-container.tar.gz"

mkdir -p "${DIST_DIR}"

echo "==> Building ${IMAGE_NAME} ${VERSION}"
echo "    Image tag:  ${IMAGE_TAG}"
echo "    Git commit: ${GIT_COMMIT}"
echo ""

# ── Stage collections ─────────────────────────────────────────────────────────
if [[ "${SKIP_COLLECTIONS}" == "false" ]]; then
  echo "==> Staging Ansible collections ..."
  "${REPO_ROOT}/scripts/stage_collections.sh" \
    --staging-root "${REPO_ROOT}" \
    --requirements "${REPO_ROOT}/ansible/collections/requirements.yml" \
    --lock-file    "${REPO_ROOT}/collections.lock.yml"
  echo "    $(ls "${REPO_ROOT}/collections"/*.tar.gz 2>/dev/null | wc -l) collection tarballs staged"
else
  [[ -d "${REPO_ROOT}/collections" ]] \
    || { echo "ERROR: collections/ missing and --skip-collections set"; exit 1; }
  echo "==> Using existing staged collections"
fi

# ── Build the image ───────────────────────────────────────────────────────────
echo ""
echo "==> Building container image ..."
${RUNTIME} build \
  --file       "${REPO_ROOT}/packaging/Containerfile.ansible-exec" \
  --tag        "${IMAGE_TAG}" \
  --tag        "${IMAGE_TAG_LATEST}" \
  --build-arg  "VERSION=${VERSION}"       \
  --build-arg  "BUILD_DATE=${BUILD_DATE}" \
  --build-arg  "GIT_COMMIT=${GIT_COMMIT}" \
  "${REPO_ROOT}"

echo ""
echo "==> Build complete: ${IMAGE_TAG}"
${RUNTIME} image inspect "${IMAGE_TAG}" \
  --format "    Size: {{.Size}} bytes" 2>/dev/null || true

# ── Smoke test ────────────────────────────────────────────────────────────────
# There is no CLI in this image — verify ansible-core runs and collections
# were installed correctly instead of a --help check.
echo ""
echo "==> Running smoke test ..."
${RUNTIME} run --rm "${IMAGE_TAG}" ansible-playbook --version > /dev/null \
  && echo "    ✔ ansible-playbook runs" \
  || { echo "    ✘ ansible-playbook smoke test FAILED"; exit 1; }

${RUNTIME} run --rm "${IMAGE_TAG}" \
  ansible-galaxy collection list --collections-path /opt/ansible/collections \
  || { echo "    ✘ Collection list FAILED"; exit 1; }
echo "    ✔ Collections present"

# ── Save image tarball ────────────────────────────────────────────────────────
echo ""
echo "==> Saving image tarball ..."
${RUNTIME} save "${IMAGE_TAG}" | gzip > "${CONTAINER_TARBALL}"
sha256sum "${CONTAINER_TARBALL}" > "${CONTAINER_TARBALL}.sha256"
CONTAINER_SIZE="$(du -sh "${CONTAINER_TARBALL}" | cut -f1)"
echo "    ✔ ${CONTAINER_TARBALL} (${CONTAINER_SIZE})"
echo "    ✔ ${CONTAINER_TARBALL}.sha256"

# ── Optional registry push ────────────────────────────────────────────────────
if [[ -n "${PUSH_TO}" ]]; then
  echo ""
  echo "==> Pushing to registry: ${PUSH_TO} ..."
  REGISTRY_TAG="${PUSH_TO}/${IMAGE_TAG}"
  ${RUNTIME} tag "${IMAGE_TAG}" "${REGISTRY_TAG}"
  ${RUNTIME} push "${REGISTRY_TAG}"
  echo "    ✔ Pushed: ${REGISTRY_TAG}"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  BUILD COMPLETE — ${IMAGE_NAME} ${VERSION}"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "  Image tarball:   ${CONTAINER_TARBALL}"
echo "  Image checksum:  ${CONTAINER_TARBALL}.sha256"
echo ""
echo "  Add this image to the haul (push with --push-to, then list it in the"
echo "  Hauler manifest). The installer pulls it from Hauler's registry at"
echo "  deploy time; it is not shipped next to the binary."
echo "════════════════════════════════════════════════════════════════"
