"""
installer/cli.py

Runs natively on the admin host as a compiled binary — no outer container.
Ansible itself executes inside a preloaded "Ansible execution image" via
ansible-runner's container executor (see installer/runner/ansible.py).
Collections are baked into that image ahead of time — the installer never
stages, validates, or otherwise concerns itself with collections at all.

The execution image ships inside the haul. Before anything else, `deploy`
starts Hauler (the only image shipped next to the binary) and the admin
host's Vault, pulling both the execution image and Vault from Hauler's
registry (see installer/runner/services.py).
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Callable

import click
import yaml
from rich.console import Console
from rich.table import Table
from rich import box

from installer import tui
from installer.config.loader import ConfigLoader, ConfigValidationError
from installer.runner.ansible import AnsibleRunner
from installer.runner.services import LocalServices, hauler_ref
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


def _installer_version() -> str:
    try:
        return version("platform-installer")
    except PackageNotFoundError:
        return "dev"


def _default_ansible_image() -> str:
    return f"platform-ansible-exec:{_installer_version()}"


def _default_hauler_image() -> str:
    return f"platform-hauler:{_installer_version()}"


def _local_vault_image(loader: ConfigLoader) -> str:
    """local_services.vault.image, else the manifest's Vault pin as Hauler serves it."""
    configured = loader.config.local_services.vault.image  # type: ignore[union-attr]
    if configured:
        return configured
    source = loader.manifest.get("image_pins", {}).get("vault", {}).get("source", "")
    if not source:
        console.print(
            "[bold red]CONFIG ERROR:[/bold red] set local_services.vault.image, or "
            "image_pins.vault.source in platform-manifest.yaml"
        )
        sys.exit(1)
    return hauler_ref(source)


