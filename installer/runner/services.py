"""
installer/runner/services.py

Containers the installer runs on the admin host, alongside the per-playbook
Ansible execution containers, from the start of a deploy until hub_services
has migrated their data to the hub cluster:

  platform-hauler-registry    `hauler store serve registry`   — every image in the haul
  platform-hauler-fileserver  `hauler store serve fileserver` — every file in the haul
                              (ISOs etc.), reachable from BMCs/ESXi hosts
  platform-vault              HashiCorp Vault, pulled from the Hauler registry

The Hauler image is the only image shipped next to the installer binary
(auto-loaded like any colocated tarball, see ensure_image_loaded()).
Everything else, including the Ansible execution image, lives in the haul
and is pulled from Hauler's registry once it's up.

ensure_running() runs on every deploy, before the progress UI starts, and
is idempotent: the haul is loaded into the store once, and containers are
only (re)started if they aren't running. Container state isn't phase state,
so this deliberately lives outside the state store: after an admin-host
reboot or a resume, the next deploy brings everything back.

Python only starts the Vault server. Initializing, unsealing and AppRole
setup are done by ansible/playbooks/local/configure_vault.yml, which runs at
the start of every deploy (see cli.make_install()).
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests
from rich.console import Console

from installer.runner.ansible import detect_container_runtime, ensure_image_loaded

if TYPE_CHECKING:
    from installer.config.models import LocalServicesConfig

# Only for ensure_running(), which the CLI runs before the progress UI starts.
console = Console()

HAULER_REGISTRY   = "platform-hauler-registry"
HAULER_FILESERVER = "platform-hauler-fileserver"
VAULT             = "platform-vault"
CONTAINERS        = (HAULER_REGISTRY, HAULER_FILESERVER, VAULT)

#: Hauler's working directory inside its containers. The store, plus the
#: registry/ and fileserver/ directories `hauler store serve` creates in its
#: working directory, live here on the host: <state-dir>/local-services/hauler.
HAULER_WORKDIR = "/var/lib/hauler"
HAULER_STORE   = f"{HAULER_WORKDIR}/store"

#: How long to wait for a freshly started service to answer.
STARTUP_TIMEOUT_SECONDS = 600


def hauler_ref(source: str) -> str:
    """
    The reference an image is served under by Hauler's registry: the source
    reference without its registry host (docker.io/hashicorp/vault:1.16.3 →
    hashicorp/vault:1.16.3). Check against `hauler store info` for your haul.
    """
    first, sep, rest = source.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        return rest
    return source


class LocalServices:
    """
    Parameters
    ----------
    state_dir:
        The deploy's --state-dir. Hauler's store and Vault's data live under
        <state_dir>/local-services/, and relative credential paths resolve
        against it.

    haul_path:
        The --haul-path bundle, loaded into Hauler's store on first run.

    config:
        platform-config.yaml's local_services section.

    hauler_image:
        Tag of the Hauler image, auto-loaded from a colocated tarball.

    exec_image / vault_image:
        References as served by Hauler's registry. Pulled from it and tagged
        locally under the same reference, so `podman run <ref>` finds them
        without a registry.
    """

    def __init__(
        self,
        state_dir:         Path,
        haul_path:         Path,
        config:            "LocalServicesConfig",
        hauler_image:      str,
        exec_image:        str,
        vault_image:       str,
        container_runtime: str | None = None,
    ):
        self.state_dir    = Path(state_dir).resolve()
        self.haul_path    = Path(haul_path).resolve()
        self.config       = config
        self.hauler_image = hauler_image
        self.exec_image   = exec_image
        self.vault_image  = vault_image
        self.root         = self.state_dir / "local-services"
        self.hauler_dir   = self.root / "hauler"
        self.vault_dir    = self.root / "vault"
        # Resolved lazily — a dry run never needs podman/docker.
        self._container_runtime_override = container_runtime
        self.runtime: str | None = None

    # ── Addresses and paths ────────────────────────────────────────────────────

    @staticmethod
    def _local_host(bind_address: str) -> str:
        """Address the admin host itself (and host-network containers) use."""
        return "127.0.0.1" if bind_address in ("0.0.0.0", "127.0.0.1") else bind_address

    @property
    def registry(self) -> str:
        h = self.config.hauler
        return f"{self._local_host(h.bind_address)}:{h.registry_port}"

    @property
    def fileserver_url(self) -> str | None:
        if not self.config.advertise_address:
            return None
        return f"http://{self.config.advertise_address}:{self.config.hauler.fileserver_port}"

    @property
    def vault_addr(self) -> str:
        v = self.config.vault
        return f"http://{self._local_host(v.bind_address)}:{v.port}"

    def _state_path(self, path: str) -> Path:
        p = Path(path)
        return (p if p.is_absolute() else self.state_dir / p).resolve()

    @property
    def init_output_path(self) -> Path:
        return self._state_path(self.config.vault.init_output_path)

    @property
    def approle_credentials_path(self) -> Path:
        return self._state_path(self.config.vault.approle_credentials_path)

    def credential_dirs(self) -> list[Path]:
        """
        Directories configure_vault.yml writes into. Created owner-only and
        bind-mounted read-write into the Ansible execution container.
        """
        dirs = {self.init_output_path.parent, self.approle_credentials_path.parent}
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return sorted(dirs)

    def ansible_vars(self) -> dict[str, Any]:
        return {
            "platform_hauler": {
                "registry":       self.registry,
                "fileserver_url": self.fileserver_url,
            },
            "platform_local_vault": {
                "addr":                     self.vault_addr,
                "init_output_path":         str(self.init_output_path),
                "approle_credentials_path": str(self.approle_credentials_path),
            },
        }

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def ensure_running(self) -> None:
        """Bring Hauler and Vault up and pull the images the deploy needs. Idempotent."""
        self.runtime = self.runtime or detect_container_runtime(self._container_runtime_override)
        ensure_image_loaded(self.hauler_image, self.runtime, smoke_cmd=["hauler", "version"])

        if self._load_haul():
            # The serve containers copy the store at startup: restart them
            # so they pick up the newly loaded haul.
            for name in (HAULER_REGISTRY, HAULER_FILESERVER):
                self._rm(name)

        self._ensure_container(HAULER_REGISTRY, self._hauler_serve_args("registry", self.config.hauler.registry_port))
        self._ensure_container(HAULER_FILESERVER, self._hauler_serve_args("fileserver", self.config.hauler.fileserver_port))
        self._wait_for(f"http://{self.registry}/v2/", "Hauler registry", any_status=False)

        self._pull_from_hauler(self.exec_image)
        self._pull_from_hauler(self.vault_image)

        self._ensure_container(VAULT, self._vault_args())
        # Any answer means Vault is up: 501 (not initialized) and 503
        # (sealed) are expected until configure_vault.yml has run.
        self._wait_for(f"{self.vault_addr}/v1/sys/health", "Vault", any_status=True)

    def stop(self) -> None:
        """Remove the containers. The store and Vault's data stay under the state dir."""
        self.runtime = self.runtime or detect_container_runtime(self._container_runtime_override)
        for name in CONTAINERS:
            self._rm(name)

    # ── Internal ───────────────────────────────────────────────────────────────

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        assert self.runtime is not None
        proc = subprocess.run([self.runtime, *args], capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise RuntimeError(f"{self.runtime} {' '.join(args)} failed:\n{proc.stderr}")
        return proc

    def _rm(self, name: str) -> None:
        self._run("rm", "--force", name, check=False)

    def _load_haul(self) -> bool:
        """
        `hauler store load` the haul, unless this exact file was loaded
        already. Returns True if it loaded.
        """
        st     = self.haul_path.stat()
        marker = self.hauler_dir / "loaded.json"
        stamp  = {"path": str(self.haul_path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}
        if marker.exists() and json.loads(marker.read_text()) == stamp:
            return False

        (self.hauler_dir / "tmp").mkdir(parents=True, exist_ok=True)
        console.print(f"  [cyan]Loading {self.haul_path.name} into the Hauler store …[/cyan]")
        haul_in_container = f"/haul/{self.haul_path.name}"
        self._run(
            "run", "--rm",
            "-v", f"{self.haul_path}:{haul_in_container}:ro,z",
            *self._hauler_volume_args(),
            "-e", f"TMPDIR={HAULER_WORKDIR}/tmp",
            self.hauler_image,
            "hauler", "store", "load", "--store", HAULER_STORE, "--filename", haul_in_container,
        )
        marker.write_text(json.dumps(stamp))
        console.print(f"  [green]✔ Loaded {self.haul_path.name}[/green]")
        return True

    def _hauler_volume_args(self) -> list[str]:
        # Lowercase z: the store is shared by the load and both serve containers.
        return ["-v", f"{self.hauler_dir}:{HAULER_WORKDIR}:z", "-w", HAULER_WORKDIR]

    def _hauler_serve_args(self, kind: str, port: int) -> list[str]:
        return [
            "-p", f"{self.config.hauler.bind_address}:{port}:{port}",
            *self._hauler_volume_args(),
            self.hauler_image,
            "hauler", "store", "serve", kind, "--store", HAULER_STORE, "--port", str(port),
        ]

    def _vault_args(self) -> list[str]:
        v = self.config.vault
        data_dir = self.vault_dir / "file"
        data_dir.mkdir(parents=True, exist_ok=True)
        # Passed as VAULT_LOCAL_CONFIG (the image's entrypoint writes it into
        # /vault/config) rather than a host file: the entrypoint chowns
        # /vault/config to the vault user, which under rootless podman maps
        # to a subuid the installer could no longer write to.
        vault_config = {
            "ui":            True,
            "disable_mlock": True,
            "api_addr":      self.vault_addr,
            "storage":       {"file": {"path": "/vault/file"}},
            "listener":      [{"tcp": {"address": "0.0.0.0:8200", "tls_disable": True}}],
        }
        return [
            "-p", f"{v.bind_address}:{v.port}:8200",
            "-v", f"{data_dir}:/vault/file:Z",
            "-e", f"VAULT_LOCAL_CONFIG={json.dumps(vault_config)}",
            # No setcap on the vault binary: mlock is disabled above, and
            # rootless podman can't grant IPC_LOCK anyway.
            "-e", "SKIP_SETCAP=true",
            self.vault_image, "server",
        ]

    def _ensure_container(self, name: str, run_args: list[str]) -> None:
        """Start `name` unless it's already running. A stopped one is replaced."""
        state = self._run("inspect", "--format", "{{.State.Running}}", name, check=False)
        if state.returncode == 0 and state.stdout.strip() == "true":
            return
        self._rm(name)
        console.print(f"  [cyan]Starting {name} …[/cyan]")
        self._run("run", "--detach", "--name", name, *run_args)

    def _pull_from_hauler(self, ref: str) -> None:
        if self._run("image", "inspect", ref, check=False).returncode == 0:
            return
        console.print(f"  [cyan]Pulling {ref} from the Hauler registry …[/cyan]")
        source = f"{self.registry}/{ref}"
        # Hauler serves plain HTTP. Docker already treats 127.0.0.0/8 as
        # an insecure registry; podman needs to be told.
        tls = ["--tls-verify=false"] if self.runtime == "podman" else []
        self._run("pull", *tls, source)
        self._run("tag", source, ref)

    def _wait_for(self, url: str, what: str, *, any_status: bool) -> None:
        deadline = time.time() + STARTUP_TIMEOUT_SECONDS
        while time.time() < deadline:
            try:
                resp = requests.get(url, timeout=5)
                if any_status or resp.status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(2)
        raise RuntimeError(f"{what} did not come up at {url} within {STARTUP_TIMEOUT_SECONDS}s")
