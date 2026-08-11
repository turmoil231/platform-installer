#!/usr/bin/env bash
# packaging/build_image.sh
#
# Builds the platform-installer container image and produces a distribution
# consisting of two tarballs:
#
#   dist/platform-installer-<VERSION>-container.tar.gz
#     The container image.  Large (~800MB-1.2GB).  Loaded once on the admin server.
#
#   dist/platform-installer-<VERSION>-wrapper.tar.gz
#     Everything the admin server needs to operate the installer:
#       platform-installer          the self-contained wrapper script
#       installer.env.example       path configuration template
#     Both files go in the same directory as the container tarball.
#     The wrapper auto-loads the image on first run if it is not already loaded.
#
# Usage:
#   ./packaging/build_image.sh
#   ./packaging/build_image.sh --version 2025.1.0
#   ./packaging/build_image.sh --runtime docker
#   ./packaging/build_image.sh --push-to registry.example.internal:5000/platform
#   ./packaging/build_image.sh --skip-collections   (use existing collections/ dir)
#
# Requirements on build host:
#   podman (preferred) or docker
#   packaging/seed_pip_cache.sh already run   (pip-cache/ dir populated)
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
IMAGE_NAME="platform-installer"

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
WRAPPER_TARBALL="${DIST_DIR}/${IMAGE_NAME}-${VERSION}-wrapper.tar.gz"

mkdir -p "${DIST_DIR}"

echo "==> Building platform-installer ${VERSION}"
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

