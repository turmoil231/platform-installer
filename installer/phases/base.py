"""
installer/phases/base.py  (and all phase implementations)

Each phase is a class that:
  1. Declares its name and dependencies
  2. Implements planned_playbooks() — the playbooks run() will execute, in
     order, decided from config alone. The CLI builds the progress UI's
     step list from it before anything runs; run() iterates over the same
     list, so the two can't drift.
  3. Optionally implements health_check() — polled after run() succeeds

The Orchestrator (see cli.py) resolves the dependency graph, skips already-
complete phases, and calls phases in order.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from installer.runner.ansible import AnsibleRunner, PlaybookResult
    from installer.state.store import StateStore


# ── Base Phase ─────────────────────────────────────────────────────────────────

@dataclass
class PhaseResult:
    success:  bool
    rc:       int = 0
    message:  str = ""


@dataclass
class PlannedPlaybook:
    playbook:        str
    failure_message: str
    extra_vars:      dict[str, Any] | None = None


class Phase(ABC):
    #: Unique phase name — must match state store initialization list
    name: str

    #: Phase names that must be complete before this phase can run
    depends_on: list[str] = []

    def __init__(
        self,
        runner:      "AnsibleRunner",
        store:       "StateStore",
        config_vars: dict[str, Any],
    ):
        self.runner      = runner
        self.store       = store
        self.config_vars = config_vars

    #: PhaseResult message when every planned playbook succeeds
    success_message: str = ""

    #: PhaseResult message when config leaves nothing to run
    nothing_to_do_message: str = "Nothing to do — skipping"

    @abstractmethod
    def planned_playbooks(self) -> list[PlannedPlaybook]:
        """The playbooks run() executes, in order. Must depend on config only."""
        ...

    def run(self) -> PhaseResult:
        """Run each planned playbook in order, stopping at the first failure."""
        planned = self.planned_playbooks()
        if not planned:
            return PhaseResult(success=True, message=self.nothing_to_do_message)
        for pb in planned:
            result = self._run_playbook(pb.playbook, extra_vars=pb.extra_vars)
            if not result.success:
                return PhaseResult(success=False, rc=result.rc, message=pb.failure_message)
        return PhaseResult(success=True, message=self.success_message)

    def step_id(self, playbook: str) -> str:
        """Progress-UI step id for one of this phase's playbooks."""
        return f"{self.name}.{Path(playbook).stem}"

    @property
    def health_check_step_id(self) -> str:
        return f"{self.name}.health_check"

    @property
    def has_health_check(self) -> bool:
        return type(self).health_check is not Phase.health_check

    def health_check(self) -> bool:
        """
        Optional post-run health check.  Return True if healthy.
        Default implementation always returns True.
        Override in phases that can verify liveness after deployment.
        """
        return True

    def _run_playbook(
        self,
        playbook:   str,
        extra_vars: dict[str, Any] | None = None,
        tags:       list[str] | None = None,
        limit:      str | None = None,
    ) -> "PlaybookResult":
        """Convenience wrapper — reports under this phase's step id, logs to state store on failure."""
        result = self.runner.run_playbook(
            playbook=playbook, extra_vars=extra_vars, tags=tags, limit=limit,
            step_id=self.step_id(playbook),
        )
        if not result.success:
            self.store.log_event(
                self.name,
                f"Playbook {playbook} failed: rc={result.rc} status={result.status}",
                level="error",
            )
        return result

    def _poll_url(
        self,
        url:               str,
        expected_status:   int = 200,
        timeout_seconds:   int = 300,
        interval_seconds:  int = 15,
        verify_tls:        bool = True,
    ) -> bool:
        """Poll a URL until it returns expected_status or timeout expires."""
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                resp = requests.get(url, verify=verify_tls, timeout=10)
                if resp.status_code == expected_status:
                    return True
            except requests.RequestException:
                pass
            time.sleep(interval_seconds)
        return False


# ── Phase: Preflight ───────────────────────────────────────────────────────────

class PreflightPhase(Phase):
    name            = "preflight"
    depends_on      = []
    success_message = "All preflight checks passed"

    def planned_playbooks(self) -> list[PlannedPlaybook]:
        return [
            PlannedPlaybook("preflight.yml", "Preflight checks failed — see output above"),
        ]


# ── Phase: VMware (ESXi + vCenter) ────────────────────────────────────────────

class VMwarePhase(Phase):
    name            = "vmware"
    depends_on      = ["preflight"]
    success_message = "vSphere stack deployed and configured"

    def planned_playbooks(self) -> list[PlannedPlaybook]:
        return [
            # ESXi installation is done serially per host via iDRAC/virtual media
            PlannedPlaybook("vmware/install_esxi.yml",       "ESXi installation failed"),
            PlannedPlaybook("vmware/deploy_vcenter.yml",     "vCenter deployment failed"),
            PlannedPlaybook("vmware/configure_vcenter.yml",  "vCenter configuration failed"),
            PlannedPlaybook("vmware/configure_storage.yml",  "Storage configuration failed"),
        ]

    def health_check(self) -> bool:
        vcenter_url = (
            f"https://{self.config_vars.get('platform_vmware', {}).get('vcenter', {}).get('hostname', '')}"
        )
        if not vcenter_url or vcenter_url == "https://":
            return True  # No URL to check — skip
        return self._poll_url(
            f"{vcenter_url}/ui/",
            expected_status=200,
            timeout_seconds=120,
            verify_tls=False,
        )


