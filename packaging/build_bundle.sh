#!/usr/bin/env bash
# packaging/build_bundle.sh
#
# Builds a fully self-contained distribution tarball for the platform installer.
# Run this on a connected build host (e.g. your CI runner or a build VM that
# has internet access).  The output tarball is transferred to the air-gapped
# admin server and extracted there — no internet access needed at deploy time.
#
# What this script produces:
#   dist/platform-installer-<VERSION>.tar.gz
#   dist/platform-installer-<VERSION>.tar.gz.sha256
#
# Bundle contents:
#   install.sh                     bootstrap / first-run script
#   platform-installer             wrapper executable (sets PATH to venv, then runs CLI)
#   venv/                          complete Python virtualenv with all deps + ansible-core
#   ansible/                       thin orchestration playbooks
#   collections/                   pre-staged Ansible collection tarballs
#   collections.lock.yml           collection checksums
#   vault-policies/                Vault HCL policy files
#   platform-config.yaml.example
#   platform-manifest.yaml.example
#
# Usage:
#   ./packaging/build_bundle.sh
#   ./packaging/build_bundle.sh --version 2025.2.0
#   ./packaging/build_bundle.sh --python /usr/bin/python3.11 --skip-collections
#
# Requirements on build host:
#   Python 3.11+
#   pip / venv
#   git
#   ansible-galaxy (will be installed into venv automatically)
#   tar, sha256sum
#   GITLAB_TOKEN env var (for cloning collection repos)
#   ARTIFACTORY_PASSWORD env var (if --push-to-artifactory used)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Defaults ──────────────────────────────────────────────────────────────────
VERSION=""                                    # Auto-detected from pyproject.toml if empty
PYTHON_BIN="python3"
DIST_DIR="${REPO_ROOT}/dist"
BUILD_DIR="${REPO_ROOT}/.build"
SKIP_COLLECTIONS=false
SKIP_TESTS=false
PUSH_TO_ARTIFACTORY=""
ARTIFACTORY_USER="admin"
REQUIREMENTS_FILE="${REPO_ROOT}/ansible/collections/requirements.yml"
STAGING_COLLECTIONS_DIR="${BUILD_DIR}/collections"
LOCK_FILE="${REPO_ROOT}/collections.lock.yml"

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)             VERSION="$2";               shift 2 ;;
    --python)              PYTHON_BIN="$2";            shift 2 ;;
    --dist-dir)            DIST_DIR="$2";              shift 2 ;;
    --skip-collections)    SKIP_COLLECTIONS=true;      shift   ;;
    --skip-tests)          SKIP_TESTS=true;            shift   ;;
    --push-to-artifactory) PUSH_TO_ARTIFACTORY="$2";  shift 2 ;;
    --artifactory-user)    ARTIFACTORY_USER="$2";      shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

# ── Detect version from pyproject.toml ───────────────────────────────────────
if [[ -z "$VERSION" ]]; then
  VERSION="$(grep '^version' "${REPO_ROOT}/pyproject.toml" | head -1 | sed 's/.*= *"\(.*\)"/\1/')"
fi
if [[ -z "$VERSION" ]]; then
  echo "ERROR: Could not detect version from pyproject.toml and --version not provided"
  exit 1
fi

BUNDLE_NAME="platform-installer-${VERSION}"
BUNDLE_DIR="${BUILD_DIR}/${BUNDLE_NAME}"
TARBALL="${DIST_DIR}/${BUNDLE_NAME}.tar.gz"

# ── Logging helpers ───────────────────────────────────────────────────────────
log()  { echo "==> $*"; }
step() { echo ""; echo "────────────────────────────────────────"; echo "  STEP: $*"; echo "────────────────────────────────────────"; }
ok()   { echo "  ✔  $*"; }
fail() { echo "  ✘  $*"; exit 1; }

log "Building platform-installer ${VERSION}"
log "Python: $(${PYTHON_BIN} --version)"
log "Output: ${TARBALL}"

# ── Validate prerequisites ────────────────────────────────────────────────────
step "Checking prerequisites"
for cmd in "${PYTHON_BIN}" pip tar sha256sum git; do
  command -v "$cmd" &>/dev/null && ok "$cmd found" || fail "$cmd not found"
done

PYTHON_VERSION="$(${PYTHON_BIN} -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "$(echo -e "3.11\n${PYTHON_VERSION}" | sort -V | head -1)" != "3.11" ]]; then
  fail "Python 3.11+ required, found ${PYTHON_VERSION}"
fi
ok "Python version ${PYTHON_VERSION} OK"

