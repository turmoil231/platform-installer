"""
installer/cli.py

Runs natively on the admin host as a compiled binary — no outer container.
Ansible itself executes inside a preloaded "Ansible execution image" via
ansible-runner's container executor (see installer/runner/ansible.py).
Collections are baked into that image at build time; `validate` and
`stage-collections` are build-time checks only, not part of `deploy`.
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
from installer.runner.ansible import AnsibleRunner, CollectionManager
from installer.state.store import PhaseStatus, StateStore
from installer.phases.base import ALL_PHASES, PHASE_NAMES, Phase

console = Console()


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


def _resolve_collections_dir(loader: ConfigLoader, override: str | None) -> Path:
    if override:
        return Path(override).resolve()
    staging_root = loader._raw_config.get("assets", {}).get("staging_root", "/mnt/platform-assets")
    return Path(staging_root) / "collections"


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
    mounts: list[tuple[Path, bool]] = [
        (Path(g.ssh.public_key_path),       True),
        (Path(g.ssh.private_key_path),      True),
        (Path(g.tls.internal_ca_cert_path), True),
        (Path(g.tls.internal_ca_key_path),  True),
        (Path(g.pull_secret_path),          True),
    ]
    staging_root = loader._raw_config.get("assets", {}).get("staging_root")
    if staging_root:
        mounts.append((Path(staging_root), True))
    return mounts


def _make_runner(
    loader:            ConfigLoader,
    state_dir:         Path,
    container_image:   str,
    container_runtime: str | None,
    dry_run:           bool,
) -> AnsibleRunner:
    ansible_dir      = _ansible_dir()
    private_data_dir = state_dir / "ansible-pdd"

    loader.generate_ansible_vars(private_data_dir / "vars")
    loader.generate_ansible_inventory(private_data_dir / "inventory" / "hosts.yml")

    automation = loader.config.global_.automation  # type: ignore[union-attr]
    secrets    = loader._raw_config.get("secrets", {}).get("vault", {})
    vault_addr = secrets.get("bootstrap_addr", "http://localhost:8200")

    return AnsibleRunner(
        ansible_dir          = ansible_dir,
        private_data_dir     = private_data_dir,
        container_image      = container_image,
        container_runtime    = container_runtime,
        host_mounts          = _build_host_mounts(loader),
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

@click.group()
def main():
    """Platform Installer — disconnected bare-metal → OpenShift deployment tool."""


# ── validate ───────────────────────────────────────────────────────────────────

@main.command()
@click.option("--config",           "-c", default="platform-config.yaml")
@click.option("--manifest",         "-m", default=None)
@click.option("--collections-dir",        default=None)
@click.option("--collections-lock",       default="collections.lock.yml")
def validate(config, manifest, collections_dir, collections_lock):
    """Validate config, manifest, and staged collections without deploying."""
    console.rule("[bold]Configuration Validation[/bold]")
    loader = _load_config(config, manifest)
    console.print("[green]✔[/green] platform-config.yaml — valid")
    console.print(
        f"[green]✔[/green] platform-manifest.yaml — "
        f"version {loader.manifest.get('manifest_version')} matches"
    )
    coll_dir     = _resolve_collections_dir(loader, collections_dir)
    ansible_dir  = _ansible_dir()
    requirements = ansible_dir / "collections" / "requirements.yml"
    manager = CollectionManager(
        staged_collections_dir = coll_dir,
        requirements_file      = requirements,
        lock_file              = collections_lock if Path(collections_lock).exists() else None,
        verify_checksums       = True,
    )
    errors = manager.validate_staged_assets()
    if errors:
        console.print("\n[bold red]Collection staging issues:[/bold red]")
        for e in errors:
            console.print(f"  [red]• {e}[/red]")
        sys.exit(1)
    console.print("[green]✔[/green] Collections — all staged and checksums valid")


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


# ── stage-collections ──────────────────────────────────────────────────────────

@main.command("stage-collections")
@click.option("--config",           "-c", default="platform-config.yaml")
@click.option("--collections-dir",        default=None)
@click.option("--collections-lock",       default="collections.lock.yml")
def stage_collections(config, collections_dir, collections_lock):
    """
    Verify all collection tarballs are staged and checksums match the lock file.

    This does NOT fetch from the internet.  Run scripts/stage_collections.sh
    in a connected environment first, then transfer tarballs here.
    """
    loader   = _load_config(config, None)
    coll_dir = _resolve_collections_dir(loader, collections_dir)
    ansible_dir  = _ansible_dir()
    requirements = ansible_dir / "collections" / "requirements.yml"

    console.rule("[bold]Collection Staging Verification[/bold]")
    console.print(f"  Staged dir:   {coll_dir}")
    console.print(f"  Requirements: {requirements}")
    console.print(f"  Lock file:    {collections_lock}\n")

    manager = CollectionManager(
        staged_collections_dir = coll_dir,
        requirements_file      = requirements,
        lock_file              = collections_lock if Path(collections_lock).exists() else None,
        verify_checksums       = True,
    )
    errors = manager.validate_staged_assets()
    if errors:
        console.print("[bold red]Staging validation FAILED:[/bold red]")
        for e in errors:
            console.print(f"  [red]• {e}[/red]")
        console.print(
            "\n[yellow]To populate staged collections, run on a connected host:[/yellow]\n"
            "  ./scripts/stage_collections.sh \\\n"
            "    --staging-root <path> \\\n"
            "    --requirements ansible/collections/requirements.yml\n"
            "Then transfer <staging-root>/collections/ to this host."
        )
        sys.exit(1)

    console.print("[bold green]✔ All collections staged and verified[/bold green]")
    tarballs = sorted(coll_dir.glob("*.tar.gz")) if coll_dir.exists() else []
    if tarballs:
        table = Table(box=box.SIMPLE)
        table.add_column("Tarball")
        table.add_column("Size", justify="right")
        for t in tarballs:
            table.add_row(t.name, f"{t.stat().st_size / 1_048_576:.1f} MB")
        console.print(table)


# ── deploy ─────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--config",                   "-c", default="platform-config.yaml")
@click.option("--manifest",                 "-m", default=None)
@click.option("--state-dir",                      default=".platform-installer-state")
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
    config, manifest, state_dir, ansible_image, container_runtime,
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
@click.option("--ansible-image",            default=None)
@click.option("--container-runtime",        default=None)
@click.option("--dry-run",                  is_flag=True, default=False)
def preflight(config, manifest, state_dir, ansible_image, container_runtime, dry_run):
    """Run preflight checks only."""
    ctx = click.get_current_context()
    ctx.invoke(
        deploy,
        config=config, manifest=manifest, state_dir=state_dir,
        ansible_image=ansible_image, container_runtime=container_runtime,
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
