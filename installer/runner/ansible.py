"""
installer/runner/ansible.py

Thin wrapper around ansible-runner that the phase classes use to execute
playbooks from external Ansible collections in separate git repos.

Collection resolution (disconnected):
  Collections are pre-staged as tarballs in assets.staging_root/collections/.
  The installer installs them into a per-run isolated directory at startup
  (CollectionManager.install()), then sets ANSIBLE_COLLECTIONS_PATH so every
  ansible-runner job finds them without any network access.

  Playbooks reference collection content with fully-qualified names:
    platform.vmware, platform.ocp, platform.storage, etc.

  The ansible/ directory in the installer repo contains only:
    - playbooks/*.yml  (thin orchestration playbooks — they import_role/include_tasks
                        from the external collections)
    - collections/requirements.yml  (the dependency manifest)
    - group_vars/generated/  (written by ConfigLoader)
    - inventory/generated/   (written by ConfigLoader)

  Nothing in ansible/ duplicates logic from the collection repos.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import ansible_runner
import yaml
from rich.console import Console

console = Console()


# ── Collection manager ─────────────────────────────────────────────────────────

class CollectionInstallError(RuntimeError):
    pass


class CollectionManager:
    """
    Installs pre-staged collection tarballs into an isolated directory so
    ansible-runner jobs can find them via ANSIBLE_COLLECTIONS_PATH.

    The install directory is per-run (inside state_dir) so upgrading a
    collection version for a re-run is as simple as clearing it and
    re-running install().

    Parameters
    ----------
    staged_collections_dir:
        Directory containing pre-staged .tar.gz collection tarballs.
        Typically: assets.staging_root/collections/
        Populated by scripts/stage_collections.sh before air-gap transfer.

    install_dir:
        Where tarballs are extracted for ansible-runner to consume.
        Typically: state_dir/collections/
        The installer creates this; it is NOT committed to git.

    requirements_file:
        ansible/collections/requirements.yml — used to validate that every
        declared dependency has a corresponding staged tarball.

    lock_file:
        collections.lock.yml — written by stage_collections.sh.
        Contains SHA-256 checksums used to verify tarball integrity before install.

    verify_checksums:
        If True, every tarball is verified against the lock file before install.
        Set False only in development with --dry-run.
    """

    def __init__(
        self,
        staged_collections_dir: str | Path,
        install_dir:            str | Path,
        requirements_file:      str | Path,
        lock_file:              str | Path | None = None,
        verify_checksums:       bool = True,
    ):
        self.staged_dir    = Path(staged_collections_dir)
        self.install_dir   = Path(install_dir)
        self.req_file      = Path(requirements_file)
        self.lock_file     = Path(lock_file) if lock_file else None
        self.verify        = verify_checksums

        self._lock: dict[str, dict[str, str]] = {}   # "namespace.name" → lock entry
        self._installed: set[str] = set()

    def install(self, force: bool = False) -> Path:
        """
        Install all collections from staged tarballs.
        Returns the install_dir path to be used as ANSIBLE_COLLECTIONS_PATH.
        Skips install if already done (unless force=True).
        """
        marker = self.install_dir / ".install_complete"
        if marker.exists() and not force:
            console.print(
                f"  [dim]Collections already installed at {self.install_dir} — skipping[/dim]"
            )
            return self.install_dir

        self.install_dir.mkdir(parents=True, exist_ok=True)

        if self.lock_file and self.lock_file.exists():
            self._load_lock()

        requirements = self._load_requirements()
        if not requirements:
            console.print("  [yellow]No collections declared in requirements.yml[/yellow]")
            return self.install_dir

        console.print(
            f"  [cyan]Installing {len(requirements)} collection(s) "
            f"from {self.staged_dir}[/cyan]"
        )

        for entry in requirements:
            self._install_one(entry)

        marker.touch()
        console.print(f"  [green]✔ All collections installed to {self.install_dir}[/green]")
        return self.install_dir

    def validate_staged_assets(self) -> list[str]:
        """
        Check that every collection in requirements.yml has a corresponding
        staged tarball.  Returns a list of error strings (empty = all good).
        """
        errors: list[str] = []
        requirements = self._load_requirements()
        if self.lock_file and self.lock_file.exists():
            self._load_lock()

        for entry in requirements:
            tarball = self._find_tarball(entry)
            if tarball is None:
                errors.append(
                    f"No staged tarball found for {entry.get('name')} "
                    f"(looked in {self.staged_dir})"
                )
            elif self.verify and self.lock_file:
                key = self._collection_key(entry)
                if key in self._lock:
                    expected = self._lock[key].get("sha256", "")
                    actual   = self._sha256(tarball)
                    if expected and actual != expected:
                        errors.append(
                            f"Checksum mismatch for {tarball.name}: "
                            f"expected {expected}, got {actual}"
                        )
        return errors

    # ── Internal ───────────────────────────────────────────────────────────────

    def _install_one(self, entry: dict[str, Any]) -> None:
        name    = entry.get("name", "")
        tarball = self._find_tarball(entry)

        if tarball is None:
            raise CollectionInstallError(
                f"Staged tarball not found for collection '{name}' in {self.staged_dir}.\n"
                f"Run scripts/stage_collections.sh to populate staged collections."
            )

        if self.verify and self.lock_file and self._lock:
            key = self._collection_key(entry)
            if key in self._lock:
                expected = self._lock[key].get("sha256", "")
                if expected:
                    actual = self._sha256(tarball)
                    if actual != expected:
                        raise CollectionInstallError(
                            f"Checksum verification failed for {tarball.name}:\n"
                            f"  Expected: {expected}\n"
                            f"  Actual:   {actual}"
                        )

        console.print(f"    Installing {tarball.name} …")

        # Use ansible-galaxy collection install against the local tarball.
        # This respects galaxy.yml metadata and places it in the correct
        # namespace/name subdirectory under install_dir.
        result = subprocess.run(
            [
                "ansible-galaxy", "collection", "install",
                str(tarball),
                "--collections-path", str(self.install_dir),
                "--force",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise CollectionInstallError(
                f"ansible-galaxy collection install failed for {tarball.name}:\n"
                f"{result.stderr}"
            )
        self._installed.add(name)

    def _find_tarball(self, entry: dict[str, Any]) -> Path | None:
        """
        Find the staged tarball for a requirements entry.
        Naming convention: <namespace>-<name>-<version>.tar.gz
        Version is optional in the search — picks the highest semver match
        if multiple exist (you should always stage exactly one version).
        """
        name = entry.get("name", "")
        if "." not in name:
            return None
        namespace, coll_name = name.split(".", 1)
        version = entry.get("version", "")

        if version:
            # Exact match first
            exact = self.staged_dir / f"{namespace}-{coll_name}-{version}.tar.gz"
            if exact.exists():
                return exact

        # Glob fallback — pick the only/latest match
        matches = sorted(self.staged_dir.glob(f"{namespace}-{coll_name}-*.tar.gz"))
        return matches[-1] if matches else None

    def _load_requirements(self) -> list[dict[str, Any]]:
        if not self.req_file.exists():
            return []
        with self.req_file.open() as fh:
            data = yaml.safe_load(fh) or {}
        return data.get("collections", [])

    def _load_lock(self) -> None:
        if not self.lock_file or not self.lock_file.exists():
            return
        with self.lock_file.open() as fh:
            data = yaml.safe_load(fh) or {}
        for entry in data.get("collections", []):
            key = f"{entry.get('namespace', '')}.{entry.get('name', '')}"
            self._lock[key] = entry

    @staticmethod
    def _collection_key(entry: dict[str, Any]) -> str:
        name = entry.get("name", "")
        return name if "." in name else ""

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()


# ── Playbook result ────────────────────────────────────────────────────────────

@dataclass
class PlaybookResult:
    rc:           int
    status:       str
    stats:        dict[str, Any] = field(default_factory=dict)
    failed_hosts: list[str]      = field(default_factory=list)
    stdout:       str            = ""

    @property
    def success(self) -> bool:
        return self.rc == 0 and self.status == "successful"


# ── Ansible runner wrapper ─────────────────────────────────────────────────────

class AnsibleRunner:
    """
    Executes Ansible playbooks via ansible-runner, wiring in pre-staged
    external collections via ANSIBLE_COLLECTIONS_PATH.

    Parameters
    ----------
    ansible_dir:
        Root of the ansible/ directory in the installer repo.
        Contains playbooks/, group_vars/generated/, inventory/generated/.
        Does NOT contain roles or collection content — those come from
        the external collection repos, pre-staged as tarballs.

    collections_install_dir:
        Path where CollectionManager extracted the collection tarballs.
        Set as ANSIBLE_COLLECTIONS_PATH for every job.

    artifacts_dir:
        Per-job ansible-runner artifact directory.

    extra_vars_files:
        Var files generated by ConfigLoader — passed as @file to every job.

    inventory_path:
        Generated static inventory YAML.

    vault_addr:
        Active Vault address (bootstrap during early phases, hub later).

    ansible_cfg_path:
        Optional path to a custom ansible.cfg.  If not provided, a minimal
        one is generated at runtime that sets collections_paths and disables
        host key checking (appropriate for a controlled deployment network).
    """

    def __init__(
        self,
        ansible_dir:              str | Path,
        collections_install_dir:  str | Path,
        artifacts_dir:            str | Path,
        extra_vars_files:         list[str | Path],
        inventory_path:           str | Path,
        vault_addr:               str,
        vault_role_id_env:        str = "VAULT_ROLE_ID",
        vault_secret_id_env:      str = "VAULT_SECRET_ID",
        max_retries:              int = 3,
        retry_delay:              int = 60,
        ansible_cfg_path:         str | Path | None = None,
        dry_run:                  bool = False,
    ):
        self.ansible_dir             = Path(ansible_dir).resolve()
        self.collections_install_dir = Path(collections_install_dir).resolve()
        self.artifacts_dir           = Path(artifacts_dir).resolve()
        self.extra_vars_files        = [Path(f).resolve() for f in extra_vars_files]
        self.inventory_path          = Path(inventory_path).resolve()
        self.vault_addr              = vault_addr
        self.vault_role_id_env       = vault_role_id_env
        self.vault_secret_id_env     = vault_secret_id_env
        self.max_retries             = max_retries
        self.retry_delay             = retry_delay
        self.dry_run                 = dry_run
        self._ansible_cfg_path       = (
            Path(ansible_cfg_path).resolve() if ansible_cfg_path else None
        )

    def switch_vault_addr(self, new_addr: str) -> None:
        """
        Switch the active Vault address.  Called by hub_services phase after
        Vault migration from bootstrap → hub is confirmed healthy.
        """
        console.print(
            f"  [cyan]Switching Vault address: {self.vault_addr} → {new_addr}[/cyan]"
        )
        self.vault_addr = new_addr

    def run_playbook(
        self,
        playbook:   str,
        extra_vars: dict[str, Any] | None = None,
        tags:       list[str] | None = None,
        limit:      str | None = None,
        retries:    int | None = None,
    ) -> PlaybookResult:
        """
        Run a playbook from ansible/playbooks/<playbook>.
        The playbook may import_role or include_tasks from any installed
        collection via its fully-qualified collection name (FQCN).
        """
        playbook_path = self.ansible_dir / "playbooks" / playbook
        if not playbook_path.exists():
            raise FileNotFoundError(
                f"Playbook not found: {playbook_path}\n"
                f"Ensure the playbook exists in ansible/playbooks/"
            )

        max_attempts = (retries if retries is not None else self.max_retries) + 1
        last_result: PlaybookResult | None = None

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                console.print(
                    f"  [yellow]Retry {attempt - 1}/{max_attempts - 1} "
                    f"in {self.retry_delay}s …[/yellow]"
                )
                time.sleep(self.retry_delay)

            label = f"ansible-playbook {playbook}"
            if max_attempts > 1:
                label += f" (attempt {attempt}/{max_attempts})"
            if self.dry_run:
                label += " [DRY RUN]"

            console.print(f"  [bold cyan]▶ {label}[/bold cyan]")

            if self.dry_run:
                console.print("  [dim]Dry-run — skipping execution[/dim]")
                return PlaybookResult(rc=0, status="successful")

            result = self._execute(playbook_path, extra_vars or {}, tags, limit)
            last_result = result

            if result.success:
                console.print(f"  [green]✔ {playbook} completed[/green]")
                return result

            console.print(
                f"  [red]✘ {playbook} failed (rc={result.rc}, status={result.status})[/red]"
            )
            if result.failed_hosts:
                console.print(
                    f"  [red]  Failed hosts: {', '.join(result.failed_hosts)}[/red]"
                )
            if attempt == max_attempts:
                break

        return last_result  # type: ignore[return-value]

    # ── Internal ───────────────────────────────────────────────────────────────

    def _execute(
        self,
        playbook_path: Path,
        extra_vars:    dict[str, Any],
        tags:          list[str] | None,
        limit:         str | None,
    ) -> PlaybookResult:

        # Build extra-vars: @file references first, then inline dict
        ev_parts = [f"@{f}" for f in self.extra_vars_files]
        if extra_vars:
            ev_parts.append(json.dumps(extra_vars))

        cmdline_args: list[str] = []
        if tags:
            cmdline_args += ["--tags", ",".join(tags)]
        if limit:
            cmdline_args += ["--limit", limit]

        # ANSIBLE_COLLECTIONS_PATH is the key env var that points ansible-runner
        # (and therefore every playbook import_role / include_tasks call) at the
        # pre-installed collection tarballs in the isolated install directory.
        env_vars = {
            **os.environ,
            "ANSIBLE_COLLECTIONS_PATH":         str(self.collections_install_dir),
            "ANSIBLE_ROLES_PATH":               str(self.collections_install_dir / "ansible_collections"),
            "VAULT_ADDR":                        self.vault_addr,
            "VAULT_ROLE_ID":                     os.environ.get(self.vault_role_id_env, ""),
            "VAULT_SECRET_ID":                   os.environ.get(self.vault_secret_id_env, ""),
            "ANSIBLE_FORCE_COLOR":               "1",
            "ANSIBLE_STDOUT_CALLBACK":           "yaml",
            "ANSIBLE_HOST_KEY_CHECKING":         "False",
            "ANSIBLE_SSH_RETRIES":               "5",
            "ANSIBLE_TIMEOUT":                   "30",
            # Disable Galaxy calls — all content is local
            "ANSIBLE_GALAXY_SERVER_LIST":        "",
            "ANSIBLE_GALAXY_SERVER_TIMEOUT":     "1",
        }

        # Inject custom ansible.cfg if provided, otherwise generate a minimal one
        cfg_path = self._ansible_cfg_path or self._write_ansible_cfg()
        env_vars["ANSIBLE_CONFIG"] = str(cfg_path)

        collected_stdout: list[str] = []

        def event_handler(event: dict[str, Any]) -> None:
            line = event.get("stdout", "")
            if line:
                for ln in line.splitlines():
                    console.print(f"    {ln}", markup=False, highlight=False)
                collected_stdout.append(line)

        job_name   = playbook_path.stem
        job_dir    = self.artifacts_dir / job_name
        job_dir.mkdir(parents=True, exist_ok=True)

        runner_obj = ansible_runner.run(
            playbook      = str(playbook_path),
            inventory     = str(self.inventory_path),
            extravars     = " ".join(ev_parts) if ev_parts else None,
            project_dir   = str(self.ansible_dir),
            artifact_dir  = str(job_dir),
            cmdline       = " ".join(cmdline_args) if cmdline_args else None,
            envvars       = env_vars,
            event_handler = event_handler,
            quiet         = True,
        )

        stats      = runner_obj.stats or {}
        failed     = list(stats.get("failures", {}).keys())

        return PlaybookResult(
            rc           = runner_obj.rc,
            status       = runner_obj.status,
            stats        = stats,
            failed_hosts = failed,
            stdout       = "\n".join(collected_stdout),
        )

    def _write_ansible_cfg(self) -> Path:
        """
        Write a minimal ansible.cfg that pins ANSIBLE_COLLECTIONS_PATH and
        disables features inappropriate for a controlled deployment network.
        Written to the ansible/ dir so ansible-runner picks it up automatically.
        """
        cfg_path = self.ansible_dir / "ansible.cfg"
        cfg_content = f"""\
[defaults]
collections_paths     = {self.collections_install_dir}
inventory             = {self.inventory_path}
host_key_checking     = False
retry_files_enabled   = False
stdout_callback       = yaml
callbacks_enabled     = timer, profile_tasks
timeout               = 30
forks                 = 20
gathering             = smart
fact_caching          = jsonfile
fact_caching_connection = /tmp/ansible_facts_cache
fact_caching_timeout  = 7200

[privilege_escalation]
become        = True
become_method = sudo
become_user   = root

[ssh_connection]
ssh_args        = -o ControlMaster=auto -o ControlPersist=60s -o StrictHostKeyChecking=no
pipelining      = True
retries         = 5

[galaxy]
server_list =
# Empty — no Galaxy servers in disconnected mode.
# All collections are installed locally from staged tarballs.
"""
        cfg_path.write_text(cfg_content)
        return cfg_path
