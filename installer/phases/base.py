"""
installer/phases/base.py  (and all phase implementations)

Each phase is a class that:
  1. Declares its name and dependencies
  2. Implements run() — which calls runner.run_playbook() one or more times
  3. Optionally implements health_check() — polled after run() succeeds

The Orchestrator (see cli.py) resolves the dependency graph, skips already-
complete phases, and calls phases in order.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import requests
from rich.console import Console

if TYPE_CHECKING:
    from installer.runner.ansible import AnsibleRunner, PlaybookResult
    from installer.state.store import StateStore

console = Console()


# ── Base Phase ─────────────────────────────────────────────────────────────────

@dataclass
class PhaseResult:
    success:  bool
    rc:       int = 0
    message:  str = ""


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

    @abstractmethod
    def run(self) -> PhaseResult:
        """Execute this phase.  Must return a PhaseResult."""
        ...

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
        """Convenience wrapper — logs to state store on failure."""
        result = self.runner.run_playbook(
            playbook=playbook, extra_vars=extra_vars, tags=tags, limit=limit
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
    name       = "preflight"
    depends_on = []

    def run(self) -> PhaseResult:
        result = self._run_playbook("preflight.yml")
        if not result.success:
            return PhaseResult(
                success=False, rc=result.rc,
                message="Preflight checks failed — see output above"
            )
        return PhaseResult(success=True, message="All preflight checks passed")


# ── Phase: VMware (ESXi + vCenter) ────────────────────────────────────────────

class VMwarePhase(Phase):
    name       = "vmware"
    depends_on = ["preflight"]

    def run(self) -> PhaseResult:
        # ESXi installation is done serially per host via iDRAC/virtual media
        result = self._run_playbook("vmware/install_esxi.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="ESXi installation failed")

        result = self._run_playbook("vmware/deploy_vcenter.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="vCenter deployment failed")

        result = self._run_playbook("vmware/configure_vcenter.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="vCenter configuration failed")

        result = self._run_playbook("vmware/configure_storage.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="Storage configuration failed")

        return PhaseResult(success=True, message="vSphere stack deployed and configured")

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
    name       = "bootstrap"
    depends_on = ["vmware"]

    def run(self) -> PhaseResult:
        result = self._run_playbook("bootstrap/deploy_bootstrap_vm.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="Bootstrap VM deploy failed")

        result = self._run_playbook("bootstrap/start_vault.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="Bootstrap Vault start failed")

        result = self._run_playbook("bootstrap/start_artifactory.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="Bootstrap Artifactory start failed")

        result = self._run_playbook("bootstrap/seed_vault.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="Vault seeding failed")

        result = self._run_playbook("bootstrap/seed_artifactory.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="Artifactory seeding failed")

        return PhaseResult(success=True, message="Bootstrap VM running with Vault + Artifactory")

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
    name       = "management_services"
    depends_on = ["bootstrap"]

    def run(self) -> PhaseResult:
        result = self._run_playbook("management/deploy_idm.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="IDM deployment failed")

        result = self._run_playbook("management/configure_idm.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="IDM configuration failed")

        result = self._run_playbook("management/deploy_kea.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="Kea DHCP deployment failed")

        return PhaseResult(success=True, message="IDM and Kea DHCP deployed")


# ── Phase: Mirror Registry ────────────────────────────────────────────────────

class MirrorRegistryPhase(Phase):
    name       = "mirror_registry"
    depends_on = ["management_services"]

    def run(self) -> PhaseResult:
        result = self._run_playbook("registry/deploy_registry_vm.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="Registry VM deploy failed")

        result = self._run_playbook("registry/push_images.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="Image push failed")

        result = self._run_playbook("registry/push_olm_catalogs.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="OLM catalog push failed")

        return PhaseResult(success=True, message="Mirror registry populated")


# ── Phase: Hub Cluster ────────────────────────────────────────────────────────

class HubClusterPhase(Phase):
    name       = "hub_cluster"
    depends_on = ["mirror_registry"]

    def run(self) -> PhaseResult:
        # Generate install-config.yaml from config vars
        result = self._run_playbook("hub/generate_install_config.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="install-config generation failed")

        # Run openshift-install (wrapped in Ansible)
        result = self._run_playbook("hub/install_cluster.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="Hub cluster install failed")

        # Configure cluster-level Day 1 settings
        result = self._run_playbook("hub/configure_cluster.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="Hub cluster configuration failed")

        # Pure CSI driver — needed before any PVC-backed services
        result = self._run_playbook("hub/install_pure_csi.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="Pure CSI install failed")

        return PhaseResult(success=True, message="Hub cluster installed and configured")

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
    name       = "hub_services"
    depends_on = ["hub_cluster"]

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

    def run(self) -> PhaseResult:
        services = self.config_vars.get("platform_hub_services", {})

        # First: install all OLM operators for enabled services
        result = self._run_playbook("hub_services/install_operators.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="Operator installation failed")

        # Then deploy each service in order
        for svc_name in self._SERVICE_ORDER:
            svc_config = services.get(svc_name, {})
            if not svc_config.get("enabled", False):
                console.print(f"    [dim]⊘ {svc_name}: disabled — skipping[/dim]")
                continue

            console.print(f"    [cyan]→ Deploying {svc_name}[/cyan]")
            playbook = f"hub_services/{svc_name}.yml"
            result = self._run_playbook(playbook)
            if not result.success:
                return PhaseResult(
                    success=False, rc=result.rc,
                    message=f"hub_services/{svc_name} deployment failed"
                )

        # Migrate Vault + Artifactory from bootstrap → hub
        if services.get("vault", {}).get("enabled"):
            result = self._run_playbook("hub_services/migrate_vault.yml")
            if not result.success:
                return PhaseResult(success=False, rc=result.rc, message="Vault migration failed")

        if services.get("artifactory", {}).get("enabled"):
            result = self._run_playbook("hub_services/migrate_artifactory.yml")
            if not result.success:
                return PhaseResult(success=False, rc=result.rc, message="Artifactory migration failed")

        return PhaseResult(success=True, message="All enabled hub services deployed")


# ── Phase: Spoke Clusters ─────────────────────────────────────────────────────

class SpokeClustersPhase(Phase):
    name       = "spoke_clusters"
    depends_on = ["hub_services"]

    def run(self) -> PhaseResult:
        spokes = self.config_vars.get("platform_spoke_clusters", {}).get("clusters", [])

        if not spokes:
            return PhaseResult(success=True, message="No spoke clusters defined — skipping")

        # Generate ZTP SiteConfig + PolicyGenTemplates for all spokes
        result = self._run_playbook("spokes/generate_ztp_manifests.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="ZTP manifest generation failed")

        # Push generated manifests to GitLab
        result = self._run_playbook("spokes/push_to_gitlab.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="GitLab push failed")

        # ArgoCD picks up and applies; TALM drives cluster installation
        # We poll until all clusters reach Installed state
        result = self._run_playbook(
            "spokes/wait_for_clusters.yml",
            extra_vars={"spoke_names": [s["name"] for s in spokes]},
        )
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="Spoke cluster install wait failed")

        return PhaseResult(
            success=True,
            message=f"All {len(spokes)} spoke cluster(s) installed"
        )


# ── Phase: VDI ────────────────────────────────────────────────────────────────

class VDIPhase(Phase):
    name       = "vdi_services"
    depends_on = ["hub_services"]

    def run(self) -> PhaseResult:
        vdi = self.config_vars.get("platform_vdi", {})
        if not vdi.get("enabled", False):
            return PhaseResult(success=True, message="VDI services disabled — skipping")

        platform = vdi.get("platform", "vmware")
        playbook = (
            "vdi/deploy_vmware_vdi.yml"
            if platform == "vmware"
            else "vdi/deploy_ocpvirt_vdi.yml"
        )
        result = self._run_playbook(playbook)
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="VDI deployment failed")

        return PhaseResult(success=True, message=f"VDI deployed on {platform}")


# ── Phase: Validation ─────────────────────────────────────────────────────────

class ValidationPhase(Phase):
    name       = "validation"
    depends_on = ["spoke_clusters", "vdi_services"]

    def run(self) -> PhaseResult:
        result = self._run_playbook("validation/validate_platform.yml")
        if not result.success:
            return PhaseResult(
                success=False, rc=result.rc,
                message="Platform validation failed — see output for details"
            )
        return PhaseResult(success=True, message="Platform validation passed")


# ── Phase: Bootstrap Teardown ─────────────────────────────────────────────────

class BootstrapTeardownPhase(Phase):
    name       = "bootstrap_teardown"
    depends_on = ["validation"]

    def run(self) -> PhaseResult:
        teardown_config = self.config_vars.get("platform_bootstrap", {}).get("teardown", {})
        if not teardown_config.get("enabled", True):
            return PhaseResult(success=True, message="Bootstrap teardown disabled — skipping")

        result = self._run_playbook("bootstrap/teardown_bootstrap.yml")
        if not result.success:
            return PhaseResult(success=False, rc=result.rc, message="Bootstrap teardown failed")

        return PhaseResult(success=True, message="Bootstrap VM decommissioned")


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
