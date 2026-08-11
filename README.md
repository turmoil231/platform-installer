# Platform Installer

Disconnected, bare-metal-to-OpenShift platform deployment tool.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Admin Server                             │
│                                                                 │
│  platform-installer (Python CLI)                                │
│  ├── Config Layer (Pydantic)                                    │
│  │   ├── Loads + validates platform-config.yaml                 │
│  │   ├── Cross-validates platform-manifest.yaml                 │
│  │   └── Generates Ansible group_vars + inventory               │
│  │                                                              │
│  ├── State Layer (SQLite)                                       │
│  │   ├── Tracks phase completion across runs                    │
│  │   └── Enables resume-from-failure                           │
│  │                                                              │
│  ├── Runner Layer (ansible-runner)                              │
│  │   ├── Executes playbooks with streaming output               │
│  │   ├── Injects generated vars as extra-vars                   │
│  │   └── Handles retry logic                                    │
│  │                                                              │
│  └── Phase Layer                                                │
│      ├── preflight → vmware → bootstrap → management_services   │
│      ├── → mirror_registry → hub_cluster → hub_services         │
│      └── → spoke_clusters → vdi_services → validation           │
│                          │                                      │
│                          ▼                                      │
│  ansible/                                                       │
│  ├── playbooks/          ← One per phase / sub-task             │
│  ├── roles/              ← Reusable role library                │
│  ├── inventory/generated/ ← Written by installer                │
│  └── group_vars/generated/ ← Written by installer               │
└─────────────────────────────────────────────────────────────────┘
```

## Installation

```bash
# On the admin server (RHEL 9, Python 3.11+)
pip install --break-system-packages -e .

# Verify
platform-installer --help
```

## Prerequisites

| Item | Where |
|------|-------|
| `platform-config.yaml` | Working directory |
| `platform-manifest.yaml` | Same directory as config |
| All staged assets | `assets.staging_root` on config |
| Gold images + checksums | `assets.gold_images.base_path` |
| Container images (OCI tarballs) | `assets.container_images.base_path` |
| Helm charts (.tgz) | `assets.helm_charts.base_path` |
| OLM catalog index tarballs | `assets.olm_catalogs.base_path` |
| Internal CA cert + key | `global.tls.*` paths |
| SSH key pair | `global.ssh.*` paths |
| Merged pull secret | `global.pull_secret_path` |
| `VAULT_ROLE_ID` env var | Exported before running |
| `VAULT_SECRET_ID` env var | Exported before running |

## Usage

### Validate config without deploying

```bash
platform-installer validate --config platform-config.yaml
```

### Run full deployment

```bash
export VAULT_ROLE_ID=<role-id>
export VAULT_SECRET_ID=<secret-id>

platform-installer deploy --config platform-config.yaml
```

### Dry run (validates + generates vars, skips Ansible)

```bash
platform-installer deploy --config platform-config.yaml --dry-run
```

### Check status

```bash
platform-installer status --config platform-config.yaml
```

Output:
```
╭──────────────────────┬──────────┬─────────┬─────────────────────┬────────────────────────────╮
│ Phase                │  Status  │ Attempt │ Started             │ Message                    │
├──────────────────────┼──────────┼─────────┼─────────────────────┼────────────────────────────┤
│ preflight            │ complete │       1 │ 2025-01-15T10:00:00 │ All preflight checks passed│
│ vmware               │ complete │       1 │ 2025-01-15T10:05:00 │ vSphere stack deployed     │
│ bootstrap            │ complete │       1 │ 2025-01-15T11:30:00 │ Bootstrap VM running       │
│ management_services  │ failed   │       2 │ 2025-01-15T12:00:00 │ IDM deployment failed      │
│ mirror_registry      │ pending  │       0 │                     │                            │
│ hub_cluster          │ pending  │       0 │                     │                            │
...
```

### Resume from a failed phase

```bash
# Reset the failed phase and re-run from there
platform-installer reset --config platform-config.yaml --phase management_services
platform-installer deploy --config platform-config.yaml --from-phase management_services
```

### Run a single phase

```bash
platform-installer deploy --config platform-config.yaml --phase preflight
```

### Run a range of phases

```bash
platform-installer deploy \
  --config platform-config.yaml \
  --from-phase hub_cluster \
  --to-phase hub_services