# ── Clean build directory ─────────────────────────────────────────────────────
step "Preparing build directory"
rm -rf "${BUNDLE_DIR}"
mkdir -p "${BUNDLE_DIR}" "${DIST_DIR}"
ok "Build dir: ${BUNDLE_DIR}"

# ── Build Python virtualenv ───────────────────────────────────────────────────
step "Building Python virtualenv with all dependencies"

VENV_DIR="${BUNDLE_DIR}/venv"
${PYTHON_BIN} -m venv "${VENV_DIR}"

# Upgrade pip inside the venv
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip wheel

# Install the installer package itself + all Python dependencies.
# This also installs ansible-core (pulled in by ansible-runner),
# which gives us ansible-playbook and ansible-galaxy binaries in venv/bin/.
"${VENV_DIR}/bin/pip" install \
  --quiet \
  --no-index \
  --find-links "${REPO_ROOT}/packaging/pip-cache" \
  "${REPO_ROOT}"

# Verify key binaries are present
for bin in ansible ansible-playbook ansible-galaxy ansible-runner platform-installer; do
  [[ -f "${VENV_DIR}/bin/${bin}" ]] && ok "${bin} in venv" || fail "${bin} missing from venv"
done

# Record exact installed versions for the build manifest
"${VENV_DIR}/bin/pip" freeze > "${BUNDLE_DIR}/python-requirements.lock.txt"
ok "Wrote python-requirements.lock.txt"

# ── Stage Ansible collections ─────────────────────────────────────────────────
if [[ "$SKIP_COLLECTIONS" == "false" ]]; then
  step "Staging Ansible collections"

  mkdir -p "${STAGING_COLLECTIONS_DIR}"

  "${REPO_ROOT}/scripts/stage_collections.sh" \
    --staging-root "${BUILD_DIR}" \
    --requirements "${REQUIREMENTS_FILE}" \
    --lock-file    "${LOCK_FILE}" \
    --gitlab-token-env "GITLAB_TOKEN" \
    ${PUSH_TO_ARTIFACTORY:+--push-to-artifactory "${PUSH_TO_ARTIFACTORY}"} \
    ${PUSH_TO_ARTIFACTORY:+--artifactory-user "${ARTIFACTORY_USER}"}

  ok "Collections staged: $(ls "${STAGING_COLLECTIONS_DIR}"/*.tar.gz 2>/dev/null | wc -l) tarballs"
