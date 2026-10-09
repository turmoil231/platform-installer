"""
installer/config/loader.py

Loads platform-config.yaml + platform-manifest.yaml, validates both,
and produces the structures the installer and Ansible runner need.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import yaml
from deepmerge import Merger
from pydantic import ValidationError

from .models import PlatformConfig

#: Merges a spoke's settings over spoke_clusters.defaults. Dicts merge key by
#: key; anything else, lists included, is replaced — `dns_servers: [...]` on a
#: spoke means exactly those servers, not the defaults plus those.
SPOKE_MERGER = Merger([(dict, ["merge"])], ["override"], ["override"])


class ConfigLoader:
    """Load, validate, and expose the merged platform configuration."""

    def __init__(self, config_path: str | Path, manifest_path: str | Path | None = None):
        self.config_path   = Path(config_path).resolve()
        self.manifest_path = (
            Path(manifest_path).resolve()
            if manifest_path
            else self.config_path.parent / "platform-manifest.yaml"
        )
        self._raw_config:   dict[str, Any] = {}
        self._raw_manifest: dict[str, Any] = {}
        self.config:        PlatformConfig | None = None
        self.manifest:      dict[str, Any] = {}

    # ── Public API ─────────────────────────────────────────────────────────────

    def load(self) -> "ConfigLoader":
        """Load and validate both files.  Raises on any error."""
        self._raw_config   = self._read_yaml(self.config_path)
        self._raw_manifest = self._read_yaml(self.manifest_path)
        self._validate_manifest_version()
        self.manifest = self._raw_manifest
        try:
            self.config = PlatformConfig.model_validate(self._raw_config)
        except ValidationError as exc:
            raise ConfigValidationError(
                f"platform-config.yaml failed validation:\n{exc}"
            ) from exc
        return self

    def generate_ansible_vars(self, output_dir: str | Path) -> None:
        """
        Write Ansible group_vars files from the validated config.
        Called once before any phase runs.  Ansible playbooks read these
        files instead of parsing the config themselves.

        Output structure:
          output_dir/
            all.yml            ← global, manifest, network
            vmware.yml         ← vmware + bootstrap + mirror_registry
            management.yml     ← management_services
            hub.yml            ← hub_cluster + hub_services + storage
            spoke_clusters.yml ← spoke_clusters, each cluster merged over the defaults
            vdi.yml            ← vdi_services

        Every file is merged into one set of extra-vars (see
        AnsibleRunner._load_extra_vars), so variable names must be unique
        across files. Previously generated files are removed first, so
        nothing stale (e.g. a since-deleted spoke) carries over.
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        for stale in out.rglob("*.yml"):
            stale.unlink()

        raw = self._raw_config
        manifest = self._raw_manifest

        self._write_yaml(out / "all.yml", {
            "platform_global":   raw.get("global", {}),
            "platform_manifest": manifest,
            "platform_inventory_network": raw.get("inventory", {}).get("network", {}),
            "platform_inventory_compute": raw.get("inventory", {}).get("compute", {}),
            "platform_inventory_storage": raw.get("inventory", {}).get("storage", {}),
            "platform_preflight": raw.get("preflight", {}),
        })

        self._write_yaml(out / "vmware.yml", {
            "platform_vmware":          raw.get("vmware", {}),
            "platform_bootstrap":       raw.get("bootstrap", {}),
            "platform_mirror_registry": raw.get("mirror_registry", {}),
        })

        self._write_yaml(out / "management.yml", {
            "platform_management_services": raw.get("management_services", {}),
        })

        self._write_yaml(out / "hub.yml", {
            "platform_hub_cluster":  raw.get("hub_cluster", {}),
            "platform_hub_services": raw.get("hub_services", {}),
        })

        spokes   = raw.get("spoke_clusters", {})
        defaults = spokes.get("defaults", {})
        self._write_yaml(out / "spoke_clusters.yml", {
            "platform_spoke_clusters": {
                "defaults": defaults,
                "clusters": [
                    # Deep copies: the merger works in place, and every spoke
                    # starts from the same defaults.
                    SPOKE_MERGER.merge(copy.deepcopy(defaults), copy.deepcopy(spoke))
                    for spoke in spokes.get("clusters", [])
                ],
            },
        })

        self._write_yaml(out / "vdi.yml", {
            "platform_vdi": raw.get("vdi_services", {}),
        })

    def generate_ansible_inventory(self, output_path: str | Path) -> None:
        """
        Write a static Ansible inventory INI/YAML from inventory.compute.
        Groups:
          all              ← every host
          esxi_hosts       ← servers claimed by vmware.esxi.hosts
          hub_nodes        ← servers claimed by hub_cluster.nodes (baremetal)
          spoke_<name>     ← servers claimed by each spoke (baremetal)
          management_vms   ← IDM, Kea, registry (management VMs, added post-ESXi)
        """
        raw = self._raw_config
        compute = raw.get("inventory", {}).get("compute", {})

        esxi_server_names = {
            h.get("server") for h in raw.get("vmware", {}).get("esxi", {}).get("hosts", [])
        }

        hub_server_names: set[str] = set()
        for group in raw.get("hub_cluster", {}).get("nodes", {}).values():
            for node in group.get("nodes", []):
                ref = node.get("server_ref", "")
                if ref:
                    hub_server_names.add(ref)

        spoke_server_map: dict[str, set[str]] = {}
        for spoke in raw.get("spoke_clusters", {}).get("clusters", []):
            names: set[str] = set()
            for node in spoke.get("nodes", []):
                ref = node.get("server_ref", "")
                if ref:
                    names.add(ref)
            spoke_server_map[spoke["name"]] = names

        inventory: dict[str, Any] = {
            "all": {
                "children": {
                    "esxi_hosts":    {"hosts": {}},
                    "hub_nodes":     {"hosts": {}},
                    "management_vms": {"hosts": {}},
                },
            }
        }

        for server_name, server in compute.items():
            bmc = server.get("bmc", {})
            host_vars = {
                "ansible_host":                bmc.get("address", ""),
                "bmc_protocol":                bmc.get("protocol", "redfish"),
                "bmc_port":                    bmc.get("port", 443),
                "bmc_credentials_secret":      bmc.get("credentials_secret", ""),
                "bmc_disable_cert_verify":     bmc.get("disable_certificate_verification", True),
                "server_make":                 server.get("make", ""),
                "server_model":                server.get("model", ""),
                "server_nics":                 server.get("nics", []),
                "server_disks":                server.get("disks", []),
                "server_memory_gb":            server.get("memory_gb", 0),
                "server_cpu_sockets":          server.get("cpu_sockets", 1),
                "server_cpu_cores_per_socket": server.get("cpu_cores_per_socket", 0),
            }

            if server_name in esxi_server_names:
                inventory["all"]["children"]["esxi_hosts"]["hosts"][server_name] = host_vars
            elif server_name in hub_server_names:
                inventory["all"]["children"]["hub_nodes"]["hosts"][server_name] = host_vars

        for spoke_name, server_names in spoke_server_map.items():
            group_key = f"spoke_{spoke_name.replace('-', '_')}"
            inventory["all"]["children"][group_key] = {"hosts": {}}
            for sn in server_names:
                if sn in compute:
                    inventory["all"]["children"][group_key]["hosts"][sn] = {
                        "ansible_host": compute[sn]["bmc"]["address"]
                    }

        # Management VMs — known IPs from management_services
        mgmt = raw.get("management_services", {})
        for svc_name, svc in mgmt.items():
            for role, vm in svc.get("vms", {}).items() if isinstance(svc, dict) else []:
                if isinstance(vm, dict) and "ip" in vm:
                    inventory["all"]["children"]["management_vms"]["hosts"][vm["hostname"]] = {
                        "ansible_host": vm["ip"],
                        "ansible_user": "ansible",
                        "ansible_become": True,
                    }

        self._write_yaml(Path(output_path), inventory)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _validate_manifest_version(self) -> None:
        config_version   = self._raw_config.get("platform_manifest", {}).get("version", "")
        manifest_version = self._raw_manifest.get("manifest_version", "")
        if config_version != manifest_version:
            raise ConfigValidationError(
                f"Manifest version mismatch: platform-config.yaml expects "
                f"{config_version!r} but platform-manifest.yaml declares {manifest_version!r}.\n"
                f"Update one file so both agree before running the installer."
            )

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"Required file not found: {path}")
        with path.open() as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            raise ConfigValidationError(f"{path} did not parse as a YAML mapping")
        return data

    @staticmethod
    def _write_yaml(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as fh:
            yaml.dump(data, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)


class ConfigValidationError(RuntimeError):
    """Raised when platform-config.yaml or platform-manifest.yaml fail validation."""
