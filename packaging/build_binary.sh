#!/usr/bin/env bash
# packaging/build_binary.sh
#
# Compiles the platform-installer CLI into a single, standalone executable
# using PyInstaller. The compiled binary runs natively on the admin host —
# no container, no venv to activate. It only needs to bundle click,
# pydantic, rich, hvac, ansible-runner and similar lightweight dependencies:
# ansible-runner does NOT require ansible-core installed locally when
# running in container-isolation mode (it shells out to
# `podman/docker run <ansible-exec-image> ansible-playbook ...`), so no
# heavy, dynamically-loaded Ansible plugin tree needs to be bundled here.
# See packaging/build_ansible_image.sh for the separate image that runs
# Ansible itself (it travels inside the haul), and
# packaging/build_hauler_image.sh for the Hauler image shipped next to this
# binary.
#
# Output:
#   dist/platform-installer-<VERSION>
#   dist/platform-installer-<VERSION>.sha256
#
# Usage:
#   ./packaging/build_binary.sh
#   ./packaging/build_binary.sh --version 2025.2.0
#   ./packaging/build_binary.sh --python /usr/bin/python3.11 --skip-tests
#
# Requirements on build host:
#   Python 3.11+
#   pip / venv
#   packaging/seed_pip_cache.sh already run (pip-cache/ populated, including
#     the [build] extra — see pyproject.toml)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Defaults ──────────────────────────────────────────────────────────────────
VERSION=""
PYTHON_BIN="python3"
DIST_DIR="${REPO_ROOT}/dist"
BUILD_DIR="${REPO_ROOT}/.build-binary"
SKIP_TESTS=false

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)     VERSION="$2";     shift 2 ;;
    --python)      PYTHON_BIN="$2"; shift 2 ;;
    --dist-dir)    DIST_DIR="$2";   shift 2 ;;
    --skip-tests)  SKIP_TESTS=true; shift   ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

log()  { echo "==> $*"; }
step() { echo ""; echo "────────────────────────────────────────"; echo "  STEP: $*"; echo "────────────────────────────────────────"; }
ok()   { echo "  ✔  $*"; }
fail() { echo "  ✘  $*"; exit 1; }

# ── Detect version from pyproject.toml ───────────────────────────────────────
if [[ -z "$VERSION" ]]; then
  VERSION="$(grep '^version' "${REPO_ROOT}/pyproject.toml" | head -1 | sed 's/.*= *"\(.*\)"/\1/')"
fi
[[ -z "$VERSION" ]] && fail "Could not detect version from pyproject.toml and --version not provided"

BINARY_NAME="platform-installer-${VERSION}"
BINARY_PATH="${DIST_DIR}/${BINARY_NAME}"

log "Building platform-installer ${VERSION}"
log "Python: $(${PYTHON_BIN} --version)"
log "Output: ${BINARY_PATH}"

# ── Validate prerequisites ────────────────────────────────────────────────────
step "Checking prerequisites"
for cmd in "${PYTHON_BIN}" sha256sum; do
  command -v "$cmd" &>/dev/null && ok "$cmd found" || fail "$cmd not found"
done
${PYTHON_BIN} -m pip --version &>/dev/null \
  && ok "${PYTHON_BIN} -m pip available" \
  || fail "${PYTHON_BIN} has no pip module"

PYTHON_VERSION="$(${PYTHON_BIN} -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "$(echo -e "3.11\n${PYTHON_VERSION}" | sort -V | head -1)" != "3.11" ]]; then
  fail "Python 3.11+ required, found ${PYTHON_VERSION}"
fi
ok "Python version ${PYTHON_VERSION} OK"

# ── Prepare build venv ────────────────────────────────────────────────────────
step "Preparing build virtualenv"
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}" "${DIST_DIR}"

VENV_DIR="${BUILD_DIR}/venv"
${PYTHON_BIN} -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip wheel

# Install the installer package + its runtime deps + PyInstaller, all from
# the offline pip cache — no network access during the build.
"${VENV_DIR}/bin/pip" install \
  --quiet \
  --no-index \
  --find-links "${REPO_ROOT}/packaging/pip-cache" \
  "${REPO_ROOT}[build]"

[[ -f "${VENV_DIR}/bin/pyinstaller" ]] && ok "pyinstaller in venv" || fail "pyinstaller missing from venv"
ok "Installed: $(${VENV_DIR}/bin/pip show platform-installer | grep '^Version')"

# ── Run tests ─────────────────────────────────────────────────────────────────
if [[ "$SKIP_TESTS" == "false" ]]; then
  step "Running installer tests"
  "${VENV_DIR}/bin/pip" install --quiet pytest
  "${VENV_DIR}/bin/pytest" "${REPO_ROOT}/tests/" -q --tb=short \
    || fail "Tests failed — binary will not be built"
  ok "Tests passed"
fi

# ── Run PyInstaller ────────────────────────────────────────────────────────────
step "Compiling single-file binary with PyInstaller"

PYINSTALLER_WORKDIR="${BUILD_DIR}/pyinstaller"
mkdir -p "${PYINSTALLER_WORKDIR}"

"${VENV_DIR}/bin/pyinstaller" \
  --onefile \
  --name "${BINARY_NAME}" \
  --distpath "${DIST_DIR}" \
  --workpath "${PYINSTALLER_WORKDIR}/build" \
  --specpath "${PYINSTALLER_WORKDIR}" \
  --add-data "${REPO_ROOT}/ansible:ansible" \
  --collect-submodules textual \
  --console \
  --clean \
  --noconfirm \
  "${REPO_ROOT}/installer/cli.py"

[[ -f "${BINARY_PATH}" ]] || fail "PyInstaller did not produce ${BINARY_PATH}"
chmod +x "${BINARY_PATH}"
ok "Built: ${BINARY_PATH} ($(du -h "${BINARY_PATH}" | cut -f1))"

# ── Smoke test ────────────────────────────────────────────────────────────────
step "Smoke-testing the compiled binary"
"${BINARY_PATH}" --help > /dev/null \
  && ok "platform-installer --help runs standalone" \
  || fail "Compiled binary smoke test FAILED"

# ── Checksum ───────────────────────────────────────────────────────────────────
sha256sum "${BINARY_PATH}" | sed "s|${DIST_DIR}/||" > "${BINARY_PATH}.sha256"
ok "Checksum: ${BINARY_PATH}.sha256"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════"
echo "  BUILD COMPLETE"
echo "════════════════════════════════════════════════════════"
echo "  Binary:    ${BINARY_PATH}"
echo "  Checksum:  ${BINARY_PATH}.sha256"
echo "  Size:      $(du -h "${BINARY_PATH}" | cut -f1)"
echo ""
echo "  This binary is the entire deploy-host artifact — no venv, no wrapper"
echo "  script. Pair it with the Hauler image tarball built by"
echo "  packaging/build_hauler_image.sh (same dist/ directory, same version)"
echo "  and transfer both, plus the haul, to the admin server:"
echo ""
echo "    scp ${BINARY_PATH} ${BINARY_PATH}.sha256 \\"
echo "        ${DIST_DIR}/platform-hauler-${VERSION}-container.tar.gz* \\"
echo "        admin-server:/opt/platform-installer/"
echo ""
echo "  On the admin server:"
echo "    sha256sum -c platform-installer-${VERSION}.sha256"
echo "    chmod +x platform-installer-${VERSION}"
echo "    ./platform-installer-${VERSION} preflight --config platform-config.yaml --haul-path haul.tar.zst"
echo "════════════════════════════════════════════════════════"