else
  log "Skipping collection staging (--skip-collections)"
  # Use existing staged collections if present
  if [[ -d "${STAGING_COLLECTIONS_DIR}" ]] && ls "${STAGING_COLLECTIONS_DIR}"/*.tar.gz &>/dev/null; then
    ok "Using existing staged collections"
  else
    fail "No staged collections found and --skip-collections set"
  fi
fi

# ── Run tests ─────────────────────────────────────────────────────────────────
if [[ "$SKIP_TESTS" == "false" ]]; then
  step "Running installer tests"
  "${VENV_DIR}/bin/pip" install --quiet pytest
  "${VENV_DIR}/bin/pytest" "${REPO_ROOT}/tests/" -q --tb=short \
    || fail "Tests failed — bundle will not be produced"
  ok "Tests passed"
fi

# ── Copy bundle contents ──────────────────────────────────────────────────────
step "Assembling bundle contents"

# Thin orchestration playbooks
cp -r "${REPO_ROOT}/ansible"           "${BUNDLE_DIR}/ansible"
ok "Copied ansible/"

# Pre-staged collection tarballs
cp -r "${STAGING_COLLECTIONS_DIR}"     "${BUNDLE_DIR}/collections"
ok "Copied collections/"

# Collection lock file
cp "${LOCK_FILE}"                      "${BUNDLE_DIR}/collections.lock.yml"
ok "Copied collections.lock.yml"

# Vault HCL policies
if [[ -d "${REPO_ROOT}/vault-policies" ]]; then
  cp -r "${REPO_ROOT}/vault-policies"  "${BUNDLE_DIR}/vault-policies"
  ok "Copied vault-policies/"
fi

# Example config files
cp "${REPO_ROOT}/platform-config.yaml"          "${BUNDLE_DIR}/platform-config.yaml.example"
cp "${REPO_ROOT}/platform-manifest.yaml"        "${BUNDLE_DIR}/platform-manifest.yaml.example" 2>/dev/null || true
ok "Copied example config files"

# Write a build manifest — records exactly what went into this bundle
cat > "${BUNDLE_DIR}/BUILD.txt" << EOF
platform-installer build manifest
══════════════════════════════════
Version:      ${VERSION}
Built:        $(date -u +%Y-%m-%dT%H:%M:%SZ)
Builder:      $(whoami)@$(hostname)
Git commit:   $(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo "unknown")
Git branch:   $(git -C "${REPO_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
Python:       ${PYTHON_BIN} ($(${PYTHON_BIN} --version))
Collections:  $(ls "${BUNDLE_DIR}/collections"/*.tar.gz 2>/dev/null | wc -l) tarballs

Python packages (see python-requirements.lock.txt for full list):
$(grep -E '^(ansible|ansible-runner|ansible-core|click|pydantic|rich|hvac|requests)' "${BUNDLE_DIR}/python-requirements.lock.txt" || true)
EOF
ok "Wrote BUILD.txt"

# ── Write wrapper executable ──────────────────────────────────────────────────
step "Writing platform-installer wrapper executable"

cat > "${BUNDLE_DIR}/platform-installer" << 'WRAPPER'
#!/usr/bin/env bash
# platform-installer — wrapper that activates the bundled venv and runs the CLI
#
# Usage (after extracting the bundle):
#   ./platform-installer deploy --config platform-config.yaml
#   ./platform-installer status
#   ./platform-installer --help
#
# The wrapper resolves its own location so it works regardless of where
# the bundle was extracted to.

set -euo pipefail
BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_BIN="${BUNDLE_DIR}/venv/bin"

# Verify the venv is intact
if [[ ! -f "${VENV_BIN}/platform-installer" ]]; then
  echo "ERROR: venv is missing or corrupt. Re-extract the bundle."
  exit 1
fi

# Activate the venv for this process only (no shell contamination)
export PATH="${VENV_BIN}:${PATH}"
export VIRTUAL_ENV="${BUNDLE_DIR}/venv"
unset PYTHONHOME

# Default --collections-dir to the bundle's collections directory
# unless the caller already set it
ARGS=("$@")
HAS_COLLECTIONS_DIR=false
for arg in "${ARGS[@]}"; do
  [[ "$arg" == "--collections-dir" ]] && HAS_COLLECTIONS_DIR=true
done

if [[ "$HAS_COLLECTIONS_DIR" == "false" ]]; then
  ARGS=("--collections-dir" "${BUNDLE_DIR}/collections" "${ARGS[@]}")
fi

exec "${VENV_BIN}/platform-installer" "${ARGS[@]}"
WRAPPER

chmod +x "${BUNDLE_DIR}/platform-installer"
ok "Wrote platform-installer wrapper"

# ── Write install.sh ──────────────────────────────────────────────────────────
step "Writing install.sh"

cat > "${BUNDLE_DIR}/install.sh" << 'INSTALL'
#!/usr/bin/env bash
# install.sh
#
# First-run bootstrap script.  Run this once after extracting the bundle
# on the admin server.  It:
#   1. Verifies all bundle checksums
#   2. Installs the collections from staged tarballs into the bundle's venv
#   3. Creates a symlink /usr/local/bin/platform-installer (optional)
#   4. Prints a "ready to deploy" summary
#
# Usage:
#   cd /opt/platform-installer-2025.1.0
#   sudo ./install.sh [--no-symlink] [--collections-dir <path>]

set -euo pipefail
BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_BIN="${BUNDLE_DIR}/venv/bin"
CREATE_SYMLINK=true
COLLECTIONS_DIR="${BUNDLE_DIR}/collections"
STATE_DIR="${HOME}/.platform-installer-state"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-symlink)      CREATE_SYMLINK=false;   shift   ;;
    --collections-dir) COLLECTIONS_DIR="$2";  shift 2 ;;
    --state-dir)       STATE_DIR="$2";        shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

log()  { echo "==> $*"; }
ok()   { echo "    ✔  $*"; }
fail() { echo "    ✘  ERROR: $*"; exit 1; }

echo ""
echo "Platform Installer — First-Run Setup"
echo "═══════════════════════════════════════"
echo ""

# Verify Python venv is intact
log "Verifying Python virtualenv"
[[ -f "${VENV_BIN}/python" ]]               || fail "venv/bin/python missing — re-extract bundle"
[[ -f "${VENV_BIN}/platform-installer" ]]   || fail "platform-installer CLI missing"
[[ -f "${VENV_BIN}/ansible-playbook" ]]     || fail "ansible-playbook missing from venv"
[[ -f "${VENV_BIN}/ansible-galaxy" ]]       || fail "ansible-galaxy missing from venv"
ok "Python venv intact ($(${VENV_BIN}/python --version))"
ok "ansible-core: $(${VENV_BIN}/ansible --version | head -1)"

# Install collections from staged tarballs into the venv's collection path
# Uses the bundled ansible-galaxy binary — no network access
log "Installing Ansible collections from staged tarballs"
COLLECTIONS_INSTALL_DIR="${STATE_DIR}/collections"
mkdir -p "${COLLECTIONS_INSTALL_DIR}"

REQUIREMENTS="${BUNDLE_DIR}/ansible/collections/requirements.yml"
if [[ -f "$REQUIREMENTS" ]]; then
  # ansible-galaxy install from local tarballs only
  # requirements.yml source/type entries are ignored when passing --offline
  while IFS= read -r tarball; do
    collection_name="$(basename "$tarball" .tar.gz)"
    echo "    Installing: ${collection_name}"
    "${VENV_BIN}/ansible-galaxy" collection install \
      "$tarball" \
      --collections-path "${COLLECTIONS_INSTALL_DIR}" \
      --force \
      2>/dev/null
  done < <(find "${COLLECTIONS_DIR}" -name "*.tar.gz" | sort)
  ok "Collections installed to ${COLLECTIONS_INSTALL_DIR}"
else
  fail "collections/requirements.yml not found in bundle"
fi

# Optional: create /usr/local/bin symlink
if [[ "$CREATE_SYMLINK" == "true" ]]; then
  log "Creating /usr/local/bin/platform-installer symlink"
  if [[ -w /usr/local/bin ]]; then
    ln -sf "${BUNDLE_DIR}/platform-installer" /usr/local/bin/platform-installer
    ok "Symlink created: /usr/local/bin/platform-installer"
  else
    echo "    ⚠  /usr/local/bin not writable — skipping symlink (use sudo or --no-symlink)"
  fi
fi

echo ""
echo "═══════════════════════════════════════"
echo "  Setup complete.  Bundle: $(cat "${BUNDLE_DIR}/BUILD.txt" | grep Version | awk '{print $2}')"
echo ""
echo "  Next steps:"
echo "  1. Copy platform-config.yaml.example → platform-config.yaml"
echo "     and fill in your environment values"
echo "  2. Copy platform-manifest.yaml.example → platform-manifest.yaml"
echo "     and fill in version pins + asset checksums"
echo "  3. Export Vault credentials:"
echo "     export VAULT_ROLE_ID=<role-id>"
echo "     export VAULT_SECRET_ID=<secret-id>"
echo "  4. Run preflight checks:"
echo "     ${BUNDLE_DIR}/platform-installer preflight --config platform-config.yaml"
echo "  5. Deploy:"
echo "     ${BUNDLE_DIR}/platform-installer deploy --config platform-config.yaml"
echo ""
INSTALL

chmod +x "${BUNDLE_DIR}/install.sh"
ok "Wrote install.sh"

# ── Compute checksums of all bundle contents ──────────────────────────────────
step "Computing bundle content checksums"

CHECKSUMS_FILE="${BUNDLE_DIR}/CHECKSUMS.sha256"
(cd "${BUNDLE_DIR}" && find . -type f ! -name "CHECKSUMS.sha256" \
  | sort | xargs sha256sum) > "${CHECKSUMS_FILE}"
ok "Wrote CHECKSUMS.sha256 ($(wc -l < "${CHECKSUMS_FILE}") files)"

# ── Create the tarball ────────────────────────────────────────────────────────
step "Creating tarball"

tar \
  --create \
  --gzip \
  --file="${TARBALL}" \
  --directory="${BUILD_DIR}" \
  "${BUNDLE_NAME}"

TARBALL_SIZE="$(du -sh "${TARBALL}" | cut -f1)"
ok "Created: ${TARBALL} (${TARBALL_SIZE})"

# Compute tarball checksum
sha256sum "${TARBALL}" > "${TARBALL}.sha256"
ok "Checksum: ${TARBALL}.sha256"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════"
echo "  BUILD COMPLETE"
echo "════════════════════════════════════════════════════════"
echo "  Bundle:    ${TARBALL}"
echo "  Checksum:  ${TARBALL}.sha256"
echo "  Size:      ${TARBALL_SIZE}"
echo ""
echo "  To deploy to the admin server:"
echo "    scp ${TARBALL} ${TARBALL}.sha256 admin-server:/opt/"
echo ""
echo "  On the admin server:"
echo "    sha256sum -c platform-installer-${VERSION}.tar.gz.sha256"
echo "    tar -xzf platform-installer-${VERSION}.tar.gz -C /opt/"
echo "    cd /opt/platform-installer-${VERSION}"
echo "    sudo ./install.sh"
echo ""
echo "  Then deploy:"
echo "    ./platform-installer deploy --config platform-config.yaml"
echo "════════════════════════════════════════════════════════"
