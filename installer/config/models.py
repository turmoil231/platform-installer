"""
installer/config/models.py

Pydantic v2 models for every section of platform-config.yaml.
The installer will refuse to start if the loaded config does not
validate against these models.

Design rules:
  - Every required field has NO default.
  - Every optional field has a sensible default.
  - Secrets are always strings of the form "vault:secret/path/key"
    and validated by the VaultSecretRef type.
  - Cross-section references (e.g. server_ref pointing at inventory.compute)
    are validated in Config.model_post_init(), not here.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Helpers ────────────────────────────────────────────────────────────────────

VAULT_REF_RE = re.compile(r"^vault:(?:secret|pki)/[\w/\-]+$")


def vault_ref(value: str) -> str:
    if not VAULT_REF_RE.match(value):
        raise ValueError(
            f"Secret references must be 'vault:secret/path' or 'vault:pki/path', got: {value!r}"
        )
    return value


VaultSecretRef = str  # annotated alias; validated via field_validator where used


class PlatformType(str, Enum):
    vsphere   = "vsphere"
    baremetal = "baremetal"


class ClusterType(str, Enum):
    sno      = "sno"
    compact  = "compact"
    standard = "standard"


class NodeTopology(str, Enum):
    schedulable_masters    = "schedulable_masters"
    masters_and_workers    = "masters_and_workers"
    masters_infra_workers  = "masters_infra_workers"


# ── Global ─────────────────────────────────────────────────────────────────────

class TLSConfig(BaseModel):
    internal_ca_cert_path: str
    internal_ca_key_path:  str
    additional_trust_bundles: list[str] = []


class SSHConfig(BaseModel):
    public_key_path:  str
    private_key_path: str


class AutomationConfig(BaseModel):
    fully_automated:       bool = True
    approval_checkpoints:  list[str] = []
    retry_on_failure:      bool = True
    max_retries:           int = 3
    retry_delay_seconds:   int = 60
    log_level:             str = "info"

    @field_validator("log_level")
    @classmethod
    def valid_log_level(cls, v: str) -> str:
        if v not in {"debug", "info", "warn", "error"}:
            raise ValueError(f"log_level must be debug|info|warn|error, got {v!r}")
        return v


class GlobalConfig(BaseModel):
    environment_name:  str
    domain_base:       str
    timezone:          str = "UTC"
    disconnected_mode: bool = True
    ntp_servers:       list[str] = Field(min_length=1)
    tls:               TLSConfig
    ssh:               SSHConfig
    pull_secret_path:  str
    automation:        AutomationConfig = AutomationConfig()
    known_external_hosts: list[str] = []


# ── Platform Manifest ──────────────────────────────────────────────────────────

class PlatformManifest(BaseModel):
    file:    str = "platform-manifest.yaml"
    version: str


# ── Inventory ──────────────────────────────────────────────────────────────────

class BMCConfig(BaseModel):
    protocol:                      str = "redfish"
    address:                       str
    port:                          int = 443
    credentials_secret:            str
    disable_certificate_verification: bool = True

    @field_validator("credentials_secret")
    @classmethod
    def validate_secret(cls, v: str) -> str:
        return vault_ref(v)


class NICEntry(BaseModel):
    name:       str
    mac:        str
    speed_gbps: int
    purpose:    str


class DiskEntry(BaseModel):
    slot:     int
    size_gb:  int
    type:     str
    purpose:  str


class ComputeServer(BaseModel):
    role_hint:           str
    make:                str = ""
    model:               str = ""
    bmc:                 BMCConfig
    nics:                list[NICEntry]
    disks:               list[DiskEntry]
    memory_gb:           int
    cpu_sockets:         int = 1
    cpu_cores_per_socket: int


class ISCSIInterface(BaseModel):
    name:       str
    ip:         str
    speed_gbps: int


class FlashArrayConfig(BaseModel):
    name:               str
    model:              str = ""
    management:         dict[str, Any]
    iscsi:              dict[str, Any] = {}
    nvme_tcp:           dict[str, Any] = {}
    fibre_channel:      dict[str, Any] = {}
    vvols:              dict[str, Any] = {}
    replication:        dict[str, Any] = {}


class FlashBladeConfig(BaseModel):
    name:       str
    model:      str = ""
    management: dict[str, Any]
    nfs:        dict[str, Any] = {}
    s3:         dict[str, Any] = {}


class NetworkSegment(BaseModel):
    cidr:       str
    vlan_id:    int
    gateway:    str = ""
    dns_zone:   str = ""
    purpose:    str = ""


class NetworkConfig(BaseModel):
    segments:            dict[str, Any]      # Flexible — base + spoke_base have different shapes
    default_mtu:         int = 9000
    jumbo_mtu_segments:  list[str] = []
    static_routes:       list[Any] = []
    uplinks:             list[dict[str, str]] = []


class InventoryStorageConfig(BaseModel):
    pure_flash_arrays: list[FlashArrayConfig] = []
    pure_flash_blades: list[FlashBladeConfig] = []


class InventoryConfig(BaseModel):
    compute: dict[str, ComputeServer]
    storage: InventoryStorageConfig
    network: NetworkConfig


# ── VMware ─────────────────────────────────────────────────────────────────────

class ESXiHostConfig(BaseModel):
    server:             str              # inventory.compute key
    hostname:           str
    management_ip:      str
    management_nic:     str
    vmotion_ip:         str = ""
    storage_ips:        list[dict[str, str]] = []
    root_password_secret: str

    @field_validator("root_password_secret")
    @classmethod
    def validate_secret(cls, v: str) -> str:
        return vault_ref(v)


class DVSwitchPortGroup(BaseModel):
    name:    str
    vlan_id: Any                         # int or str (trunk range)
    trunk:   bool = False
    teaming: str = "active-active"


class DVSwitchConfig(BaseModel):
    name:        str
    version:     str
    uplinks:     list[str]
    mtu:         int = 9000
    port_groups: list[DVSwitchPortGroup]


class VCenterDatastore(BaseModel):
    name:        str
    type:        str                     # vmfs | nfs
    array:       str = ""
    blade:       str = ""
    protocol:    str = ""
    size_tb:     float = 0
    nfs_export:  str = ""
    nfs_version: str = ""
    purpose:     str = ""


class VCenterConfig(BaseModel):
    hostname:            str
    ip:                  str
    bootstrap_host:      str
    vcsa_ova_path:       str
    credentials_secret:  str
    thumbprint:          str = ""
    sso:                 dict[str, str]
    datacenter:          str
    cluster:             str
    ha:                  dict[str, Any] = {}
    drs:                 dict[str, Any] = {}
    datastores:          list[VCenterDatastore] = []
    vm_folder_structure: list[str] = []

    @field_validator("credentials_secret")
    @classmethod
    def validate_secret(cls, v: str) -> str:
        return vault_ref(v)


class VMwareConfig(BaseModel):
    enabled:  bool = True
    esxi:     dict[str, Any]
    vcenter:  VCenterConfig


# ── Bootstrap ──────────────────────────────────────────────────────────────────

class BootstrapVMConfig(BaseModel):
    hostname:   str
    fqdn:       str
    ip:         str
    gold_image: str
    vsphere:    dict[str, Any]
    resources:  dict[str, Any]


class BootstrapConfig(BaseModel):
    vm:           BootstrapVMConfig
    vault:        dict[str, Any]
    artifactory:  dict[str, Any]
    teardown:     dict[str, Any]


# ── Cluster Storage ────────────────────────────────────────────────────────────

class StorageClass(BaseModel):
    name:                str
    backend:             str
    type:                str              # block | file
    reclaim_policy:      str = "Delete"
    volume_binding_mode: str = "WaitForFirstConsumer"
    fstype:              str = "xfs"
    nfs_version:         str = ""
    is_default:          bool = False


class PureCSIConfig(BaseModel):
    enabled:                    bool = True
    arrays:                     list[str] = []
    blades:                     list[str] = []
    chart_ref:                  str = "pure-pso"
    namespace:                  str = "pure-csi"
    storage_classes:            list[StorageClass] = []
    default_block_storage_class: str = "pure-block-rwo"
    default_file_storage_class:  str = "pure-file-rwx"


class ODFConfig(BaseModel):
    enabled:                        bool = False
    mode:                           str = "internal"
    storage_class_for_device_sets:  str = "pure-block-rwo"
    device_sets:                    list[dict[str, Any]] = []
    mcg:                            dict[str, Any] = {}


class S3BucketConfig(BaseModel):
    name:                str
    blade:               str
    credentials_secret:  str
    versioning:          bool = False
    purpose:             str = ""

    @field_validator("credentials_secret")
    @classmethod
    def validate_secret(cls, v: str) -> str:
        return vault_ref(v)


class ClusterStorageConfig(BaseModel):
    pure_csi:    PureCSIConfig = PureCSIConfig()
    odf:         ODFConfig     = ODFConfig()
    s3_buckets:  list[S3BucketConfig] = []


# ── Hub Cluster ────────────────────────────────────────────────────────────────

class VSphereClusterPlatform(BaseModel):
    vcenter:             str
    datacenter:          str
    cluster:             str
    datastore:           str
    network:             str
    folder:              str
    credentials_secret:  str
    disk_type:           str = "thick"

    @field_validator("credentials_secret")
    @classmethod
    def validate_secret(cls, v: str) -> str:
        return vault_ref(v)


class BaremetalClusterPlatform(BaseModel):
    provisioning_network: dict[str, str]
    deploy_kernel_url:    str = ""
    deploy_ramdisk_url:   str = ""


class ClusterPlatform(BaseModel):
    type:      PlatformType
    vsphere:   VSphereClusterPlatform  | None = None
    baremetal: BaremetalClusterPlatform | None = None

    @model_validator(mode="after")
    def platform_config_present(self) -> "ClusterPlatform":
        if self.type == PlatformType.vsphere and self.vsphere is None:
            raise ValueError("platform.vsphere must be defined when type=vsphere")
        if self.type == PlatformType.baremetal and self.baremetal is None:
            raise ValueError("platform.baremetal must be defined when type=baremetal")
        return self


class NodeDefinition(BaseModel):
    hostname:         str
    ip:               str
    server_ref:       str = ""           # inventory.compute key (baremetal only)
    boot_mac_address: str = ""
    vm_profile:       dict[str, Any] = {}
    root_device_hint: dict[str, str] = {}


class NodeGroup(BaseModel):
    replicas:  int
    vm_profile: dict[str, Any] = {}
    nodes:     list[NodeDefinition] = []
    taint:     str = ""
    labels:    dict[str, str] = {}
    node_selectors_applied: list[str] = []


class InfraNodeGroup(NodeGroup):
    enabled: bool = True


class OLMOperator(BaseModel):
    name:      str
    channel:   str
    catalog:   str
    namespace: str
    approval:  str = "Automatic"


class LDAPGroupSync(BaseModel):
    enabled:  bool = True
    schedule: str = "*/30 * * * *"
    groups:   list[dict[str, str]] = []


class LDAPAuthConfig(BaseModel):
    url:                  str
    bind_dn:              str
    bind_password_secret: str
    attributes:           dict[str, list[str]]
    group_sync:           LDAPGroupSync = LDAPGroupSync()

    @field_validator("bind_password_secret")
    @classmethod
    def validate_secret(cls, v: str) -> str:
        return vault_ref(v)


class AuthConfig(BaseModel):
    type: str = "ldap"
    ldap: LDAPAuthConfig | None = None


class HubClusterConfig(BaseModel):
    name:             str
    base_domain:      str
    version_ref:      str
    platform:         ClusterPlatform
    cluster_network:  dict[str, Any]
    service_network:  dict[str, Any]
    machine_network:  dict[str, Any]
    api_vip:          str
    ingress_vip:      str
    install_config:   dict[str, Any] = {}
    node_topology:    NodeTopology = NodeTopology.masters_infra_workers
    nodes:            dict[str, Any]
    storage:          ClusterStorageConfig = ClusterStorageConfig()
    authentication:   AuthConfig
    operators:        list[OLMOperator] = []


# ── Hub Services ───────────────────────────────────────────────────────────────

class HubService(BaseModel):
    """Base for all hub services.  All services must have enabled:."""
    enabled: bool = True


class VaultService(HubService):
    namespace:             str = "vault"
    chart_ref:             str = "vault"
    helm_values:           dict[str, Any] = {}
    migration:             dict[str, Any] = {}
    route:                 dict[str, str] = {}
    admin_password_secret: str = ""


class ArtifactoryService(HubService):
    namespace:             str = "artifactory"
    chart_ref:             str = "artifactory-oss"
    helm_values:           dict[str, Any] = {}
    migration:             dict[str, Any] = {}
    route:                 dict[str, str] = {}
    admin_password_secret: str = ""
    repositories:          list[dict[str, str]] = []


class GitLabService(HubService):
    namespace:             str = "gitlab"
    chart_ref:             str = "gitlab"
    deployment_method:     str = "acm_policy"
    edition:               str = "ee"
    route:                 dict[str, str] = {}
    admin_password_secret: str = ""
    ldap:                  dict[str, Any] = {}
    storage:               dict[str, Any] = {}
    runners:               dict[str, Any] = {}


class JiraService(HubService):
    namespace:             str = "jira"
    chart_ref:             str = "jira"
    route:                 dict[str, str] = {}
    admin_password_secret: str = ""
    ldap:                  dict[str, Any] = {}
    storage:               dict[str, Any] = {}


class ConfluenceService(HubService):
    namespace:             str = "confluence"
    chart_ref:             str = "confluence"
    route:                 dict[str, str] = {}
    admin_password_secret: str = ""
    ldap:                  dict[str, Any] = {}
    storage:               dict[str, Any] = {}


class AAPService(HubService):
    namespace:  str = "aap"
    operator:   dict[str, Any] = {}
    controller: dict[str, Any] = {}
    hub:        dict[str, Any] = {}
    eda:        dict[str, Any] = {}


class ELKService(HubService):
    namespace:       str = "elastic-system"
    operator:        dict[str, Any] = {}
    elasticsearch:   dict[str, Any] = {}
    kibana:          dict[str, Any] = {}
    logstash:        dict[str, Any] = {}
    log_sources:     list[str] = []


class ServiceMeshService(HubService):
    namespace:     str = "istio-system"
    operator:      dict[str, Any] = {}
    control_plane: dict[str, Any] = {}
    kiali:         dict[str, Any] = {}


class ACSService(HubService):
    namespace:        str = "stackrox"
    operator:         dict[str, Any] = {}
    central:          dict[str, Any] = {}
    secured_clusters: list[dict[str, str]] = []


class AnchoreService(HubService):
    namespace:             str = "anchore"
    chart_ref:             str = "anchore-enterprise"
    admin_password_secret: str = ""
    route:                 dict[str, str] = {}
    ldap:                  dict[str, Any] = {}
    storage:               dict[str, Any] = {}
    integrations:          dict[str, bool] = {}


class ODFService(HubService):
    namespace:  str = "openshift-storage"
    mcg_route:  dict[str, str] = {}
    rgw_route:  dict[str, Any] = {}


class LocalStorageService(HubService):
    namespace: str = "openshift-local-storage"


class GitOpsService(HubService):
    namespace:             str = "openshift-gitops"
    admin_password_secret: str = ""
    git_backend:           dict[str, str] = {}
    applications:          list[dict[str, str]] = []


class ObservabilityService(HubService):
    monitoring: dict[str, Any] = {}
    logging:    dict[str, Any] = {}


class ACMService(HubService):
    namespace:              str = "open-cluster-management"
    multicluster_hub:       dict[str, Any] = {}
    assisted_service:       dict[str, Any] = {}
    infrastructure_environment: dict[str, Any] = {}


class HubServicesConfig(BaseModel):
    vault:          VaultService         = VaultService(enabled=False)
    artifactory:    ArtifactoryService   = ArtifactoryService(enabled=False)
    gitlab:         GitLabService        = GitLabService(enabled=False)
    jira:           JiraService          = JiraService(enabled=False)
    confluence:     ConfluenceService    = ConfluenceService(enabled=False)
    aap:            AAPService           = AAPService(enabled=False)
    elk:            ELKService           = ELKService(enabled=False)
    service_mesh:   ServiceMeshService   = ServiceMeshService(enabled=False)
    acs:            ACSService           = ACSService(enabled=False)
    anchore:        AnchoreService       = AnchoreService(enabled=False)
    odf:            ODFService           = ODFService(enabled=False)
    local_storage:  LocalStorageService  = LocalStorageService(enabled=False)
    gitops:         GitOpsService        = GitOpsService(enabled=False)
    observability:  ObservabilityService = ObservabilityService(enabled=True)
    acm:            ACMService           = ACMService(enabled=False)

    def enabled_services(self) -> list[tuple[str, HubService]]:
        """Return (name, service) pairs for every enabled service, in deploy order."""
        return [
            (name, svc)
            for name, svc in self.model_dump(exclude_none=True).items()
            if isinstance(getattr(self, name), HubService)
            and getattr(self, name).enabled
        ]


# ── Spoke Clusters ─────────────────────────────────────────────────────────────

class SpokeNodeDefinition(BaseModel):
    hostname:         str
    role:             str              # master | worker
    ip:               str
    server_ref:       str = ""
    boot_mac_address: str = ""
    vm_profile:       dict[str, Any] = {}
    root_device_hint: dict[str, str] = {}


class SpokeClusterConfig(BaseModel):
    name:             str
    base_domain:      str
    site_id:          str = ""
    cluster_type:     ClusterType
    machine_network:  dict[str, str]
    api_vip:          str = ""
    ingress_vip:      str = ""
    platform:         ClusterPlatform
    nodes:            list[SpokeNodeDefinition]
    storage:          dict[str, Any] = {}
    overrides:        dict[str, Any] = {}


class SpokeClustersConfig(BaseModel):
    defaults:  dict[str, Any] = {}
    clusters:  list[SpokeClusterConfig] = []


# ── VDI ────────────────────────────────────────────────────────────────────────

class VDIServicesConfig(BaseModel):
    enabled:                   bool = False
    platform:                  str = "vmware"
    vmware:                    dict[str, Any] = {}
    openshift_virtualization:  dict[str, Any] = {}
    migration:                 dict[str, Any] = {}


# ── Secrets ────────────────────────────────────────────────────────────────────

class SecretsConfig(BaseModel):
    backend:  str = "vault"
    vault:    dict[str, Any]


# ── Local services (admin host) ────────────────────────────────────────────────

class HaulerServiceConfig(BaseModel):
    bind_address:    str = "0.0.0.0"
    registry_port:   int = 5000
    fileserver_port: int = 8080


class LocalVaultConfig(BaseModel):
    #: Image reference as served by Hauler's registry (no registry host).
    #: Defaults to manifest image_pins.vault.source with its registry host
    #: stripped (see installer/cli.py::_local_vault_image).
    image:        str | None = None
    bind_address: str = "127.0.0.1"
    port:         int = 8200
    #: Where the configure-vault playbook writes Vault's init output (unseal
    #: keys, root token) and the AppRole credentials (JSON or YAML with
    #: role_id and secret_id) the installer passes to every later playbook.
    #: Relative paths resolve against --state-dir.
    init_output_path:         str = "vault-credentials/init.json"
    approle_credentials_path: str = "vault-credentials/approle.json"


class LocalServicesConfig(BaseModel):
    """
    Hauler and Vault containers the installer runs on the admin host from
    the start of a deploy until hub_services has migrated their data to the
    hub cluster. See installer/runner/services.py.
    """
    #: Routable admin-host address that BMCs and ESXi hosts use to reach
    #: Hauler's fileserver (e.g. for Redfish virtual media).
    advertise_address: str | None = None
    hauler:            HaulerServiceConfig = HaulerServiceConfig()
    vault:             LocalVaultConfig = LocalVaultConfig()


# ── Preflight ──────────────────────────────────────────────────────────────────

class PreflightCheck(BaseModel):
    id:           str
    description:  str
    threshold_gb: int | None = None


class PreflightConfig(BaseModel):
    fail_fast: bool = True
    checks:    list[PreflightCheck]


# ── Root Config ────────────────────────────────────────────────────────────────

class PlatformConfig(BaseModel):
    """Root model.  Validates the entire platform-config.yaml."""

    platform_manifest: PlatformManifest
    global_:           GlobalConfig = Field(alias="global")
    inventory:         InventoryConfig
    vmware:            VMwareConfig
    bootstrap:         BootstrapConfig
    mirror_registry:   dict[str, Any]
    management_services: dict[str, Any]
    hub_cluster:       HubClusterConfig
    hub_services:      HubServicesConfig
    spoke_clusters:    SpokeClustersConfig
    vdi_services:      VDIServicesConfig = VDIServicesConfig()
    local_services:    LocalServicesConfig = LocalServicesConfig()
    secrets:           SecretsConfig
    preflight:         PreflightConfig

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def cross_section_validation(self) -> "PlatformConfig":
        """Validate references that span sections."""
        compute = self.inventory.compute

        # Every ESXi host server_ref must exist in inventory.compute
        for host in self.vmware.esxi.get("hosts", []):
            server = host.get("server", "")
            if server and server not in compute:
                raise ValueError(
                    f"vmware.esxi.hosts[].server={server!r} not found in inventory.compute"
                )

        # Every hub node server_ref must exist in inventory.compute
        for group_name, group in self.hub_cluster.nodes.items():
            for node in group.get("nodes", []):
                ref = node.get("server_ref", "")
                if ref and ref not in compute:
                    raise ValueError(
                        f"hub_cluster.nodes.{group_name}[].server_ref={ref!r} not found in inventory.compute"
                    )

        # Every spoke node server_ref must exist in inventory.compute
        for spoke in self.spoke_clusters.clusters:
            for node in spoke.nodes:
                if node.server_ref and node.server_ref not in compute:
                    raise ValueError(
                        f"spoke_clusters[{spoke.name}].nodes[].server_ref={node.server_ref!r} "
                        f"not found in inventory.compute"
                    )

        # No server claimed by both esxi and a baremetal cluster
        esxi_claimed = {
            host.get("server") for host in self.vmware.esxi.get("hosts", [])
        }
        for spoke in self.spoke_clusters.clusters:
            if spoke.platform.type == PlatformType.baremetal:
                for node in spoke.nodes:
                    if node.server_ref in esxi_claimed:
                        raise ValueError(
                            f"Server {node.server_ref!r} is claimed by both vmware.esxi and "
                            f"spoke_clusters[{spoke.name}] — a server cannot have two roles"
                        )

        return self
