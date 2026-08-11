"""
tests/unit/config/test_models.py

Unit tests for Pydantic config models.

TODO: Implement tests for:
  - PlatformConfig validates a minimal valid config dict
  - VaultSecretRef rejects strings not matching vault:secret/... pattern
  - ClusterPlatform rejects vsphere type with missing vsphere block
  - ClusterPlatform rejects baremetal type with missing baremetal block
  - HubServicesConfig.enabled_services() returns only enabled services
"""
import pytest


def test_placeholder():
    """Remove this once real tests are written."""
    assert True
