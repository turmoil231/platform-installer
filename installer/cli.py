"""
installer/cli.py

Runs natively on the admin host as a compiled binary — no outer container.
Ansible itself executes inside a preloaded "Ansible execution image" via
ansible-runner's container executor (see installer/runner/ansible.py).
Collections are baked into that image ahead of time — the installer never
stages, validates, or otherwise concerns itself with collections at all.
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table
from rich import box

from installer.config.loader import ConfigLoader, ConfigValidationError
from installer.runner.ansible import AnsibleRunner
from installer.state.store import PhaseStatus, StateStore
from installer.phases.base import ALL_PHASES, PHASE_NAMES, Phase

console = Console()

# The platform release this installer build is intended to deploy — distinct
# from the installer's own version (below) and from any particular
# operator-provided platform-manifest.yaml's manifest_version, which just
# pins that one environment's component versions. Bump this when cutting an
# installer release against a new platform-manifest.yaml.example baseline.
PLATFORM_VERSION = "2025.1.0"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_config(config: str, manifest: str | None) -> ConfigLoader:
    try:
        return ConfigLoader(config, manifest).load()
    except (FileNotFoundError, ConfigValidationError) as exc:
        console.print(f"[bold red]CONFIG ERROR:[/bold red] {exc}")
        sys.exit(1)


def _ansible_dir() -> Path:
    """
    Locate the ansible/ directory (playbooks/, collections/requirements.yml).
    When compiled with PyInstaller (--add-data "ansible:ansible", see
    packaging/build_binary.sh), it's bundled into the executable and
    extracted at runtime under sys._MEIPASS — Path(__file__) instead
    resolves into that same temp extraction dir, not the real repo, so it
    can't be used to find sibling data directories in a frozen binary.
    """
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "ansible"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[1] / "ansible"


def _default_ansible_image() -> str:
    try:
        v = version("platform-installer")
    except PackageNotFoundError:
        v = "dev"
    return f"platform-ansible-exec:{v}"


def _build_host_mounts(loader: ConfigLoader) -> list[tuple[Path, bool]]:
    """
    External, user-provided paths that must be visible inside the Ansible
    execution container but live outside private_data_dir (see the
    AnsibleRunner docstring for why these are mounted rather than copied).
    """
    g = loader.config.global_  # type: ignore[union-attr]
    return [
        (Path(g.ssh.public_key_path),       True),
        (Path(g.ssh.private_key_path),      True),
        (Path(g.tls.internal_ca_cert_path), True),
        (Path(g.tls.internal_ca_key_path),  True),
        (Path(g.pull_secret_path),          True),
    ]


def _make_runner(
    loader:            ConfigLoader,
    state_dir:         Path,
    container_image:   str,
    container_runtime: str | None,
    haul_path:         Path,
    dry_run:           bool,
) -> AnsibleRunner:
    ansible_dir      = _ansible_dir()
    private_data_dir = state_dir / "ansible-pdd"

    loader.generate_ansible_vars(private_data_dir / "vars")
    loader.generate_ansible_inventory(private_data_dir / "inventory" / "hosts.yml")

    # Not config-derived (haul_path is a per-run CLI flag, not part of
    # platform-config.yaml) — written directly rather than through
    # ConfigLoader.generate_ansible_vars().
    vars_dir = private_data_dir / "vars"
    vars_dir.mkdir(parents=True, exist_ok=True)
    with (vars_dir / "haul.yml").open("w") as fh:
        yaml.dump({"platform_haul_path": str(haul_path)}, fh)

    automation = loader.config.global_.automation  # type: ignore[union-attr]
    secrets    = loader._raw_config.get("secrets", {}).get("vault", {})
    vault_addr = secrets.get("bootstrap_addr", "http://localhost:8200")

    host_mounts = _build_host_mounts(loader)
    host_mounts.append((haul_path, True))

    return AnsibleRunner(
        ansible_dir          = ansible_dir,
        private_data_dir     = private_data_dir,
        container_image      = container_image,
        container_runtime    = container_runtime,
        host_mounts          = host_mounts,
        vault_addr           = vault_addr,
        vault_role_id_env    = secrets.get("role_id_env",   "VAULT_ROLE_ID"),
        vault_secret_id_env  = secrets.get("secret_id_env", "VAULT_SECRET_ID"),
        max_retries          = automation.max_retries,
        retry_delay          = automation.retry_delay_seconds,
        dry_run              = dry_run,
    )


def _instantiate_phases(runner: AnsibleRunner, store: StateStore, loader: ConfigLoader) -> list[Phase]:
    vars_dir = runner.private_data_dir / "vars"
    merged: dict = {}
    for f in sorted(vars_dir.glob("**/*.yml")):
        with f.open() as fh:
            merged.update(yaml.safe_load(fh) or {})
    return [Cls(runner=runner, store=store, config_vars=merged) for Cls in ALL_PHASES]


def _print_phase_table(store: StateStore) -> None:
    table = Table(box=box.ROUNDED, padding=(0, 1))
    table.add_column("Phase",   style="bold")
    table.add_column("Status",  justify="center")
    table.add_column("Attempt", justify="right")
    table.add_column("Started",  style="dim")
    table.add_column("Finished", style="dim")
    table.add_column("Message")
    colours = {
        PhaseStatus.COMPLETE: "green", PhaseStatus.FAILED: "red",
        PhaseStatus.RUNNING: "cyan",   PhaseStatus.SKIPPED: "dim",
        PhaseStatus.PENDING: "yellow",
    }
    for rec in store.all_phases():
        c = colours.get(rec.status, "white")
        table.add_row(
            rec.name, f"[{c}]{rec.status}[/{c}]", str(rec.attempt),
            (rec.started_at or "")[:19], (rec.finished_at or "")[:19], rec.message or "",
        )
    console.print(table)


# ── CLI group ──────────────────────────────────────────────────────────────────

def _print_version(ctx: click.Context, param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    try:
        installer_version = version("platform-installer")
    except PackageNotFoundError:
        installer_version = "dev"
    console.print(f"platform-installer version: {installer_version}")
    console.print(f"platform version:           {PLATFORM_VERSION}")
    ctx.exit()


@click.group()
@click.option(
    "--version", is_flag=True, expose_value=False, is_eager=True,
    callback=_print_version,
    help="Show the installer version and the platform version it deploys, then exit.",
)
def main():
    """Platform Installer — disconnected bare-metal → OpenShift deployment tool."""


# ── validate ───────────────────────────────────────────────────────────────────

@main.command()
@click.option("--config",           "-c", default="platform-config.yaml")
@click.option("--manifest",         "-m", default=None)
def validate(config, manifest):
    """Validate config and manifest without deploying."""
    console.rule("[bold]Configuration Validation[/bold]")
    loader = _load_config(config, manifest)
    console.print("[green]✔[/green] platform-config.yaml — valid")
    console.print(
        f"[green]✔[/green] platform-manifest.yaml — "
        f"version {loader.manifest.get('manifest_version')} matches"
    )


# ── status ─────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--config",    "-c", default="platform-config.yaml")
@click.option("--manifest",  "-m", default=None)
@click.option("--state-dir",       default=".platform-installer-state")
def status(config, manifest, state_dir):
    """Show phase completion status."""
    _load_config(config, manifest)
    store = StateStore(Path(state_dir) / "state.db")
    store.initialize(PHASE_NAMES)
    _print_phase_table(store)


# ── reset ──────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--config",    "-c", default="platform-config.yaml")
@click.option("--state-dir",       default=".platform-installer-state")
@click.option("--phase",     "-p", required=True, multiple=True)
@click.confirmation_option(prompt="Reset phases back to pending?")
def reset(config, state_dir, phase):
    """Reset phases back to pending so they will re-run."""
    _load_config(config, None)
    store = StateStore(Path(state_dir) / "state.db")
    store.initialize(PHASE_NAMES)
    for p in phase:
        if p not in PHASE_NAMES:
            console.print(f"[red]Unknown phase: {p!r}[/red]  Valid: {PHASE_NAMES}")
            sys.exit(1)
        store.reset_phase(p)
        console.print(f"[yellow]↺[/yellow] {p} reset to pending")


# ── deploy ─────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--config",                   "-c", default="platform-config.yaml")
@click.option("--manifest",                 "-m", default=None)
@click.option("--state-dir",                      default=".platform-installer-state")
@click.option("--haul-path",                      required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Path to the Rancher Hauler bundle (.tar.zst) containing "
                   "all staged deployment assets.")
@click.option("--ansible-image",                  default=None,
              help="Tag of the preloaded Ansible execution image "
                   "(default: platform-ansible-exec:<installer version>).")
@click.option("--container-runtime",              default=None,
              help="podman or docker. Auto-detected (podman preferred) if not set.")
@click.option("--phase",                    "-p", default=None)
@click.option("--from-phase",                     default=None)
@click.option("--to-phase",                       default=None)
@click.option("--dry-run",                        is_flag=True, default=False)
@click.option("--skip-health-checks",             is_flag=True, default=False)
@click.option("--auto-approve",                   is_flag=True, default=False)
def deploy(
    config, manifest, state_dir, haul_path, ansible_image, container_runtime,
    phase, from_phase, to_phase, dry_run, skip_health_checks,
    auto_approve,
):
    """Run the full deployment or a subset of phases."""
    console.rule("[bold blue]Platform Installer[/bold blue]")

    loader     = _load_config(config, manifest)
    state_path = Path(state_dir)
    state_path.mkdir(parents=True, exist_ok=True)

    store = StateStore(state_path / "state.db")
    store.initialize(PHASE_NAMES)

    runner = _make_runner(
        loader=loader, state_dir=state_path,
        container_image=ansible_image or _default_ansible_image(),
        container_runtime=container_runtime,
        haul_path=haul_path,
        dry_run=dry_run,
    )
    phases        = _instantiate_phases(runner, store, loader)
    phase_by_name = {p.name: p for p in phases}

    # Determine run list
    if phase:
        if phase not in phase_by_name:
            console.print(f"[red]Unknown phase: {phase!r}[/red]  Valid: {PHASE_NAMES}")
            sys.exit(1)
        run_list = [phase_by_name[phase]]
    else:
        run_list = list(phases)
        if from_phase:
            idx = next((i for i, p in enumerate(run_list) if p.name == from_phase), None)
            if idx is None:
                console.print(f"[red]Unknown --from-phase: {from_phase!r}[/red]")
                sys.exit(1)
            for p in run_list[idx:]:
                store.reset_phase(p.name)
            run_list = run_list[idx:]
        if to_phase:
            idx = next((i for i, p in enumerate(run_list) if p.name == to_phase), None)
            if idx is None:
                console.print(f"[red]Unknown --to-phase: {to_phase!r}[/red]")
                sys.exit(1)
            run_list = run_list[:idx + 1]

    automation  = loader.config.global_.automation  # type: ignore[union-attr]
    fully_auto  = automation.fully_automated or auto_approve
    checkpoints = set(automation.approval_checkpoints)

    for p in run_list:
        record = store.get_phase(p.name)
        if record and record.is_complete:
            console.print(f"[dim]⊘  {p.name}: already complete — skipping[/dim]")
            continue

        if not fully_auto and f"post_{p.name}" in checkpoints:
            click.confirm(f"\n⚑  Checkpoint before '{p.name}'. Continue?", abort=True)

        # Switch Vault to hub address after hub_services completes
        if p.name == "hub_services":
            secrets   = loader._raw_config.get("secrets", {}).get("vault", {})
            hub_vault = secrets.get("hub_addr", "")
            if hub_vault:
                runner.switch_vault_addr(hub_vault)

        console.rule(f"[bold]Phase: {p.name}[/bold]")

        with store.phase_context(p.name) as ctx:
            try:
                result = p.run()
            except Exception as exc:
                ctx.fail(rc=1, message=str(exc))
                console.print(f"[bold red]✘ {p.name} raised: {exc}[/bold red]")
                sys.exit(1)

            if not result.success:
                ctx.fail(rc=result.rc, message=result.message)
                console.print(f"[bold red]✘ {p.name} FAILED: {result.message}[/bold red]")
                _print_phase_table(store)
                sys.exit(result.rc or 1)

            if not skip_health_checks:
                console.print(f"  [dim]Health check: {p.name} …[/dim]")
                if not p.health_check():
                    ctx.fail(rc=1, message="Health check failed")
                    console.print(f"[bold red]✘ {p.name} health check FAILED[/bold red]")
                    sys.exit(1)

            ctx.complete(message=result.message)
            console.print(f"[bold green]✔ {p.name}: {result.message}[/bold green]")

    console.rule("[bold green]Deployment Complete[/bold green]")
    _print_phase_table(store)


# ── preflight (alias) ──────────────────────────────────────────────────────────

@main.command()
@click.option("--config",             "-c", default="platform-config.yaml")
@click.option("--manifest",           "-m", default=None)
@click.option("--state-dir",                default=".platform-installer-state")
@click.option("--haul-path",                required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Path to the Rancher Hauler bundle (.tar.zst) containing "
                   "all staged deployment assets.")
@click.option("--ansible-image",            default=None)
@click.option("--container-runtime",        default=None)
@click.option("--dry-run",                  is_flag=True, default=False)
def preflight(config, manifest, state_dir, haul_path, ansible_image, container_runtime, dry_run):
    """Run preflight checks only."""
    ctx = click.get_current_context()
    ctx.invoke(
        deploy,
        config=config, manifest=manifest, state_dir=state_dir,
        haul_path=haul_path, ansible_image=ansible_image, container_runtime=container_runtime,
        phase="preflight", dry_run=dry_run, skip_health_checks=False,
        auto_approve=True, from_phase=None, to_phase=None,
    )


# ── report ─────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--config",   "-c", default="platform-config.yaml")
@click.option("--state-dir",      default=".platform-installer-state")
def report(config, state_dir):
    """Print a full deployment report."""
    loader = _load_config(config, None)
    store  = StateStore(Path(state_dir) / "state.db")
    store.initialize(PHASE_NAMES)
    console.rule("[bold]Deployment Report[/bold]")
    raw = loader._raw_config
    console.print(f"  Environment: {raw.get('global', {}).get('environment_name', '?')}")
    console.print(f"  Manifest:    {loader.manifest.get('manifest_version', '?')}")
    console.print()
    _print_phase_table(store)
    for rec in store.all_phases():
        for ev in store.events_for(rec.name):
            colour = {"error": "red", "warn": "yellow"}.get(ev["level"], "dim")
            console.print(f"  [{colour}]{ev['ts']} [{ev['level']}] {ev['message']}[/{colour}]")


if __name__ == "__main__":
    main()
