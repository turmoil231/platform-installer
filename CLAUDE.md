# Platform Installer — Claude Code Context

## What this project is

A Python CLI application that orchestrates a fully automated, disconnected
(air-gapped) enterprise platform deployment. It reads `platform-config.yaml`
and `platform-manifest.yaml`, validates them, generates Ansible inventory and
group_vars, then executes a series of phases by calling Ansible playbooks
through ansible-runner.

The installer itself is a PyInstaller-compiled single binary that runs
**natively** on the admin host — no container, no venv to activate. Ansible
never runs on the host: every playbook executes inside a separate, preloaded
"Ansible execution image" (ansible-core + all collections baked in at build
time), launched per playbook run via ansible-runner's container/
process-isolation executor (`process_isolation=True`). See
`installer/runner/ansible.py` and `packaging/Containerfile.ansible-exec`.

## Architecture decisions already made — do not relitigate these

- **Python + ansible-runner**: The installer is Python. Ansible does all
  infrastructure work. Python owns orchestration, state, and the CLI.
- **Pydantic v2** for config validation. All models are in
  `installer/config/models.py`.
- **SQLite state store** (`installer/state/store.py`) tracks phase completion.
  Enables resume-from-failure. Never re-run a completed phase.
- **ansible-runner** (not subprocess) for Ansible execution. Streaming output
  via event_handler. Configured for containerized execution
  (`process_isolation=True`, `container_image=...`) — it shells out to
  `podman/docker run <ansible-exec-image> ansible-playbook ...` per playbook.
  See `installer/runner/ansible.py`.
- **The installer has no concept of collection staging at all.** Collections
  are baked into the Ansible execution image ahead of time, by whatever
  process produces that image — `packaging/build_ansible_image.sh` is one
  option, but the installer doesn't require or assume it. The CLI just takes
  an `--ansible-image` tag and runs it; it never inspects, validates, or
  installs collections itself.
- **Two-file config model**: `platform-config.yaml` (topology/intent) +
  `platform-manifest.yaml` (all version pins and checksums).
- **Distribution**: a compiled single binary (`platform-installer-<version>`,
  built by `packaging/build_binary.sh`) + a preloaded Ansible execution image
  tarball (built by `packaging/build_ansible_image.sh`), transferred together.
  The binary auto-loads the image into the local podman/docker store on
  first run if it isn't already present (`AnsibleRunner.ensure_ready()`) —
  no separate load step, no wrapper script.

## Project structure

```
platform-installer/
├── installer/
│   ├── cli.py                  # Click CLI — all commands (PyInstaller entry point)
│   ├── config/
│   │   ├── loader.py           # YAML load, manifest validation, var generation
│   │   └── models.py           # Pydantic v2 models for full config schema
│   ├── phases/
│   │   └── base.py             # Phase base class + all phase implementations
│   ├── runner/
│   │   └── ansible.py          # AnsibleRunner (container executor)
│   └── state/
│       └── store.py            # SQLite phase state store
├── ansible/
│   ├── playbooks/              # Thin orchestration playbooks (call collection roles)
│   └── collections/
│       └── requirements.yml    # Collection dependency declarations
├── packaging/
│   ├── Containerfile.ansible-exec  # Ansible execution image build (ansible-core + collections)
│   ├── build_ansible_image.sh      # Builds + saves the Ansible execution image
│   ├── build_binary.sh             # PyInstaller build of the compiled CLI binary
│   └── seed_pip_cache.sh           # Pre-downloads pip wheels for offline builds
├── scripts/
│   └── stage_collections.sh   # Fetches collection tarballs from GitLab
├── vault-policies/             # Vault HCL policies seeded during bootstrap
├── pyproject.toml
├── platform-config.yaml.example
└── platform-manifest.yaml.example
```

At runtime, ansible-runner's `private_data_dir` (`<state-dir>/ansible-pdd/`)
holds a synced copy of `ansible/` (`project/`), generated extra-vars
(`vars/`), and generated inventory (`inventory/`) — this whole directory is
bind-mounted into the Ansible execution container on every playbook run.
Nothing is written under the `ansible/` source tree at runtime anymore.

## Deployment phases (in order)