# ── Phase: Bootstrap ───────────────────────────────────────────────────────────

class BootstrapPhase(Phase):
    name            = "bootstrap"
    depends_on      = ["vmware"]
    success_message = "Bootstrap VM running with Vault + Artifactory"

    def planned_playbooks(self) -> list[PlannedPlaybook]:
        return [
            PlannedPlaybook("bootstrap/deploy_bootstrap_vm.yml", "Bootstrap VM deploy failed"),
            PlannedPlaybook("bootstrap/start_vault.yml",         "Bootstrap Vault start failed"),
            PlannedPlaybook("bootstrap/start_artifactory.yml",   "Bootstrap Artifactory start failed"),
            PlannedPlaybook("bootstrap/seed_vault.yml",          "Vault seeding failed"),
            PlannedPlaybook("bootstrap/seed_artifactory.yml",    "Artifactory seeding failed"),
        ]

    def health_check(self) -> bool:
        bootstrap_ip = self.config_vars.get("platform_bootstrap", {}).get("vm", {}).get("ip", "")
        if not bootstrap_ip:
            return True
        vault_ok = self._poll_url(
            f"https://{bootstrap_ip}:8200/v1/sys/health",
            expected_status=200,
            timeout_seconds=120,
            verify_tls=False,
        )
        artifactory_ok = self._poll_url(
            f"https://{bootstrap_ip}:8082/artifactory/api/system/ping",
            expected_status=200,
            timeout_seconds=120,
            verify_tls=False,
        )
        return vault_ok and artifactory_ok


# ── Phase: Management Services ────────────────────────────────────────────────

class ManagementServicesPhase(Phase):
    name            = "management_services"
    depends_on      = ["bootstrap"]
    success_message = "IDM and Kea DHCP deployed"

    def planned_playbooks(self) -> list[PlannedPlaybook]:
        return [
            PlannedPlaybook("management/deploy_idm.yml",    "IDM deployment failed"),
            PlannedPlaybook("management/configure_idm.yml", "IDM configuration failed"),
            PlannedPlaybook("management/deploy_kea.yml",    "Kea DHCP deployment failed"),
        ]


# ── Phase: Mirror Registry ────────────────────────────────────────────────────

class MirrorRegistryPhase(Phase):
    name            = "mirror_registry"
    depends_on      = ["management_services"]
    success_message = "Mirror registry populated"

    def planned_playbooks(self) -> list[PlannedPlaybook]:
        return [
            PlannedPlaybook("registry/deploy_registry_vm.yml", "Registry VM deploy failed"),
            PlannedPlaybook("registry/push_images.yml",        "Image push failed"),
            PlannedPlaybook("registry/push_olm_catalogs.yml",  "OLM catalog push failed"),
        ]


# ── Phase: Hub Cluster ────────────────────────────────────────────────────────

class HubClusterPhase(Phase):
    name            = "hub_cluster"
    depends_on      = ["mirror_registry"]
    success_message = "Hub cluster installed and configured"

    def planned_playbooks(self) -> list[PlannedPlaybook]:
        return [
            # Generate install-config.yaml from config vars
            PlannedPlaybook("hub/generate_install_config.yml", "install-config generation failed"),
            # Run openshift-install (wrapped in Ansible)
            PlannedPlaybook("hub/install_cluster.yml",         "Hub cluster install failed"),
            # Configure cluster-level Day 1 settings
            PlannedPlaybook("hub/configure_cluster.yml",       "Hub cluster configuration failed"),
            # Pure CSI driver — needed before any PVC-backed services
            PlannedPlaybook("hub/install_pure_csi.yml",        "Pure CSI install failed"),
        ]

    def health_check(self) -> bool:
        hub_config = self.config_vars.get("platform_hub_cluster", {})
        api_vip = hub_config.get("api_vip", "")
        name    = hub_config.get("name", "hub")
        domain  = hub_config.get("base_domain", "")
        if api_vip:
            return self._poll_url(
                f"https://api.{name}.{domain}:6443/healthz",
                expected_status=200,
                timeout_seconds=600,
                verify_tls=False,
            )
        return True


# ── Phase: Hub Services ───────────────────────────────────────────────────────

