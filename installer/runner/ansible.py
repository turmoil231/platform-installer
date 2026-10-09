"""
installer/runner/ansible.py

Thin wrapper around ansible-runner that the phase classes use to execute
playbooks from external Ansible collections in separate git repos.

Execution model (containerized):
  The compiled platform-installer binary runs natively on the admin host.
  Ansible itself never runs on the host — every playbook is executed inside
  a purpose-built "Ansible execution image" (ansible-core + all collections
  preloaded at build time, see packaging/Containerfile.ansible-exec) via
  ansible-runner's container/process-isolation executor
  (process_isolation=True, container_image=...). ansible-runner shells out to
  `podman run --rm <image> ansible-playbook ...` (or docker) and streams
  events back the same way it does for local execution.

  Collections are baked into the execution image at build time
  (scripts/stage_collections.sh + packaging/build_ansible_image.sh). Nothing
  on the host installs or references collections at deploy time.

  The ansible/ directory in the installer repo contains only:
    - playbooks/*.yml  (thin orchestration playbooks — they import_role/include_tasks
                        from the external collections)
    - collections/requirements.yml  (the dependency manifest, used at image build time)

  Playbooks reference collection content with fully-qualified names:
    platform.vmware, platform.ocp, platform.storage, etc.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import ansible_runner
import yaml
from rich.console import Console

from installer.tui.reporter import strip_ansi

if TYPE_CHECKING:
    from installer.tui.reporter import Reporter

# Only for ensure_image_loaded(), which the CLI runs before the progress UI
# starts. Everything during a run goes through AnsibleRunner.reporter.
console = Console()

#: Lines of playbook output used as the error detail when no failed task
#: result was captured (e.g. a syntax error, or the container failed to start).
FAILURE_TAIL_LINES = 40


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Container runtime detection ─────────────────────────────────────────────────

CONTAINER_RUNTIME_PREFERENCE = ("podman", "docker")


def detect_container_runtime(preferred: str | None = None) -> str:
    """
    Pick the container runtime used to launch the Ansible execution image.
    Honors an explicit override; otherwise prefers podman, falling back to
    docker (matches the environment assumptions in CLAUDE.md).
    """
    candidates = [preferred] if preferred else list(CONTAINER_RUNTIME_PREFERENCE)
    for candidate in candidates:
        if shutil.which(candidate):
            return candidate
    raise RuntimeError(
        f"No container runtime found (tried: {', '.join(candidates)}). "
        f"Install podman (preferred) or docker."
    )


def _binary_dir() -> Path:
    """
    Directory containing the running executable.  A colocated Ansible
    execution image tarball (<image-name>-<version>-container.tar.gz) is
    expected to live here for auto-loading.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def ensure_image_loaded(
    image_tag: str,
    runtime:   str,
    search_dir: Path | None = None,
) -> None:
    """
    Make sure `image_tag` is present in the local podman/docker image store.
    If it isn't, look for a colocated tarball next to the running binary,
    verify its checksum, load it, and smoke-test it.
    """
    inspect = subprocess.run(
        [runtime, "image", "inspect", image_tag],
        capture_output=True, text=True,
    )
    if inspect.returncode == 0:
        return

    console.print(f"  [yellow]Ansible execution image not found locally — loading {image_tag}[/yellow]")

    search_dir = search_dir or _binary_dir()
    image_name = image_tag.split(":", 1)[0]
    candidates = sorted(search_dir.glob(f"{image_name}-*-container.tar.gz"))
    if not candidates:
        raise RuntimeError(
            f"Image {image_tag!r} is not loaded and no tarball was found in {search_dir}.\n"
            f"Expected: {image_name}-<version>-container.tar.gz"
        )
    tarball = candidates[-1]

    checksum_file = tarball.with_suffix(tarball.suffix + ".sha256")
    if checksum_file.exists():
        expected = checksum_file.read_text().split()[0]
        actual = _sha256(tarball)
        if expected != actual:
            raise RuntimeError(
                f"Checksum verification failed for {tarball.name}:\n"
                f"  Expected: {expected}\n"
                f"  Actual:   {actual}"
            )
        console.print(f"  [dim]Checksum verified: {tarball.name}[/dim]")
    else:
        console.print(f"  [yellow]No .sha256 file found for {tarball.name} — skipping checksum verification[/yellow]")

    console.print(f"  [cyan]Loading {tarball.name} with {runtime} …[/cyan]")
    load = subprocess.run([runtime, "load", "-i", str(tarball)], capture_output=True, text=True)
    if load.returncode != 0:
        raise RuntimeError(f"{runtime} load failed:\n{load.stderr}")

    smoke = subprocess.run(
        [runtime, "run", "--rm", image_tag, "ansible-playbook", "--version"],
        capture_output=True, text=True,
    )
    if smoke.returncode != 0:
        raise RuntimeError(f"Image {image_tag} loaded but smoke test failed:\n{smoke.stderr}")

    console.print(f"  [green]✔ Loaded and verified {image_tag}[/green]")


