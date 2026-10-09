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
│  │   └── Generates Ansible extra-vars + inventory               │
│  │                                                              │
│  ├── Local Services (podman, every run)                         │
│  │   ├── Hauler: serves the haul's images + files               │
│  │   └── Vault: secrets until migrated to the hub cluster       │
│  │                                                              │
│  ├── State Layer (SQLite)                                       │
│  │   ├── Tracks phase completion across runs                    │
│  │   └── Enables resume-from-failure                           │
│  │                                                              │
│  ├── Runner Layer (ansible-runner, container executor)          │
│  │   ├── Launches the Ansible execution image per playbook      │
│  │   ├── Injects generated vars/inventory via private_data_dir  │
│  │   └── Handles retry logic                                    │
│  │                                                              │
│  └── Phase Layer                                                │
│      ├── preflight → vmware → bootstrap → management_services   │
│      ├── → mirror_registry → hub_cluster → hub_services         │
│      └── → spoke_clusters → vdi_services → validation           │
│                          │                                      │
│                          ▼                                      │
│  <state-dir>/ansible-pdd/  (ansible-runner private_data_dir)    │
│  ├── project/    ← synced copy of ansible/ (playbooks/, ...)    │
│  ├── vars/       ← generated extra-vars (every playbook)        │
│  └── inventory/  ← generated static inventory                   │
└─────────────────────────────────────────────────────────────────┘
```

Ansible itself never runs on the admin host — every playbook executes inside
a preloaded "Ansible execution image" (ansible-core + all collections baked
in at build time), launched via `ansible-runner`'s container executor. See
`installer/runner/ansible.py` and `packaging/Containerfile.ansible-exec`.

## Installation

The installer ships as a compiled binary plus a Hauler image tarball; the
admin server needs podman (or docker), not Python.

```bash
# Build host (see packaging/)
./packaging/build_binary.sh
./packaging/build_hauler_image.sh --hauler-binary /path/to/hauler
./packaging/build_ansible_image.sh --push-to <registry>   # then add it to the haul

# Admin server (RHEL 9): binary and Hauler tarball in the same directory
./platform-installer-<version> --help
```

For development: `python -m venv .venv && .venv/bin/pip install -e '.[dev]'`,
then `.venv/bin/pytest`.

## Prerequisites

| Item | Where |
|------|-------|
| `platform-config.yaml` | Working directory (start from `platform-config.yaml.example`) |
| `platform-manifest.yaml` | Same directory as config |
| Rancher Hauler bundle (all staged assets, including the Ansible execution image) | `.tar.zst` file, passed via `--haul-path` on `deploy`/`preflight` |
| Hauler image tarball (`platform-hauler-<version>-container.tar.gz` + `.sha256`) | Next to the installer binary; loaded automatically |
| podman (preferred) or docker | Admin server |
| Internal CA cert + key | `global.tls.*` paths |
| SSH key pair | `global.ssh.*` paths |
| Merged pull secret | `global.pull_secret_path` |
| `VAULT_ROLE_ID` / `VAULT_SECRET_ID` env vars | Hub Vault only, used after `hub_services`. The admin host's local Vault credentials are written by `local/configure_vault.yml` |

## Usage

### Validate config without deploying

```bash
platform-installer validate --config platform-config.yaml
```

### Run full deployment

```bash
platform-installer deploy --config platform-config.yaml --haul-path ./platform.tar.zst
```

Before any phase runs, `deploy` loads the haul into Hauler, starts the Hauler
registry, Hauler fileserver and Vault containers on the admin host (if they
aren't already running), and then runs `local/configure_vault.yml` to
initialize/unseal Vault. This happens on every run until `hub_services` has
migrated Vault to the hub.

### Dry run (validates + generates vars, skips Ansible)

```bash
platform-installer deploy --config platform-config.yaml --haul-path ./platform.tar.zst --dry-run
```

A dry run never writes to the state store and skips health checks.

### Progress UI, logs and CI

At an interactive terminal, `deploy` and `preflight` show a live dashboard
(every phase and playbook, streaming Ansible output, and an Errors tab).
In CI, or when output is piped, they write a plain, streaming log instead.

| Option | Default | |
|---|---|---|
| `--ui auto\|tui\|plain` | `auto` | `auto` picks the dashboard only at an interactive terminal outside CI |
| `--log-file PATH` | `<state-dir>/install.log` | Appended to on every run; the durable record once the dashboard closes |
| `--junit PATH` | — | JUnit XML report, one test case per step |
| `--exit-when-done` | off | Close the dashboard as soon as the run finishes (otherwise press `q`) |

Exit code: `0` if every step succeeded or was skipped, `1` if anything failed or
didn't run, `130` on Ctrl-C.

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
platform-installer deploy --config platform-config.yaml --haul-path ./platform.tar.zst --from-phase management_services
```

### Run a single phase

```bash
platform-installer deploy --config platform-config.yaml --haul-path ./platform.tar.zst --phase preflight
```

### Run a range of phases

```bash
platform-installer deploy \
  --config platform-config.yaml \
  --haul-path ./platform.tar.zst \
  --from-phase hub_cluster \
  --to-phase hub_services
```

### Manual approval mode

Set `global.automation.fully_automated: false` in your config, then:

```bash
platform-installer deploy --config platform-config.yaml --haul-path ./platform.tar.zst --ui plain
# Installer will pause at each checkpoint defined in approval_checkpoints
# (pre_<phase>: before that phase; post_<phase>: before the phase after it)
```

Checkpoints can only be answered in `--ui plain` at an interactive terminal.
Under the dashboard, or without a terminal (CI), `deploy` refuses to start if
any checkpoint would fire. Pass `--auto-approve` or set `fully_automated: true`.

Or override for a single run:
```bash
platform-installer deploy --config platform-config.yaml --haul-path ./platform.tar.zst --auto-approve
```

### Full deployment report

```bash
platform-installer report --config platform-config.yaml
```

## Phase Overview

| Phase | What happens |
|-------|-------------|
| `local_services` | Every run, not state-tracked: configure/unseal the admin host's Vault |
| `preflight` | Validate config, check BMC/storage reachability, verify the Hauler bundle |
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

See the project structure section of `CLAUDE.md`, which is kept current.

## Adding a New Phase

1. Create a class in `installer/phases/base.py` extending `Phase`
2. Set `name`, `depends_on` and `success_message`
3. Implement `planned_playbooks()` returning `PlannedPlaybook`s, decided from
   config alone (the shared `run()` executes them, and the progress UI is
   built from the same list)
4. Optionally implement `health_check()`
5. Add the class to `ALL_PHASES` at the bottom of `base.py`
6. Create the corresponding playbook in `ansible/playbooks/`

## Adding a New Hub Service

1. Add an enable/disable + config block to `hub_services:` in `platform-config.yaml`
2. Add a Pydantic model in `installer/config/models.py` and field in `HubServicesConfig`
3. Add the service name to `HubServicesPhase._SERVICE_ORDER` in `base.py`
4. Create `ansible/playbooks/hub_services/<service_name>.yml`

## Ansible Collections (Disconnected)

Collections are never installed on the admin server or at deploy time. They
are declared in `ansible/collections/requirements.yml`, staged as tarballs by
`scripts/stage_collections.sh`, and baked into the Ansible execution image by
`packaging/build_ansible_image.sh`. See `CLAUDE.md` for the required list.
