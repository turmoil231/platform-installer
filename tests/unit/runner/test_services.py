"""
tests/unit/runner/test_services.py

LocalServices brings up Hauler and Vault on the admin host idempotently:
the haul is loaded once, containers start only if they aren't running, and
images are pulled from Hauler's registry and tagged under their haul reference.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from installer.config.models import LocalServicesConfig
from installer.runner import services as services_mod
from installer.runner.services import (
    HAULER_FILESERVER, HAULER_REGISTRY, VAULT, LocalServices, hauler_ref,
)


class FakeRuntime:
    """Stands in for `podman`: tracks running containers and local images."""

    def __init__(self):
        self.calls:   list[list[str]] = []
        self.running: set[str] = set()
        self.images:  set[str] = set()

    def __call__(self, cmd, capture_output=True, text=True):
        args = cmd[1:]
        self.calls.append(args)
        rc, out = 0, ""
        if args[0] == "inspect":
            out = "true" if args[-1] in self.running else ""
            rc = 0 if args[-1] in self.running else 1
        elif args[:2] == ["image", "inspect"]:
            rc = 0 if args[2] in self.images else 1
        elif args[0] == "run" and "--detach" in args:
            self.running.add(args[args.index("--name") + 1])
        elif args[0] == "rm":
            self.running.discard(args[-1])
        elif args[0] == "tag":
            self.images.add(args[2])
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")

    def of(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


@pytest.fixture
def runtime(monkeypatch):
    fake = FakeRuntime()
    monkeypatch.setattr(services_mod.subprocess, "run", fake)
    monkeypatch.setattr(services_mod, "detect_container_runtime", lambda preferred: preferred)
    monkeypatch.setattr(services_mod, "ensure_image_loaded", lambda *a, **k: None)
    monkeypatch.setattr(services_mod.requests, "get", lambda url, timeout: SimpleNamespace(status_code=200))
    return fake


def make_services(tmp_path: Path, **config) -> LocalServices:
    haul = tmp_path / "haul.tar.zst"
    if not haul.exists():
        haul.write_bytes(b"haul")
    return LocalServices(
        state_dir=tmp_path / "state", haul_path=haul,
        config=LocalServicesConfig(**config),
        hauler_image="platform-hauler:test",
        exec_image="platform-ansible-exec:test",
        vault_image="hashicorp/vault:1.16.3",
        container_runtime="podman",
    )


@pytest.mark.parametrize("source, expected", [
    ("docker.io/hashicorp/vault:1.16.3",           "hashicorp/vault:1.16.3"),
    ("registry.example.internal:5000/platform/x:1", "platform/x:1"),
    ("localhost/foo:1",                            "foo:1"),
    ("hashicorp/vault:1.16.3",                     "hashicorp/vault:1.16.3"),
    ("vault:1.16.3",                               "vault:1.16.3"),
])
def test_hauler_ref_strips_the_registry_host(source, expected):
    assert hauler_ref(source) == expected


def test_first_run_loads_haul_starts_everything_and_pulls_images(tmp_path, runtime):
    svc = make_services(tmp_path)
    svc.ensure_running()

    load, = [c for c in runtime.of("run", "--rm") if "load" in c]
    assert load[-4:] == ["--store", "/var/lib/hauler/store", "--filename", "/haul/haul.tar.zst"]
    assert runtime.running == {HAULER_REGISTRY, HAULER_FILESERVER, VAULT}
    assert runtime.of("pull") == [
        ["pull", "--tls-verify=false", "127.0.0.1:5000/platform-ansible-exec:test"],
        ["pull", "--tls-verify=false", "127.0.0.1:5000/hashicorp/vault:1.16.3"],
    ]
    assert {"platform-ansible-exec:test", "hashicorp/vault:1.16.3"} <= runtime.images

    vault_run, = [c for c in runtime.of("run", "--detach") if VAULT in c]
    assert "127.0.0.1:8200:8200" in vault_run
    config_env = next(a for a in vault_run if a.startswith("VAULT_LOCAL_CONFIG="))
    assert json.loads(config_env.split("=", 1)[1])["storage"] == {"file": {"path": "/vault/file"}}


def test_second_run_is_a_no_op(tmp_path, runtime):
    svc = make_services(tmp_path)
    svc.ensure_running()
    runtime.calls.clear()

    make_services(tmp_path).ensure_running()

    assert runtime.of("run") == []
    assert runtime.of("pull") == []


def test_stopped_containers_are_restarted_without_reloading(tmp_path, runtime):
    make_services(tmp_path).ensure_running()
    runtime.running.clear()  # e.g. admin host rebooted
    runtime.calls.clear()

    make_services(tmp_path).ensure_running()

    assert not [c for c in runtime.of("run", "--rm") if "load" in c]
    assert runtime.running == {HAULER_REGISTRY, HAULER_FILESERVER, VAULT}


def test_a_changed_haul_is_reloaded_and_the_serve_containers_restarted(tmp_path, runtime):
    make_services(tmp_path).ensure_running()
    (tmp_path / "haul.tar.zst").write_bytes(b"a newer, bigger haul")
    runtime.calls.clear()

    make_services(tmp_path).ensure_running()

    assert [c for c in runtime.of("run", "--rm") if "load" in c]
    assert ["rm", "--force", HAULER_REGISTRY] in runtime.calls
    assert ["rm", "--force", HAULER_FILESERVER] in runtime.calls
    assert ["rm", "--force", VAULT] not in runtime.calls


def test_stop_removes_every_container(tmp_path, runtime):
    svc = make_services(tmp_path)
    svc.ensure_running()
    svc.stop()
    assert runtime.running == set()


def test_credential_paths_resolve_against_state_dir_and_are_exposed_to_ansible(tmp_path):
    svc = make_services(
        tmp_path, advertise_address="10.0.0.10",
        vault={"approle_credentials_path": "/srv/creds/approle.yml"},
    )
    state = (tmp_path / "state").resolve()

    assert svc.init_output_path == state / "vault-credentials" / "init.json"
    assert svc.approle_credentials_path == Path("/srv/creds/approle.yml")

    v = svc.ansible_vars()
    assert v["platform_local_vault"]["addr"] == "http://127.0.0.1:8200"
    assert v["platform_local_vault"]["init_output_path"] == str(svc.init_output_path)
    assert v["platform_hauler"] == {"registry": "127.0.0.1:5000", "fileserver_url": "http://10.0.0.10:8080"}


def test_credential_dirs_are_created_owner_only(tmp_path):
    svc = make_services(tmp_path)
    dirs = svc.credential_dirs()
    assert dirs == [(tmp_path / "state" / "vault-credentials").resolve()]
    assert dirs[0].stat().st_mode & 0o777 == 0o700