```

### Manual approval mode

Set `global.automation.fully_automated: false` in your config, then:

```bash
platform-installer deploy --config platform-config.yaml
# Installer will pause at each checkpoint defined in approval_checkpoints
```

Or override for a single run:
```bash
platform-installer deploy --config platform-config.yaml --auto-approve
```

### Full deployment report

```bash
platform-installer report --config platform-config.yaml
```

## Phase Overview

| Phase | What happens |
|-------|-------------|
| `preflight` | Validate config, check BMC/storage reachability, verify staged assets |
| `vmware` | Install ESXi on claimed servers → deploy vCenter OVA → configure cluster, dvSwitch, datastores |
| `bootstrap` | Deploy bootstrap VM → start Vault + Artifactory containers → seed both |
| `management_services` | Deploy IDM VMs (primary + replica) → configure DNS/LDAP/CA/Kerberos → deploy Kea DHCP |
| `mirror_registry` | Deploy registry VM → push all container images and OLM catalog indexes |
| `hub_cluster` | Deploy OpenShift Hub (vSphere IPI or Bare Metal AI) → configure → install Pure CSI |
| `hub_services` | Install operators → deploy all enabled services → migrate Vault + Artifactory from bootstrap |
| `spoke_clusters` | Generate ZTP manifests → push to GitLab → wait for ACM/TALM to install all spokes |
| `vdi_services` | Deploy VDI pool (vSphere or OCP Virtualization) |
| `validation` | End-to-end health checks across all components |
| `bootstrap_teardown` | (Manual/optional) Decommission bootstrap VM |

## Project Structure

```
platform-installer/
├── installer/
│   ├── cli.py              # Click CLI — all commands
│   ├── config/
│   │   ├── loader.py       # YAML load, manifest check, var + inventory generation
│   │   └── models.py       # Pydantic v2 models for full config schema
│   ├── phases/
│   │   └── base.py         # Phase base class + all phase implementations
│   ├── runner/
│   │   └── ansible.py      # ansible-runner wrapper with streaming + retry
│   └── state/
│       └── store.py        # SQLite phase state store
├── ansible/
│   ├── playbooks/          # One playbook per phase/sub-task
│   │   ├── preflight.yml
│   │   ├── vmware/
│   │   │   ├── install_esxi.yml
│   │   │   ├── deploy_vcenter.yml
│   │   │   ├── configure_vcenter.yml
│   │   │   └── configure_storage.yml
│   │   ├── bootstrap/
│   │   ├── management/
│   │   ├── registry/
│   │   ├── hub/
│   │   ├── hub_services/   # One playbook per enabled service
│   │   ├── spokes/
│   │   ├── vdi/
│   │   └── validation/
│   ├── roles/              # Reusable Ansible roles
│   │   ├── pure_csi/
│   │   ├── idm/
│   │   ├── kea/
│   │   ├── ocp_install/
│   │   └── ...
│   ├── collections/        # Offline collection tarballs (no Galaxy calls)
│   ├── inventory/
│   │   └── generated/      # Written by installer — do not edit manually
│   └── group_vars/
│       └── generated/      # Written by installer — do not edit manually
├── vault-policies/         # HCL policies seeded into Vault
│   ├── platform-installer.hcl
│   └── ocp-workloads.hcl
├── platform-config.yaml
├── platform-manifest.yaml
└── pyproject.toml
```

## Adding a New Phase

1. Create a class in `installer/phases/base.py` extending `Phase`
2. Set `name` and `depends_on`
3. Implement `run()` calling `self._run_playbook()`
4. Optionally implement `health_check()`
5. Add the class to `ALL_PHASES` at the bottom of `base.py`
6. Create the corresponding playbook in `ansible/playbooks/`

## Adding a New Hub Service

1. Add an enable/disable + config block to `hub_services:` in `platform-config.yaml`
2. Add a Pydantic model in `installer/config/models.py` and field in `HubServicesConfig`
3. Add the service name to `HubServicesPhase._SERVICE_ORDER` in `base.py`
4. Create `ansible/playbooks/hub_services/<service_name>.yml`

## Ansible Collections (Disconnected)

All required collections must be pre-staged as tarballs in `ansible/collections/`.
Do not use `ansible-galaxy install` at deploy time.

Required collections:
- `ansible.builtin`
- `community.general`
- `redhat.rhel_system_roles`
- `kubernetes.core`
- `community.vmware`
- `community.crypto`
- `ansible.posix`
