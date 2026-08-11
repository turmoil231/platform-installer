"""
tests/unit/runner/test_collection_manager.py

Unit tests for CollectionManager.

TODO: Implement tests for:
  - validate_staged_assets() returns empty list when all tarballs present
  - validate_staged_assets() returns errors for missing tarballs
  - validate_staged_assets() returns errors for checksum mismatches
  - _find_tarball() finds exact version match
  - _find_tarball() falls back to glob when exact version not found
  - _find_tarball() returns None when no tarball exists
  - install() runs ansible-galaxy for each tarball (mock subprocess)
  - install() writes .install_complete marker
  - install() skips if marker exists and force=False
  - install() re-installs if force=True
"""
import pytest
import tempfile
from pathlib import Path


def test_placeholder():
    """Remove this once real tests are written."""
    assert True