# ── Verify pip cache ──────────────────────────────────────────────────────────
PIP_CACHE="${REPO_ROOT}/packaging/pip-cache"
if [[ $(ls "${PIP_CACHE}"/*.whl 2>/dev/null | wc -l) -lt 5 ]]; then
  echo "==> Pip cache appears empty — seeding now ..."
  "${REPO_ROOT}/packaging/seed_pip_cache.sh"
fi
echo "==> Pip cache: $(ls "${PIP_CACHE}" | wc -l) packages"

# ── Build the image ───────────────────────────────────────────────────────────
echo ""
echo "==> Building container image ..."
${RUNTIME} build \
  --file       "${REPO_ROOT}/packaging/Containerfile" \
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
echo ""
echo "==> Running smoke test ..."
${RUNTIME} run --rm "${IMAGE_TAG}" --help > /dev/null \
  && echo "    ✔ Smoke test passed" \
  || { echo "    ✘ Smoke test FAILED"; exit 1; }

# ── Save image tarball ────────────────────────────────────────────────────────
echo ""
echo "==> Saving image tarball ..."
${RUNTIME} save "${IMAGE_TAG}" | gzip > "${CONTAINER_TARBALL}"
sha256sum "${CONTAINER_TARBALL}" > "${CONTAINER_TARBALL}.sha256"
CONTAINER_SIZE="$(du -sh "${CONTAINER_TARBALL}" | cut -f1)"
echo "    ✔ ${CONTAINER_TARBALL} (${CONTAINER_SIZE})"
echo "    ✔ ${CONTAINER_TARBALL}.sha256"

# ── Build wrapper tarball ─────────────────────────────────────────────────────
# The wrapper tarball is small (~5KB) and contains exactly two files:
#   platform-installer       the self-contained wrapper script (stamped with version)
#   installer.env.example    path configuration template
#
# Both files are shipped alongside the container tarball.
# The wrapper script auto-loads the image if it is not already in the local store.
echo ""
echo "==> Building wrapper tarball ..."

WRAPPER_STAGING="$(mktemp -d)"
trap 'rm -rf "${WRAPPER_STAGING}"' EXIT

WRAPPER_DIR="${WRAPPER_STAGING}/${IMAGE_NAME}-${VERSION}-wrapper"
mkdir -p "${WRAPPER_DIR}"

# ── Stamp the wrapper script ──────────────────────────────────────────────────
# platform-installer lives in the repo root as a real, editable file.
# build_image.sh copies it into the wrapper tarball and substitutes the
# @@VERSION@@ and @@IMAGE_NAME@@ placeholders with the actual values.
WRAPPER_SOURCE="${REPO_ROOT}/platform-installer"
[[ -f "${WRAPPER_SOURCE}" ]] \
  || { echo "ERROR: platform-installer wrapper not found at ${WRAPPER_SOURCE}"; exit 1; }

sed \
  -e "s|@@VERSION@@|${VERSION}|g" \
  -e "s|@@IMAGE_NAME@@|${IMAGE_NAME}|g" \
  "${WRAPPER_SOURCE}" \
  > "${WRAPPER_DIR}/platform-installer"

chmod +x "${WRAPPER_DIR}/platform-installer"
echo "    Stamped: platform-installer (version=${VERSION}, image=${IMAGE_NAME})"

# ── Write installer.env.example ───────────────────────────────────────────────
cat > "${WRAPPER_DIR}/installer.env.example" << ENV_EXAMPLE
# installer.env
#
# Local path configuration for the platform-installer wrapper script.
# Copy this file to installer.env in the same directory and fill in values.
# This file is sourced by the platform-installer wrapper on every invocation.
#
# Do NOT commit installer.env to version control — it contains site-specific
# paths and must be created manually on each admin server.

# ── Required ──────────────────────────────────────────────────────────────────

# Directory containing platform-config.yaml and platform-manifest.yaml
CONFIG_DIR="\${HOME}/platform"

# Directory where installer state is persisted between runs.
# Must be on persistent storage (not /tmp).
# The SQLite state DB and ansible-runner artifacts are written here.
STATE_DIR="\${HOME}/platform/state"

# SSH key pair used by the installer to connect to managed hosts.
# Private key must not have a passphrase (or use ssh-agent).
SSH_PUBLIC_KEY="\${HOME}/.ssh/platform-installer.pub"
SSH_PRIVATE_KEY="\${HOME}/.ssh/platform-installer"

# Internal CA certificate and private key.
# The CA cert is distributed to all managed nodes.
# The CA key is used to issue TLS certificates for platform services.
INTERNAL_CA_CERT="/etc/pki/ca-trust/source/anchors/internal-ca.crt"
INTERNAL_CA_KEY="/etc/pki/private/internal-ca.key"

# Merged pull secret (Red Hat entitlement + mirror registry auth).
PULL_SECRET="\${HOME}/pull-secret.json"

# Root directory containing all pre-staged assets.
# Mounted read-only into the container — large files never copied in.
ASSETS_DIR="/mnt/platform-assets"

# ── Optional ──────────────────────────────────────────────────────────────────

# Container runtime. Auto-detected (podman preferred) if not set.
# CONTAINER_RUNTIME=podman

# Additional volume mounts (space-separated host:container[:options] pairs).
# EXTRA_VOLUMES=""

# Additional podman/docker run flags.
# EXTRA_RUN_FLAGS=""

# Proxy settings — forwarded into the container if set.
# HTTP_PROXY=""
# HTTPS_PROXY=""
# NO_PROXY=".example.internal,.svc,.cluster.local,10.0.0.0/8"
ENV_EXAMPLE

echo "    Wrote: installer.env.example"

# ── Package the wrapper dir ───────────────────────────────────────────────────
tar \
  --create --gzip \
  --file="${WRAPPER_TARBALL}" \
  --directory="${WRAPPER_STAGING}" \
  "${IMAGE_NAME}-${VERSION}-wrapper"

sha256sum "${WRAPPER_TARBALL}" > "${WRAPPER_TARBALL}.sha256"
WRAPPER_SIZE="$(du -sh "${WRAPPER_TARBALL}" | cut -f1)"
echo "    ✔ ${WRAPPER_TARBALL} (${WRAPPER_SIZE})"
echo "    ✔ ${WRAPPER_TARBALL}.sha256"

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
echo "  BUILD COMPLETE — platform-installer ${VERSION}"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "  Image tarball:   ${CONTAINER_TARBALL}"
echo "  Image checksum:  ${CONTAINER_TARBALL}.sha256"
echo "  Wrapper tarball: ${WRAPPER_TARBALL}"
echo ""
echo "  Transfer both tarballs + checksums to the admin server:"
echo ""
echo "    scp \\"
echo "      ${CONTAINER_TARBALL} \\"
echo "      ${CONTAINER_TARBALL}.sha256 \\"
echo "      ${WRAPPER_TARBALL} \\"
echo "      ${WRAPPER_TARBALL}.sha256 \\"
echo "      admin-server:/opt/platform-installer/"
echo ""
echo "  On the admin server:"
echo ""
echo "    cd /opt/platform-installer"
echo "    sha256sum -c ${IMAGE_NAME}-${VERSION}-wrapper.tar.gz.sha256"
echo "    tar -xzf ${IMAGE_NAME}-${VERSION}-wrapper.tar.gz"
echo "    cd ${IMAGE_NAME}-${VERSION}-wrapper"
echo "    cp installer.env.example installer.env"
echo "    \$EDITOR installer.env"
echo "    export VAULT_ROLE_ID=<role-id>"
echo "    export VAULT_SECRET_ID=<secret-id>"
echo "    ./platform-installer deploy"
echo ""
echo "  The wrapper will automatically load the image on first run."
echo "  No separate 'podman load' step required."
echo "════════════════════════════════════════════════════════════════"
