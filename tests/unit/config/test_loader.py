"""
tests/unit/config/test_loader.py

Unit tests for ConfigLoader.

TODO: Implement tests for:
  - load() with valid config + manifest → no exception
  - load() with mismatched manifest version → ConfigValidationError
  - load() with missing config file → FileNotFoundError
  - load() with missing manifest file → FileNotFoundError
  - load() with invalid YAML → ConfigValidationError
  - cross-section validation: server_ref not in inventory.compute → error
  - cross-section validation: same server claimed by esxi and baremetal cluster → error
  - generate_ansible_vars() writes expected files to output dir
  - generate_ansible_inventory() produces valid YAML with expected groups
"""
import pytest
from pathlib import Path


# Placeholder — implement with pytest fixtures
def test_placeholder():
    """Remove this once real tests are written."""
    assert True