class HubServicesPhase(Phase):
    name            = "hub_services"
    depends_on      = ["hub_cluster"]
    success_message = "All enabled hub services deployed"

    # Services deployed in this fixed order (dependency order within the phase)
    _SERVICE_ORDER = [
        "local_storage",
        "odf",
        "vault",
        "artifactory",
        "gitops",
        "gitlab",
        "service_mesh",
        "acs",
        "anchore",
        "elk",
        "aap",
        "jira",
        "confluence",
        "observability",
        "acm",
    ]

    def planned_playbooks(self) -> list[PlannedPlaybook]:
        services = self.config_vars.get("platform_hub_services", {})

        def enabled(svc_name: str) -> bool:
            return bool(services.get(svc_name, {}).get("enabled", False))

        # First: install all OLM operators for enabled services
        planned = [PlannedPlaybook("hub_services/install_operators.yml", "Operator installation failed")]

        # Then deploy each enabled service in order
        planned += [
            PlannedPlaybook(f"hub_services/{svc_name}.yml", f"hub_services/{svc_name} deployment failed")
            for svc_name in self._SERVICE_ORDER
            if enabled(svc_name)
        ]

        # Migrate Vault + Artifactory from bootstrap → hub
        if enabled("vault"):
            planned.append(PlannedPlaybook("hub_services/migrate_vault.yml", "Vault migration failed"))
        if enabled("artifactory"):
            planned.append(PlannedPlaybook("hub_services/migrate_artifactory.yml", "Artifactory migration failed"))

        return planned


# ── Phase: Spoke Clusters ─────────────────────────────────────────────────────

class SpokeClustersPhase(Phase):
    name                  = "spoke_clusters"
    depends_on            = ["hub_services"]
    nothing_to_do_message = "No spoke clusters defined — skipping"

    def _spokes(self) -> list[dict[str, Any]]:
        return self.config_vars.get("platform_spoke_clusters", {}).get("clusters", [])

    @property
    def success_message(self) -> str:  # type: ignore[override]
        return f"All {len(self._spokes())} spoke cluster(s) installed"

    def planned_playbooks(self) -> list[PlannedPlaybook]:
        spokes = self._spokes()
        if not spokes:
            return []
        return [
            # Generate ZTP SiteConfig + PolicyGenTemplates for all spokes
            PlannedPlaybook("spokes/generate_ztp_manifests.yml", "ZTP manifest generation failed"),
            # Push generated manifests to GitLab
            PlannedPlaybook("spokes/push_to_gitlab.yml",         "GitLab push failed"),
            # ArgoCD picks up and applies; TALM drives cluster installation
            # We poll until all clusters reach Installed state
            PlannedPlaybook(
                "spokes/wait_for_clusters.yml",
                "Spoke cluster install wait failed",
                extra_vars={"spoke_names": [s["name"] for s in spokes]},
            ),
        ]


# ── Phase: VDI ────────────────────────────────────────────────────────────────

class VDIPhase(Phase):
    name                  = "vdi_services"
    depends_on            = ["hub_services"]
    nothing_to_do_message = "VDI services disabled — skipping"

    def _platform(self) -> str:
        return self.config_vars.get("platform_vdi", {}).get("platform", "vmware")

    @property
    def success_message(self) -> str:  # type: ignore[override]
        return f"VDI deployed on {self._platform()}"

    def planned_playbooks(self) -> list[PlannedPlaybook]:
        if not self.config_vars.get("platform_vdi", {}).get("enabled", False):
            return []
        playbook = (
            "vdi/deploy_vmware_vdi.yml"
            if self._platform() == "vmware"
            else "vdi/deploy_ocpvirt_vdi.yml"
        )
        return [PlannedPlaybook(playbook, "VDI deployment failed")]


# ── Phase: Validation ─────────────────────────────────────────────────────────

class ValidationPhase(Phase):
    name            = "validation"
    depends_on      = ["spoke_clusters", "vdi_services"]
    success_message = "Platform validation passed"

    def planned_playbooks(self) -> list[PlannedPlaybook]:
        return [
            PlannedPlaybook(
                "validation/validate_platform.yml",
                "Platform validation failed — see output for details",
            ),
        ]


# ── Phase: Bootstrap Teardown ─────────────────────────────────────────────────

class BootstrapTeardownPhase(Phase):
    name                  = "bootstrap_teardown"
    depends_on            = ["validation"]
    success_message       = "Bootstrap VM decommissioned"
    nothing_to_do_message = "Bootstrap teardown disabled — skipping"

    def planned_playbooks(self) -> list[PlannedPlaybook]:
        teardown_config = self.config_vars.get("platform_bootstrap", {}).get("teardown", {})
        if not teardown_config.get("enabled", True):
            return []
        return [PlannedPlaybook("bootstrap/teardown_bootstrap.yml", "Bootstrap teardown failed")]


# ── Phase Registry ────────────────────────────────────────────────────────────

#: Ordered list of all phases.  The Orchestrator resolves this list
#: respecting depends_on before executing.
ALL_PHASES: list[type[Phase]] = [
    PreflightPhase,
    VMwarePhase,
    BootstrapPhase,
    ManagementServicesPhase,
    MirrorRegistryPhase,
    HubClusterPhase,
    HubServicesPhase,
    SpokeClustersPhase,
    VDIPhase,
    ValidationPhase,
    BootstrapTeardownPhase,
]

PHASE_NAMES: list[str] = [p.name for p in ALL_PHASES]