# ── Playbook result ────────────────────────────────────────────────────────────

@dataclass
class PlaybookResult:
    rc:           int
    status:       str
    stats:        dict[str, Any] = field(default_factory=dict)
    failed_hosts: list[str]      = field(default_factory=list)
    stdout:       str            = ""
    #: The last failed task and its result, from runner_on_failed /
    #: runner_on_unreachable events. Empty if none was seen.
    last_failure: str            = ""

    @property
    def success(self) -> bool:
        return self.rc == 0 and self.status == "successful"


# ── Ansible runner wrapper ─────────────────────────────────────────────────────

class AnsibleRunner:
    """
    Executes Ansible playbooks inside the Ansible execution image via
    ansible-runner's container/process-isolation executor.

    Parameters
    ----------
    ansible_dir:
        Root of the ansible/ directory in the installer repo.
        Contains playbooks/ and collections/requirements.yml.
        Synced into private_data_dir/project before every run.

    private_data_dir:
        ansible-runner's private data directory. Mounted wholesale into the
        execution container at /runner — generated inventory and extra-vars
        files must live under here (see cli.py::_make_runner) so they're
        visible inside the container. Reused across every playbook run in
        a single `deploy` invocation.

    container_image:
        Tag of the preloaded Ansible execution image
        (e.g. platform-ansible-exec:2025.1.0).

    container_runtime:
        "podman" or "docker". Auto-detected (podman preferred) if not given.

    host_mounts:
        External, user-provided host paths that must be visible inside the
        execution container but are NOT copied into private_data_dir (SSH
        keypair, internal CA cert/key, pull secret, the Hauler haul bundle).
        Each entry is (path, read_only). Mounted at the identical path
        inside the container, so extravars values referencing these paths
        (e.g. global.ssh.private_key_path) stay valid on both sides with no
        translation needed.

    vault_addr:
        Active Vault address (bootstrap during early phases, hub later).

    reporter:
        Progress reporter (installer.tui.Reporter) that every playbook run
        reports its step start/finish/output to. Attached by the CLI once
        the progress UI has created it, before the first run_playbook().
    """

    def __init__(
        self,
        ansible_dir:          str | Path,
        private_data_dir:     str | Path,
        container_image:      str,
        vault_addr:           str,
        vault_role_id_env:    str = "VAULT_ROLE_ID",
        vault_secret_id_env:  str = "VAULT_SECRET_ID",
        container_runtime:    str | None = None,
        host_mounts:          list[tuple[str | Path, bool]] | None = None,
        max_retries:          int = 3,
        retry_delay:          int = 60,
        dry_run:              bool = False,
    ):
        self.ansible_dir         = Path(ansible_dir).resolve()
        self.private_data_dir    = Path(private_data_dir).resolve()
        self.private_data_dir.mkdir(parents=True, exist_ok=True)
        self.container_image     = container_image
        self.host_mounts         = [(Path(p).resolve(), ro) for p, ro in (host_mounts or [])]
        self.vault_addr          = vault_addr
        self.vault_role_id_env   = vault_role_id_env
        self.vault_secret_id_env = vault_secret_id_env
        self.max_retries         = max_retries
        self.retry_delay         = retry_delay
        self.dry_run             = dry_run
        self._image_ready        = False
        self.reporter: "Reporter | None" = None
        # Resolved lazily in ensure_ready() — a dry-run (config validation,
        # --dry-run deploy) should never require podman/docker to be present.
        self._container_runtime_override = container_runtime
        self.container_runtime: str | None = None

    def ensure_ready(self) -> None:
        """Verify (and auto-load if needed) the Ansible execution image. Idempotent."""
        if self._image_ready or self.dry_run:
            return
        self.container_runtime = self.container_runtime or detect_container_runtime(
            self._container_runtime_override
        )
        ensure_image_loaded(self.container_image, self.container_runtime)
        self._image_ready = True

    def switch_vault_addr(self, new_addr: str) -> None:
        """
        Switch the active Vault address.  Called by hub_services phase after
        Vault migration from bootstrap → hub is confirmed healthy.
        """
        self._reporter().log(f"Switching Vault address: {self.vault_addr} → {new_addr}")
        self.vault_addr = new_addr

    def run_playbook(
        self,
        playbook:   str,
        extra_vars: dict[str, Any] | None = None,
        tags:       list[str] | None = None,
        limit:      str | None = None,
        retries:    int | None = None,
        *,
        step_id:    str,
    ) -> PlaybookResult:
        """
        Run a playbook from ansible/playbooks/<playbook> inside the Ansible
        execution image. The playbook may import_role or include_tasks from
        any collection baked into that image via its fully-qualified
        collection name (FQCN).

        Reports as a single progress step, `step_id`: retries are output
        lines within it, not new steps. An exception still finishes the step
        (as failed) before propagating.
        """
        reporter = self._reporter()

        playbook_src = self.ansible_dir / "playbooks" / playbook
        if not playbook_src.exists():
            message = (
                f"Playbook not found: {playbook_src}\n"
                f"Ensure the playbook exists in ansible/playbooks/"
            )
            reporter.start_step(step_id)
            reporter.finish_step(step_id, success=False, error=message)
            raise FileNotFoundError(message)

        if self.dry_run:
            reporter.skip_step(step_id, reason="dry run")
            return PlaybookResult(rc=0, status="successful")

        reporter.start_step(step_id)
        try:
            result = self._run_with_retries(playbook, extra_vars or {}, tags, limit, retries, step_id)
        except Exception as exc:
            reporter.finish_step(step_id, success=False, error=f"{type(exc).__name__}: {exc}")
            raise

        reporter.finish_step(
            step_id,
            success=result.success,
            error=None if result.success else self._failure_detail(result),
        )
        return result

    # ── Internal ───────────────────────────────────────────────────────────────

    def _reporter(self) -> "Reporter":
        if self.reporter is None:
            raise RuntimeError("AnsibleRunner.reporter must be attached before running playbooks")
        return self.reporter

    def _run_with_retries(
        self,
        playbook:   str,
        extra_vars: dict[str, Any],
        tags:       list[str] | None,
        limit:      str | None,
        retries:    int | None,
        step_id:    str,
    ) -> PlaybookResult:
        reporter     = self._reporter()
        max_attempts = (retries if retries is not None else self.max_retries) + 1
        last_result: PlaybookResult | None = None

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                reporter.append_output(
                    step_id,
                    f"Retry {attempt - 1}/{max_attempts - 1} in {self.retry_delay}s …",
                )
                time.sleep(self.retry_delay)

            label = f"ansible-playbook {playbook}"
            if max_attempts > 1:
                label += f" (attempt {attempt}/{max_attempts})"
            reporter.append_output(step_id, f"▶ {label}")

            result = self._execute(playbook, extra_vars, tags, limit, step_id)
            last_result = result
            if result.success:
                return result

            reporter.append_output(
                step_id, f"✘ {playbook} failed (rc={result.rc}, status={result.status})"
            )
            if result.failed_hosts:
                reporter.append_output(step_id, f"  Failed hosts: {', '.join(result.failed_hosts)}")

        return last_result  # type: ignore[return-value]

    @staticmethod
    def _failure_detail(result: PlaybookResult) -> str:
        """Error text for the progress UI's Errors tab and the JUnit failure body."""
        lines = [f"rc={result.rc} status={result.status}"]
        if result.failed_hosts:
            lines.append(f"Failed hosts: {', '.join(result.failed_hosts)}")
        if result.last_failure:
            lines.append(result.last_failure)
        elif result.stdout:
            tail = strip_ansi(result.stdout).splitlines()[-FAILURE_TAIL_LINES:]
            lines.append(f"Last {len(tail)} lines of output:")
            lines.extend(tail)
        return "\n".join(lines)

    def _sync_project(self) -> None:
        """
        Refresh private_data_dir/project from ansible_dir. Full replace
        (not a merge) so playbooks removed from ansible_dir don't linger.
        """
        project_dir = self.private_data_dir / "project"
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.copytree(self.ansible_dir, project_dir)

    def _load_extra_vars(self) -> dict[str, Any]:
        """Merge every generated var file under private_data_dir/vars/."""
        merged: dict[str, Any] = {}
        vars_dir = self.private_data_dir / "vars"
        for f in sorted(vars_dir.glob("**/*.yml")):
            with f.open() as fh:
                merged.update(yaml.safe_load(fh) or {})
        return merged

    def _clear_stale_env_artifacts(self) -> None:
        """
        ansible-runner writes extravars/envvars/cmdline/etc. to
        private_data_dir/env/<name> and SILENTLY REUSES them on a later run
        against the same private_data_dir if the corresponding kwarg isn't
        passed (see ansible_runner.utils.dump_artifacts). Since one
        AnsibleRunner instance runs many playbooks against one shared
        private_data_dir, stale files here would leak a previous playbook's
        tags/limit/extravars into the next run. Clear them before every run.
        """
        env_dir = self.private_data_dir / "env"
        for name in ("extravars", "envvars", "cmdline", "passwords", "settings", "ssh_key"):
            (env_dir / name).unlink(missing_ok=True)

    def _execute(
        self,
        playbook:   str,
        extra_vars: dict[str, Any],
        tags:       list[str] | None,
        limit:      str | None,
        step_id:    str,
    ) -> PlaybookResult:
        self.ensure_ready()
        self._sync_project()
        self._clear_stale_env_artifacts()

        merged_vars = self._load_extra_vars()
        merged_vars.update(extra_vars)

        cmdline_args: list[str] = []
        if tags:
            cmdline_args += ["--tags", ",".join(tags)]
        if limit:
            cmdline_args += ["--limit", limit]

        # Mount external, user-provided paths at the identical path inside the
        # container so extravars referencing them (e.g. SSH key paths) need
        # no host↔container path translation.
        container_volume_mounts = [
            f"{path}:{path}:{'ro' if read_only else 'rw'},Z"
            for path, read_only in self.host_mounts
        ]

        env_vars = {
            "VAULT_ADDR":                self.vault_addr,
            "VAULT_ROLE_ID":             os.environ.get(self.vault_role_id_env, ""),
            "VAULT_SECRET_ID":           os.environ.get(self.vault_secret_id_env, ""),
            "ANSIBLE_FORCE_COLOR":       "1",
            # "default", not "yaml": the yaml callback isn't part of
            # ansible-core — ansible-core redirects the bare name to
            # community.general.yaml, so it silently breaks if that
            # collection is ever missing from the execution image. "default"
            # is always available with zero collections installed.
            "ANSIBLE_STDOUT_CALLBACK":   "default",
            "ANSIBLE_HOST_KEY_CHECKING": "False",
            "ANSIBLE_SSH_RETRIES":       "5",
            "ANSIBLE_TIMEOUT":           "30",
            # ansible-runner's docker executor runs the container as the host
            # UID (--user=$(id -u)), with no matching /etc/passwd entry, so
            # $HOME defaults to "/" — unwritable, and ansible.cfg's local_tmp
            # default (~/.ansible/tmp) fails outright. /runner is the
            # bind-mounted private_data_dir, owned by that same host UID.
            "HOME":                      "/runner",
        }
        for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
            if proxy_var in os.environ:
                env_vars[proxy_var] = os.environ[proxy_var]

        reporter = self._reporter()
        collected_stdout: list[str] = []
        last_failure: list[str] = []

        def event_handler(event: dict[str, Any]) -> None:
            line = event.get("stdout", "")
            if line:
                for ln in line.splitlines():
                    reporter.append_output(step_id, ln)
                collected_stdout.append(line)
            if event.get("event") in ("runner_on_failed", "runner_on_unreachable"):
                data = event.get("event_data", {})
                if not data.get("ignore_errors"):
                    last_failure[:] = [
                        f"Last failed task: {data.get('task', '?')} on {data.get('host', '?')}",
                        strip_ansi(json.dumps(data.get("res", {}), indent=2, default=str)),
                    ]

        job_name = Path(playbook).stem

        runner_obj = ansible_runner.run(
            # Relative to private_data_dir/project (container_workdir), NOT
            # an absolute host path — the container only sees /runner/project.
            playbook                      = f"playbooks/{playbook}",
            private_data_dir              = str(self.private_data_dir),
            extravars                     = merged_vars,
            cmdline                       = " ".join(cmdline_args) if cmdline_args else None,
            envvars                       = env_vars,
            event_handler                 = event_handler,
            quiet                         = True,
            ident                         = job_name,
            process_isolation             = True,
            process_isolation_executable  = self.container_runtime,
            container_image               = self.container_image,
            container_volume_mounts       = container_volume_mounts,
        )

        stats  = runner_obj.stats or {}
        failed = list(stats.get("failures", {}).keys())

        return PlaybookResult(
            rc           = runner_obj.rc,
            status       = runner_obj.status,
            stats        = stats,
            failed_hosts = failed,
            stdout       = "\n".join(collected_stdout),
            last_failure = "\n".join(last_failure),
        )
