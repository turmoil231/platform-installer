"""
tests/unit/config/test_loader.py

Unit tests for ConfigLoader.

Covered: generate_ansible_vars() for spoke clusters — every spoke reaches
the phase and the playbooks, merged over the defaults without leaking
between spokes.

TODO: Implement tests for:
  - load() with valid config + manifest → no exception
  - load() with mismatched manifest version → ConfigValidationError
  - load() with missing config file → FileNotFoundError
  - load() with missing manifest file → FileNotFoundError
  - load() with invalid YAML → ConfigValidationError
  - cross-section validation: server_ref not in inventory.compute → error
  - cross-section validation: same server claimed by esxi and baremetal cluster → error
  - generate_ansible_inventory() produces valid YAML with expected groups
"""
import shutil
from pathlib import Path

import pytest
import yaml

from installer.config.loader import ConfigLoader
from installer.phases.base import SpokeClustersPhase
from installer.runner.ansible import AnsibleRunner

REPO = Path(__file__).resolve().parents[3]


def spoke(name: str, **settings) -> dict:
    return {
        "name": name, "base_domain": "example.internal", "cluster_type": "compact",
        "machine_network": {"cidr": "10.0.31.0/24"},
        "platform": {"type": "vsphere", "vsphere": {
            "vcenter": "vcenter.mgmt.example.internal", "datacenter": "DC-Primary",
            "cluster": "Compute-Cluster-01", "datastore": "pure-vmfs-mgmt",
            "network": "PG-OCP-Spoke-Trunk", "folder": f"Platform/{name}",
            "credentials_secret": "vault:secret/vmware/vcenter-creds",
        }},
        "nodes": [],
        **settings,
    }


@pytest.fixture
def make_loader(tmp_path):
    """The example config with spoke_clusters.clusters replaced by `clusters`."""
    def make(*clusters: dict) -> ConfigLoader:
        config = yaml.safe_load((REPO / "platform-config.yaml.example").read_text())
        config["spoke_clusters"]["clusters"] = list(clusters)
        (tmp_path / "platform-config.yaml").write_text(yaml.safe_dump(config))
        shutil.copy(REPO / "platform-manifest.yaml.example", tmp_path / "platform-manifest.yaml")
        return ConfigLoader(tmp_path / "platform-config.yaml").load()
    return make


def generated_extra_vars(loader: ConfigLoader, tmp_path: Path) -> dict:
    """What every playbook receives: the generated vars, merged as the runner does."""
    pdd = tmp_path / "pdd"
    loader.generate_ansible_vars(pdd / "vars")
    runner = AnsibleRunner(
        ansible_dir=REPO / "ansible", private_data_dir=pdd,
        container_image="unused", vault_addr="unused",
    )
    return runner._load_extra_vars()


def test_every_spoke_reaches_the_playbooks(make_loader, tmp_path):
    extra = generated_extra_vars(make_loader(spoke("spoke-01"), spoke("spoke-02")), tmp_path)

    clusters = extra["platform_spoke_clusters"]["clusters"]
    assert [c["name"] for c in clusters] == ["spoke-01", "spoke-02"]
    assert extra["platform_spoke_clusters"]["defaults"]["fips"] is False


def test_spoke_phase_plans_every_spoke(make_loader, tmp_path):
    extra = generated_extra_vars(make_loader(spoke("spoke-01"), spoke("spoke-02")), tmp_path)
    phase = SpokeClustersPhase(runner=None, store=None, config_vars=extra)

    wait = phase.planned_playbooks()[-1]
    assert wait.extra_vars == {"spoke_names": ["spoke-01", "spoke-02"]}


def test_no_spokes_plans_nothing(make_loader, tmp_path):
    extra = generated_extra_vars(make_loader(), tmp_path)
    assert extra["platform_spoke_clusters"]["clusters"] == []
    assert SpokeClustersPhase(runner=None, store=None, config_vars=extra).planned_playbooks() == []


def test_spokes_inherit_defaults_and_override_without_leaking(make_loader, tmp_path):
    loader = make_loader(
        spoke("spoke-01", cluster_network={"cidr": "10.200.0.0/14"}, dns_servers=["10.9.9.9"]),
        spoke("spoke-02"),
    )
    defaults_before = yaml.safe_dump(loader._raw_config["spoke_clusters"]["defaults"])
    s1, s2 = generated_extra_vars(loader, tmp_path)["platform_spoke_clusters"]["clusters"]

    # Dicts merge key by key; lists replace rather than append.
    assert s1["cluster_network"] == {"cidr": "10.200.0.0/14", "host_prefix": 23}
    assert s1["dns_servers"] == ["10.9.9.9"]
    # spoke-02 gets the untouched defaults, and so does the next generation.
    assert s2["cluster_network"] == {"cidr": "10.132.0.0/14", "host_prefix": 23}
    assert s2["dns_servers"] == ["10.0.10.20", "10.0.10.21"]
    assert s2["storage"]["pure_csi"]["arrays"] == ["flasharray-01"]
    assert yaml.safe_dump(loader._raw_config["spoke_clusters"]["defaults"]) == defaults_before


def test_regeneration_removes_stale_var_files(make_loader, tmp_path):
    vars_dir = tmp_path / "pdd" / "vars"
    (vars_dir / "spokes").mkdir(parents=True)
    (vars_dir / "spokes" / "old-spoke.yml").write_text(yaml.safe_dump({"platform_spoke": {"name": "old"}}))
    (vars_dir / "spoke_defaults.yml").write_text(yaml.safe_dump({"platform_spoke_defaults": {}}))

    extra = generated_extra_vars(make_loader(), tmp_path)

    assert "platform_spoke" not in extra
    assert "platform_spoke_defaults" not in extra