| Phase | Python class | Key playbooks |
|-------|-------------|---------------|
| preflight | PreflightPhase | preflight.yml |
| vmware | VMwarePhase | vmware/install_esxi.yml, vmware/deploy_vcenter.yml, vmware/configure_vcenter.yml, vmware/configure_storage.yml |
| bootstrap | BootstrapPhase | bootstrap/deploy_bootstrap_vm.yml, bootstrap/start_vault.yml, bootstrap/start_artifactory.yml, bootstrap/seed_vault.yml, bootstrap/seed_artifactory.yml |
| management_services | ManagementServicesPhase | management/deploy_idm.yml, management/configure_idm.yml, management/deploy_kea.yml |
| mirror_registry | MirrorRegistryPhase | registry/deploy_registry_vm.yml, registry/push_images.yml, registry/push_olm_catalogs.yml |
| hub_cluster | HubClusterPhase | hub/generate_install_config.yml, hub/install_cluster.yml, hub/configure_cluster.yml, hub/install_pure_csi.yml |
| hub_services | HubServicesPhase | hub_services/install_operators.yml, hub_services/<service>.yml per enabled service, hub_services/migrate_vault.yml, hub_services/migrate_artifactory.yml |
| spoke_clusters | SpokeClustersPhase | spokes/generate_ztp_manifests.yml, spokes/push_to_gitlab.yml, spokes/wait_for_clusters.yml |
| vdi_services | VDIPhase | vdi/deploy_vmware_vdi.yml or vdi/deploy_ocpvirt_vdi.yml |
| validation | ValidationPhase | validation/validate_platform.yml |
| bootstrap_teardown | BootstrapTeardownPhase | bootstrap/teardown_bootstrap.yml |

## Key variable conventions

- All vars generated by ConfigLoader use prefix `platform_`:
  `platform_global`, `platform_hub_cluster`, `platform_hub_services`,
  `platform_vmware`, `platform_management_services`, `platform_vdi`,
  `platform_inventory_compute`, `platform_inventory_storage`,
  `platform_inventory_network`, `platform_manifest`, `platform_assets`
- Per-spoke vars: `platform_spoke` (merged defaults + cluster overrides)
- Secrets: always `vault:secret/path` strings resolved at task time
  via `community.hashi_vault.hashi_vault` lookup

## Ansible collection dependencies

All collections come from the platform.ocp and other platform.* collections
in separate GitLab repos. Declared in `ansible/collections/requirements.yml`.
Staged as tarballs (`scripts/stage_collections.sh`), then baked into the
Ansible execution image at build time (`packaging/build_ansible_image.sh`).
Never installed on the admin host, at build time or deploy time.

Required collections:
- platform.ocp (OCP install, post-install, validation)
- platform.vmware (ESXi install, vCenter deploy/config)
- platform.storage (Pure CSI, FlashArray/FlashBlade config)
- platform.idm (IDM deploy and config)
- platform.bootstrap (bootstrap VM, Vault, Artifactory)
- platform.hub_services (GitLab, Vault, Artifactory, AAP, ACS, etc.)
- platform.ztp (ZTP manifest generation, spoke management)
- community.vmware, kubernetes.core, community.hashi_vault, redhat.rhel_system_roles

## What needs to be built (priority order)

1. **Tests** — `tests/unit/` for config loader, models, state store
2. **Playbooks** — stub playbooks for each phase (thin — they import_role from collections)
3. **Ansible collection: platform.vmware** — ESXi install via Redfish, vCenter OVA deploy, dvSwitch, iSCSI
4. **Ansible collection: platform.bootstrap** — bootstrap VM deploy, Vault/Artifactory containers
5. **Ansible collection: platform.idm** — IDM install, LDAP config, DNS records
6. **Vault policies** — HCL files in vault-policies/
7. **GitLab CI pipeline** — `.gitlab-ci.yml` for building and publishing

## Environment assumptions

- Admin server: RHEL 9, podman installed (docker fallback) — no Python
  required; the installer is a compiled binary
- All deployments: fully disconnected (no internet access at deploy time)
- Secrets: HashiCorp Vault (AppRole auth), VAULT_ROLE_ID + VAULT_SECRET_ID env vars
- Container runtime: podman preferred, docker fallback (used to launch the
  Ansible execution image — the installer binary itself never runs containerized)
- All OCP clusters: RHEL 9 gold images, OCP 4.16
- Storage: Pure FlashArray (iSCSI) + Pure FlashBlade (NFS/S3)
- Identity: Red Hat IDM (LDAP + DNS + CA)

## Do not

- Add internet-calling code — everything must work air-gapped
- Use subprocess for Ansible — use ansible-runner only
- Put secrets in var files — always vault lookup at task time
- Call ansible-galaxy at deploy time — collections are pre-staged and baked
  into the Ansible execution image at build time. This is structurally
  enforced now: collections aren't even reachable from the admin host
  process, only from inside the execution image.
- Use `set_fact` to pass data between separate playbook runs —
  use the discovered_vars.yml file pattern (see platform.ocp collection)
