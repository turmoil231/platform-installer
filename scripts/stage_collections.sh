#!/usr/bin/env bash
# scripts/stage_collections.sh
#
# Run this script in a CONNECTED environment (your build pipeline or a
# connected staging server) BEFORE transferring assets to the air-gapped
# admin server.
#
# What it does:
#   1. Reads ansible/collections/requirements.yml
#   2. Installs each collection into a temp directory
#   3. Re-packages each as a .tar.gz in assets.staging_root/collections/
#   4. Pushes tarballs to Artifactory (if --push-to-artifactory is set)
#   5. Writes a collections.lock.yml recording exact versions and SHA-256
#      checksums — this feeds into platform-manifest.yaml validation
#
# Usage:
#   ./scripts/stage_collections.sh \
#       --staging-root /mnt/platform-assets \
#       --requirements ansible/collections/requirements.yml \
#       [--push-to-artifactory https://artifactory.example.internal] \
#       [--artifactory-user admin] \
#       [--artifactory-password-env ARTIFACTORY_PASSWORD] \
#       [--gitlab-token-env GITLAB_TOKEN]
#
# Dependencies (on the staging/build host):
#   ansible-galaxy (ansible-core)
#   curl
#   sha256sum
#   jq (optional — for Artifactory API calls)

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
STAGING_ROOT="/mnt/platform-assets"
REQUIREMENTS_FILE="ansible/collections/requirements.yml"
PUSH_TO_ARTIFACTORY=""
ARTIFACTORY_USER=""
ARTIFACTORY_PASSWORD_ENV="ARTIFACTORY_PASSWORD"
GITLAB_TOKEN_ENV="GITLAB_TOKEN"
COLLECTIONS_OUTPUT_DIR=""         # Set from STAGING_ROOT below
LOCK_FILE="collections.lock.yml"
ANSIBLE_GALAXY_SERVER_TIMEOUT=60

# ── Argument parsing ───────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --staging-root)            STAGING_ROOT="$2";            shift 2 ;;
    --requirements)            REQUIREMENTS_FILE="$2";       shift 2 ;;
    --push-to-artifactory)     PUSH_TO_ARTIFACTORY="$2";     shift 2 ;;
    --artifactory-user)        ARTIFACTORY_USER="$2";        shift 2 ;;
    --artifactory-password-env) ARTIFACTORY_PASSWORD_ENV="$2"; shift 2 ;;
    --gitlab-token-env)        GITLAB_TOKEN_ENV="$2";        shift 2 ;;
    --lock-file)               LOCK_FILE="$2";               shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

COLLECTIONS_OUTPUT_DIR="${STAGING_ROOT}/collections"
INSTALL_TMP="$(mktemp -d)"
trap 'rm -rf "$INSTALL_TMP"' EXIT

# ── Validate prerequisites ─────────────────────────────────────────────────────
for cmd in ansible-galaxy curl sha256sum; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "ERROR: Required command not found: $cmd"
    exit 1
  fi
done

if [[ ! -f "$REQUIREMENTS_FILE" ]]; then
  echo "ERROR: Requirements file not found: $REQUIREMENTS_FILE"
  exit 1
fi

mkdir -p "$COLLECTIONS_OUTPUT_DIR"

GITLAB_TOKEN="${!GITLAB_TOKEN_ENV:-}"
if [[ -n "$GITLAB_TOKEN" ]]; then
  # Set git credential helper so ansible-galaxy can clone from GitLab
  git config --global credential.helper \
    "!f() { echo \"username=oauth2\"; echo \"password=${GITLAB_TOKEN}\"; }; f"
fi

echo "==> Staging collections from: $REQUIREMENTS_FILE"
echo "    Output directory: $COLLECTIONS_OUTPUT_DIR"
echo ""

# ── Install all collections into temp dir ─────────────────────────────────────
echo "==> Running ansible-galaxy collection install ..."
ansible-galaxy collection install \
  --requirements-file "$REQUIREMENTS_FILE" \
  --collections-path "$INSTALL_TMP/collections" \
  --force \
  --timeout "$ANSIBLE_GALAXY_SERVER_TIMEOUT"