def _make_local_services(
    loader:            ConfigLoader,
    state_dir:         Path,
    haul_path:         Path,
    hauler_image:      str,
    exec_image:        str,
    container_runtime: str | None,
) -> LocalServices:
    return LocalServices(
        state_dir         = state_dir,
        haul_path         = haul_path,
        config            = loader.config.local_services,  # type: ignore[union-attr]
        hauler_image      = hauler_image,
        exec_image        = exec_image,
        vault_image       = _local_vault_image(loader),
        container_runtime = container_runtime,
    )


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
    services:          LocalServices,
    hub_vault_addr:    str | None,
    dry_run:           bool,
) -> AnsibleRunner:
    """
    `hub_vault_addr` is set when the local services have been retired (a
    previous run completed hub_services): playbooks then talk to the hub's
    Vault from the start.
    """
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
    with (vars_dir / "local_services.yml").open("w") as fh:
        yaml.dump(services.ansible_vars(), fh)

    automation = loader.config.global_.automation  # type: ignore[union-attr]
    secrets    = loader._raw_config.get("secrets", {}).get("vault", {})

    host_mounts = _build_host_mounts(loader)
    host_mounts.append((haul_path, True))
    host_mounts += [(d, False) for d in services.credential_dirs()]

    return AnsibleRunner(
        ansible_dir          = ansible_dir,
        private_data_dir     = private_data_dir,
        container_image      = container_image,
        container_runtime    = container_runtime,
        host_mounts          = host_mounts,
        vault_addr           = hub_vault_addr or services.vault_addr,
        vault_role_id_env    = secrets.get("role_id_env",   "VAULT_ROLE_ID"),
        vault_secret_id_env  = secrets.get("secret_id_env", "VAULT_SECRET_ID"),
        vault_credentials_file = None if hub_vault_addr else services.approle_credentials_path,
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


#: Runs at the start of every deploy, outside the state store: it must be
#: idempotent (initialize if needed, unseal if sealed, ensure the AppRole),
#: because the local Vault comes back sealed after every container restart.
LOCAL_SERVICES          = "local_services"
CONFIGURE_VAULT         = "local/configure_vault.yml"
CONFIGURE_VAULT_STEP_ID = f"{LOCAL_SERVICES}.configure_vault"


def build_plan(phases: list[Phase]) -> tui.InstallPlan:
    """
    Progress-UI plan: the always-run local_services step, then one tui.Phase
    per installer phase, one tui.Step per planned playbook plus a
    health-check step for phases that have one. Phases that won't run are
    still in the plan (and skipped by install()), so the operator sees the
    whole deployment.
    """
    plan = tui.InstallPlan()
    plan.phases.append(tui.Phase(id=LOCAL_SERVICES, name=LOCAL_SERVICES, steps=[
        tui.Step(id=CONFIGURE_VAULT_STEP_ID, name=CONFIGURE_VAULT, phase_id=LOCAL_SERVICES),
    ]))
    for p in phases:
        ui_phase = tui.Phase(id=p.name, name=p.name)
        for pb in p.planned_playbooks():
            ui_phase.steps.append(tui.Step(id=p.step_id(pb.playbook), name=pb.playbook, phase_id=p.name))
        if p.has_health_check:
            ui_phase.steps.append(
                tui.Step(id=p.health_check_step_id, name=f"{p.name} health check", phase_id=p.name)
            )
        plan.phases.append(ui_phase)
    return plan


def checkpoint_before(phase_name: str, checkpoints: set[str]) -> str | None:
    """
    The approval checkpoint (if any) that gates the start of `phase_name`:
    `pre_<phase>`, or `post_<previous phase>` (the phase before it in
    PHASE_NAMES order).
    """
    if f"pre_{phase_name}" in checkpoints:
        return f"pre_{phase_name}"
    idx = PHASE_NAMES.index(phase_name)
    if idx > 0 and f"post_{PHASE_NAMES[idx - 1]}" in checkpoints:
        return f"post_{PHASE_NAMES[idx - 1]}"
    return None


def make_install(
    *,
    plan:               tui.InstallPlan,
    phases:             list[Phase],
    run_names:          set[str],
    store:              StateStore,
    runner:             AnsibleRunner,
    checkpoints:        set[str],
    confirm:            Callable[[str], bool],
    hub_vault_addr:     str,
    local_services:     LocalServices | None,
    dry_run:            bool,
    skip_health_checks: bool,
):
    """
    Build the install(reporter) function that run_installer() drives.

    `local_services` is None once they've been retired (hub_services
    completed on an earlier run). Otherwise configure_vault.yml runs first,
    and when hub_services completes the runner switches to the hub's Vault
    and the local containers are removed.

    Nothing in here may print, prompt (except through `confirm`, only ever
    a real prompt in plain mode at an interactive terminal), or sys.exit():
    under the dashboard it runs on a background thread. A failure finishes
    the failing step and returns, leaving later steps pending, which is what
    makes run_installer() exit 1. The state store stays the source of truth
    for resume; a dry run never writes to it.
    """
    steps_by_phase = {ui_phase.id: [s.id for s in ui_phase.steps] for ui_phase in plan.phases}

    def install(reporter: tui.Reporter) -> None:
        runner.reporter = reporter

        def skip_phase(p: Phase, reason: str) -> None:
            for step_id in steps_by_phase[p.name]:
                reporter.skip_step(step_id, reason=reason)

        if local_services is None:
            reporter.skip_step(CONFIGURE_VAULT_STEP_ID, reason="local services retired")
        else:
            try:
                result = runner.run_playbook(CONFIGURE_VAULT, step_id=CONFIGURE_VAULT_STEP_ID)
            except Exception as exc:
                # The runner has already finished the step that raised.
                reporter.log(f"Local Vault configuration raised: {exc}", level="error")
                return
            if not result.success:
                reporter.log("Local Vault configuration FAILED", level="error")
                return

        for p in phases:
            if p.name not in run_names:
                skip_phase(p, "not selected")
                continue

            record = store.get_phase(p.name)
            if record and record.is_complete:
                skip_phase(p, "already complete")
                continue

            gate = checkpoint_before(p.name, checkpoints)
            if gate and not confirm(f"Checkpoint {gate}: continue with '{p.name}'?"):
                reporter.log(f"Stopped at approval checkpoint {gate} before {p.name}", level="warning")
                return

            reporter.log(f"Phase {p.name}")

            if dry_run:
                p.run()  # every playbook step is skipped as "dry run"
                if p.has_health_check:
                    reporter.skip_step(p.health_check_step_id, reason="dry run")
                continue

            with store.phase_context(p.name) as ctx:
                try:
                    result = p.run()
                except Exception as exc:
                    # The runner has already finished the step that raised.
                    ctx.fail(rc=1, message=str(exc))
                    reporter.log(f"{p.name} raised: {exc}", level="error")
                    return

                if not result.success:
                    ctx.fail(rc=result.rc, message=result.message)
                    reporter.log(f"{p.name} FAILED: {result.message}", level="error")
                    return

                if p.has_health_check:
                    hc = p.health_check_step_id
                    if skip_health_checks:
                        reporter.skip_step(hc, reason="--skip-health-checks")
                    else:
                        reporter.start_step(hc)
                        try:
                            healthy = p.health_check()
                        except Exception as exc:
                            reporter.finish_step(hc, success=False, error=f"{type(exc).__name__}: {exc}")
                            ctx.fail(rc=1, message=f"Health check raised: {exc}")
                            return
                        reporter.finish_step(hc, success=healthy, error=None if healthy else "Health check failed")
                        if not healthy:
                            ctx.fail(rc=1, message="Health check failed")
                            reporter.log(f"{p.name} health check FAILED", level="error")
                            return

                ctx.complete(message=result.message)
                reporter.log(f"{p.name}: {result.message}")

            # hub_services migrated the local Vault and Hauler content to the
            # hub: switch over and retire the admin host's containers.
            if p.name == "hub_services" and hub_vault_addr and local_services is not None:
                runner.switch_vault_addr(hub_vault_addr)
                try:
                    local_services.stop()
                    reporter.log("Local Hauler and Vault containers removed")
                except Exception as exc:
                    reporter.log(f"Could not remove local Hauler/Vault containers: {exc}", level="warning")

    return install


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

def _progress_options(f):
    """Progress UI / reporting options shared by deploy and preflight."""
    f = click.option("--exit-when-done",              is_flag=True, default=False,
                     help="Close the dashboard as soon as the run finishes.")(f)
    f = click.option("--log-file",                    default=None,
                     type=click.Path(dir_okay=False, path_type=Path),
                     help="Append the run log here (default: <state-dir>/install.log).")(f)
    f = click.option("--junit",                       default=None,
                     type=click.Path(dir_okay=False, path_type=Path),
                     help="Write a JUnit XML report of every step to this path.")(f)
    f = click.option("--ui",                          default="auto", show_default=True,
                     type=click.Choice(tui.UI_MODES),
                     help="tui: dashboard; plain: streaming log (CI); "
                          "auto: dashboard only at an interactive terminal.")(f)
    return f


@main.command()
@click.option("--config",                   "-c", default="platform-config.yaml")
@click.option("--manifest",                 "-m", default=None)
@click.option("--state-dir",                      default=".platform-installer-state")
@click.option("--haul-path",                      required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Path to the Rancher Hauler bundle (.tar.zst) containing "
                   "all staged deployment assets.")
@click.option("--ansible-image",                  default=None,
              help="Ansible execution image reference inside the haul, as served by "
                   "Hauler's registry (default: platform-ansible-exec:<installer version>).")
@click.option("--hauler-image",                   default=None,
              help="Tag of the Hauler image, auto-loaded from a tarball next to the binary "
                   "(default: platform-hauler:<installer version>).")
@click.option("--container-runtime",              default=None,
              help="podman or docker. Auto-detected (podman preferred) if not set.")
@click.option("--phase",                    "-p", default=None)
@click.option("--from-phase",                     default=None)
@click.option("--to-phase",                       default=None)
@click.option("--dry-run",                        is_flag=True, default=False)
@click.option("--skip-health-checks",             is_flag=True, default=False)
@click.option("--auto-approve",                   is_flag=True, default=False)
@_progress_options
def deploy(
    config, manifest, state_dir, haul_path, ansible_image, hauler_image, container_runtime,
    phase, from_phase, to_phase, dry_run, skip_health_checks,
    auto_approve, ui, junit, log_file, exit_when_done,
):
    """Run the full deployment or a subset of phases."""
    console.rule("[bold blue]Platform Installer[/bold blue]")

    loader     = _load_config(config, manifest)
    state_path = Path(state_dir)
    state_path.mkdir(parents=True, exist_ok=True)

    store = StateStore(state_path / "state.db")
    store.initialize(PHASE_NAMES)

    # Determine run list (before anything reads phase state: --from-phase
    # resets phases, which decides whether the local services are retired)
    if phase:
        if phase not in PHASE_NAMES:
            console.print(f"[red]Unknown phase: {phase!r}[/red]  Valid: {PHASE_NAMES}")
            sys.exit(1)
        run_names_ordered = [phase]
    else:
        run_names_ordered = list(PHASE_NAMES)
        if from_phase:
            if from_phase not in run_names_ordered:
                console.print(f"[red]Unknown --from-phase: {from_phase!r}[/red]")
                sys.exit(1)
            run_names_ordered = run_names_ordered[run_names_ordered.index(from_phase):]
            if not dry_run:
                for name in run_names_ordered:
                    store.reset_phase(name)
        if to_phase:
            if to_phase not in run_names_ordered:
                console.print(f"[red]Unknown --to-phase: {to_phase!r}[/red]")
                sys.exit(1)
            run_names_ordered = run_names_ordered[:run_names_ordered.index(to_phase) + 1]
    run_names = set(run_names_ordered)

    # Once hub_services has migrated Vault (and Hauler's content) to the hub,
    # the admin host's Hauler and Vault are retired for good.
    secrets        = loader._raw_config.get("secrets", {}).get("vault", {})
    hub_vault_addr = secrets.get("hub_addr", "")
    hub_record     = store.get_phase("hub_services")
    retired        = bool(hub_vault_addr and hub_record and hub_record.is_complete)

    exec_image = ansible_image or _default_ansible_image()
    services = _make_local_services(
        loader=loader, state_dir=state_path, haul_path=haul_path,
        hauler_image=hauler_image or _default_hauler_image(),
        exec_image=exec_image, container_runtime=container_runtime,
    )
    runner = _make_runner(
        loader=loader, state_dir=state_path,
        container_image=exec_image,
        container_runtime=container_runtime,
        haul_path=haul_path,
        services=services,
        hub_vault_addr=hub_vault_addr if retired else None,
        dry_run=dry_run,
    )
    phases = _instantiate_phases(runner, store, loader)

    automation  = loader.config.global_.automation  # type: ignore[union-attr]
    fully_auto  = automation.fully_automated or auto_approve
    checkpoints = set() if fully_auto else set(automation.approval_checkpoints)
    mode        = tui.choose_ui(ui)

    # Checkpoints can only be answered in plain mode at an interactive
    # terminal: the dashboard owns the terminal, and in CI nobody can answer.
    gates = []
    for name in run_names_ordered:
        record = store.get_phase(name)
        gate   = checkpoint_before(name, checkpoints)
        if gate and not (record and record.is_complete):
            gates.append(gate)
    if gates and (mode == "tui" or not sys.stdin.isatty()):
        console.print(
            f"[bold red]Approval checkpoints would pause this run:[/bold red] {', '.join(gates)}\n"
            f"They can't be answered under the dashboard or without an interactive terminal. "
            f"Re-run with --auto-approve, set global.automation.fully_automated: true, "
            f"or use --ui plain at a terminal to answer them."
        )
        sys.exit(1)

    # Loading the haul and pulling images prints progress and can take a
    # long time: finish it before the dashboard takes over the terminal.
    if not dry_run:
        try:
            if not retired:
                services.ensure_running()
            runner.ensure_ready()
        except RuntimeError as exc:
            console.print(f"[bold red]STARTUP ERROR:[/bold red] {exc}")
            sys.exit(1)

    plan    = build_plan(phases)
    install = make_install(
        plan=plan, phases=phases, run_names=run_names, store=store, runner=runner,
        checkpoints=checkpoints,
        confirm=lambda question: click.confirm(question, default=False),
        hub_vault_addr=hub_vault_addr,
        local_services=None if retired else services,
        dry_run=dry_run, skip_health_checks=skip_health_checks,
    )
    sys.exit(tui.run_installer(
        plan, install,
        ui=mode,
        junit_path=junit,
        log_path=log_file or state_path / "install.log",
        exit_when_done=exit_when_done,
    ))


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
@click.option("--hauler-image",             default=None)
@click.option("--container-runtime",        default=None)
@click.option("--dry-run",                  is_flag=True, default=False)
@_progress_options
def preflight(
    config, manifest, state_dir, haul_path, ansible_image, hauler_image, container_runtime, dry_run,
    ui, junit, log_file, exit_when_done,
):
    """Run preflight checks only."""
    ctx = click.get_current_context()
    ctx.invoke(
        deploy,
        config=config, manifest=manifest, state_dir=state_dir,
        haul_path=haul_path, ansible_image=ansible_image, hauler_image=hauler_image,
        container_runtime=container_runtime,
        phase="preflight", dry_run=dry_run, skip_health_checks=False,
        auto_approve=True, from_phase=None, to_phase=None,
        ui=ui, junit=junit, log_file=log_file, exit_when_done=exit_when_done,
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
