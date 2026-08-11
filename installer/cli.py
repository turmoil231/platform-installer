"""
installer/cli.py  (collection-aware revision)

Added to the previous version:
  - CollectionManager is initialized before any phase runs
  - `stage-collections` command for verifying the staged tarball set
  - `--collections-dir` option to override staged collections path
  - `--skip-collection-install` for development
  - Vault address auto-switching after hub_services phase
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table
from rich import box

from installer.config.loader import ConfigLoader, ConfigValidationError
from installer.runner.ansible import AnsibleRunner, CollectionManager, CollectionInstallError
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


def _resolve_collections_dir(loader: ConfigLoader, override: str | None) -> Path:
    if override:
        return Path(override).resolve()
    staging_root = loader._raw_config.get("assets", {}).get("staging_root", "/mnt/platform-assets")
    return Path(staging_root) / "collections"


def _setup_collection_manager(
    loader: ConfigLoader, state_dir: Path, collections_dir: Path,
    lock_file: str, skip_install: bool, dry_run: bool,
) -> CollectionManager:
    ansible_dir  = Path(__file__).resolve().parents[1] / "ansible"
    requirements = ansible_dir / "collections" / "requirements.yml"
    install_dir  = state_dir / "collections"

    manager = CollectionManager(
        staged_collections_dir = collections_dir,
        install_dir            = install_dir,
        requirements_file      = requirements,
        lock_file              = lock_file if Path(lock_file).exists() else None,
        verify_checksums       = not dry_run,
    )

    if not skip_install:
        console.rule("[bold]Collection Setup[/bold]")
        errors = manager.validate_staged_assets()
        if errors:
            console.print("[bold red]Collection staging validation failed:[/bold red]")
            for err in errors:
                console.print(f"  [red]• {err}[/red]")
            console.print(
                "\nRun [bold]scripts/stage_collections.sh[/bold] in a connected "
                "environment, then transfer tarballs to this host."
            )
            sys.exit(1)
        try:
            manager.install()
        except CollectionInstallError as exc:
            console.print(f"[bold red]Collection install failed:[/bold red] {exc}")
            sys.exit(1)

    return manager


def _make_runner(
    loader: ConfigLoader, state_dir: Path, collections_install_dir: Path, dry_run: bool,
) -> AnsibleRunner:
    ansible_dir   = Path(__file__).resolve().parents[1] / "ansible"
    artifacts_dir = state_dir / "ansible-artifacts"
    vars_dir      = state_dir / "ansible-vars"
    inventory     = state_dir / "inventory.yml"

    loader.generate_ansible_vars(vars_dir)
    loader.generate_ansible_inventory(inventory)

    extra_vars_files = sorted(vars_dir.glob("**/*.yml"))
    automation  = loader.config.global_.automation  # type: ignore[union-attr]
    secrets     = loader._raw_config.get("secrets", {}).get("vault", {})
    vault_addr  = secrets.get("bootstrap_addr", "http://localhost:8200")

    return AnsibleRunner(
        ansible_dir             = ansible_dir,
        collections_install_dir = collections_install_dir,
        artifacts_dir           = artifacts_dir,
        extra_vars_files        = extra_vars_files,
        inventory_path          = inventory,
        vault_addr              = vault_addr,
        vault_role_id_env       = secrets.get("role_id_env",   "VAULT_ROLE_ID"),
        vault_secret_id_env     = secrets.get("secret_id_env", "VAULT_SECRET_ID"),
        max_retries             = automation.max_retries,
        retry_delay             = automation.retry_delay_seconds,
        dry_run                 = dry_run,
    )


def _instantiate_phases(runner: AnsibleRunner, store: StateStore, loader: ConfigLoader) -> list[Phase]:
    vars_dir = runner.artifacts_dir.parent / "ansible-vars"
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
    ansible_dir  = Path(__file__).resolve().parents[1] / "ansible"
    requirements = ansible_dir / "collections" / "requirements.yml"
    manager = CollectionManager(
        staged_collections_dir = coll_dir,
        install_dir            = Path("/tmp/platform-validate-collections"),
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
    ansible_dir  = Path(__file__).resolve().parents[1] / "ansible"
    requirements = ansible_dir / "collections" / "requirements.yml"

    console.rule("[bold]Collection Staging Verification[/bold]")
    console.print(f"  Staged dir:   {coll_dir}")
    console.print(f"  Requirements: {requirements}")
    console.print(f"  Lock file:    {collections_lock}\n")

    manager = CollectionManager(
        staged_collections_dir = coll_dir,
        install_dir            = Path("/tmp/platform-stage-check"),
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
@click.option("--collections-dir",                default=None)
@click.option("--collections-lock",               default="collections.lock.yml")
@click.option("--phase",                    "-p", default=None)
@click.option("--from-phase",                     default=None)
@click.option("--to-phase",                       default=None)
@click.option("--dry-run",                        is_flag=True, default=False)
@click.option("--skip-health-checks",             is_flag=True, default=False)
@click.option("--auto-approve",                   is_flag=True, default=False)
@click.option("--skip-collection-install",        is_flag=True, default=False,
              help="Skip collection install (already installed in state dir).")
@click.option("--reinstall-collections",          is_flag=True, default=False,
              help="Force re-install even if collections already installed.")
def deploy(
    config, manifest, state_dir, collections_dir, collections_lock,
    phase, from_phase, to_phase, dry_run, skip_health_checks,
    auto_approve, skip_collection_install, reinstall_collections,
):
    """Run the full deployment or a subset of phases."""
    console.rule("[bold blue]Platform Installer[/bold blue]")

    loader     = _load_config(config, manifest)
    state_path = Path(state_dir)
    state_path.mkdir(parents=True, exist_ok=True)

    store = StateStore(state_path / "state.db")
    store.initialize(PHASE_NAMES)

    # Collection setup — must happen before any Ansible execution
    coll_staged = _resolve_collections_dir(loader, collections_dir)
    manager = _setup_collection_manager(
        loader=loader, state_dir=state_path, collections_dir=coll_staged,
        lock_file=collections_lock, skip_install=skip_collection_install, dry_run=dry_run,
    )
    if reinstall_collections:
        marker = state_path / "collections" / ".install_complete"
        if marker.exists():
            marker.unlink()
        manager.install(force=True)

    runner = _make_runner(
        loader=loader, state_dir=state_path,
        collections_install_dir=state_path / "collections", dry_run=dry_run,
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
@click.option("--config",          "-c", default="platform-config.yaml")
@click.option("--manifest",        "-m", default=None)
@click.option("--state-dir",             default=".platform-installer-state")
@click.option("--collections-dir",       default=None)
@click.option("--collections-lock",      default="collections.lock.yml")
@click.option("--dry-run",               is_flag=True, default=False)
def preflight(config, manifest, state_dir, collections_dir, collections_lock, dry_run):
    """Run preflight checks only."""
    ctx = click.get_current_context()
    ctx.invoke(
        deploy,
        config=config, manifest=manifest, state_dir=state_dir,
        collections_dir=collections_dir, collections_lock=collections_lock,
        phase="preflight", dry_run=dry_run, skip_health_checks=False,
        auto_approve=True, skip_collection_install=False,
        reinstall_collections=False, from_phase=None, to_phase=None,
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