echo ""
echo "==> Packaging collections as tarballs ..."

# ── Re-package each installed collection as a tarball ─────────────────────────
LOCK_ENTRIES=()

find "$INSTALL_TMP/collections/ansible_collections" \
  -mindepth 2 -maxdepth 2 -type d | sort | while read -r coll_dir; do

  namespace="$(basename "$(dirname "$coll_dir")")"
  name="$(basename "$coll_dir")"
  manifest_json="${coll_dir}/MANIFEST.json"

  if [[ ! -f "$manifest_json" ]]; then
    echo "  WARN: No MANIFEST.json in ${namespace}.${name} — skipping"
    continue
  fi

  version="$(python3 -c "import json,sys; print(json.load(open('$manifest_json'))['collection_info']['version'])")"
  tarball_name="${namespace}-${name}-${version}.tar.gz"
  tarball_path="${COLLECTIONS_OUTPUT_DIR}/${tarball_name}"

  echo "  Packaging: ${namespace}.${name} ${version} → ${tarball_name}"

  # ansible-galaxy collection build re-creates the tarball from source
  pushd "$coll_dir" >/dev/null
  ansible-galaxy collection build \
    --output-path "$COLLECTIONS_OUTPUT_DIR" \
    --force \
    2>/dev/null
  popd >/dev/null

  checksum="$(sha256sum "$tarball_path" | awk '{print $1}')"
  echo "    SHA-256: ${checksum}"

  LOCK_ENTRIES+=("  - namespace: \"${namespace}\"")
  LOCK_ENTRIES+=("    name: \"${name}\"")
  LOCK_ENTRIES+=("    version: \"${version}\"")
  LOCK_ENTRIES+=("    tarball: \"${tarball_name}\"")
  LOCK_ENTRIES+=("    sha256: \"${checksum}\"")
  LOCK_ENTRIES+=("")
done

# ── Write lock file ────────────────────────────────────────────────────────────
{
  echo "# Auto-generated by stage_collections.sh — do not edit manually"
  echo "# Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "collections:"
  for entry in "${LOCK_ENTRIES[@]}"; do
    echo "$entry"
  done
} > "$LOCK_FILE"

echo ""
echo "==> Lock file written: $LOCK_FILE"

# ── Push to Artifactory (optional) ────────────────────────────────────────────
if [[ -n "$PUSH_TO_ARTIFACTORY" ]]; then
  ARTIFACTORY_PASSWORD="${!ARTIFACTORY_PASSWORD_ENV:-}"
  if [[ -z "$ARTIFACTORY_PASSWORD" ]]; then
    echo "ERROR: --push-to-artifactory set but ${ARTIFACTORY_PASSWORD_ENV} is empty"
    exit 1
  fi

  echo ""
  echo "==> Pushing collection tarballs to Artifactory: $PUSH_TO_ARTIFACTORY"

  for tarball in "$COLLECTIONS_OUTPUT_DIR"/*.tar.gz; do
    filename="$(basename "$tarball")"
    echo "  Uploading: $filename"
    curl --silent --show-error --fail \
      --user "${ARTIFACTORY_USER}:${ARTIFACTORY_PASSWORD}" \
      --upload-file "$tarball" \
      "${PUSH_TO_ARTIFACTORY}/artifactory/ansible-collections-local/${filename}"
    echo "    ✔ Uploaded"
  done
fi

echo ""
echo "==> Collection staging complete."
echo "    Tarballs: $(ls "$COLLECTIONS_OUTPUT_DIR"/*.tar.gz 2>/dev/null | wc -l) files"
echo "    Lock file: $LOCK_FILE"
echo ""
echo "Next steps:"
echo "  1. Copy ${STAGING_ROOT}/collections/ to your air-gapped admin server"
echo "  2. Update platform-manifest.yaml collection_versions from $LOCK_FILE"
echo "  3. Run: platform-installer deploy --config platform-config.yaml"
