"""Demo driver - exercises the UI the way the real installer will.

This stands in for the Python/ansible-runner orchestration layer described
in the design: it builds an install plan, then drives a Reporter from
background threads exactly the way ansible-runner's event_handler/
status_handler callbacks would (see installer/tui/README.md for the real wiring). One step
is scripted to fail with a realistic Ansible error so you can see the Errors
tab - and a non-zero exit code - in action.

Run from the repo root, with the installer installed in your venv
(``pip install -e .``):
    python scripts/tui_demo.py                      # dashboard (in a terminal)
    python scripts/tui_demo.py --ui plain           # CI-style log output
    python scripts/tui_demo.py --junit report.xml   # also write a JUnit report
    python scripts/tui_demo.py --log-file run.log   # log somewhere other than install.log
    python scripts/tui_demo.py --no-failure         # all steps pass, exit 0
"""

from __future__ import annotations

import argparse
import random
import sys
import threading
import time
from dataclasses import dataclass, field

from installer.tui import UI_MODES, InstallPlan, Phase, Reporter, Step, run_installer

FAILING_STEP_ID = "services.deploy_registry_mirror"

FAILING_STEP_ERROR = """TASK [registry_mirror : wait for registry TLS listener] ***************************
fatal: [paas-svc-01]: FAILED! => {"changed": false, "elapsed": 30, "msg": "Timeout when waiting for 10.20.4.11:5000"}

TASK [registry_mirror : seed mirror from offline image bundle] ********************
fatal: [paas-svc-01]: FAILED! => {"changed": true, "cmd": "skopeo sync --src dir --dest docker /opt/bootstrap/images registry-mirror.internal:5000", "rc": 1}
stderr: level=fatal msg="Error trying to reuse blob sha256:3a2e19f2...: x509: certificate signed by unknown authority"

PLAY RECAP **************************************************************************
paas-svc-01                : ok=6    changed=4    unreachable=0    failed=1

The registry mirror's TLS certificate was not signed by the offline CA
bundle staged in /opt/bootstrap/pki. Re-provision the CA bundle and re-run:
  paas-installer bootstrap --resume-from services.deploy_registry_mirror"""


@dataclass
class SimStep:
    step: Step
    min_seconds: float
    max_seconds: float
    output_lines: list[str] = field(default_factory=list)


def build_plan() -> tuple[InstallPlan, dict[str, SimStep]]:
    plan = InstallPlan()
    sim: dict[str, SimStep] = {}

    def add_phase(phase_id: str, name: str) -> Phase:
        phase = Phase(id=phase_id, name=name)
        plan.phases.append(phase)
        return phase

    def add_step(
        phase: Phase, step_id: str, name: str, lo: float, hi: float, lines: list[str]
    ) -> None:
        step = Step(id=step_id, name=name, phase_id=phase.id)
        phase.steps.append(step)
        sim[step_id] = SimStep(step=step, min_seconds=lo, max_seconds=hi, output_lines=lines)

    preflight = add_phase("preflight", "Preflight Checks")
    add_step(
        preflight, "preflight.verify_media", "Verify install media checksums", 0.8, 1.4,
        ["mounting /mnt/bootstrap-media", "sha256sum -c manifest.sha256", "192/192 files verified"],
    )
    add_step(
        preflight, "preflight.verify_hardware", "Verify hardware inventory", 0.6, 1.2,
        ["reading /etc/paas/hardware-manifest.yml", "cross-checking dmidecode output"],
    )
    add_step(
        preflight, "preflight.verify_network", "Validate airgapped network topology", 1.0, 1.8,
        ["checking VLAN trunk config", "confirming no default route to internet"],
    )

    storage = add_phase("storage", "Provision Storage")
    for i in range(1, 4):
        add_step(
            storage, f"storage.init_node{i}", f"Initialize storage on paas-store-0{i}", 1.5, 3.0,
            [f"wiping /dev/nvme{i}n1", "creating LVM physical volume", "creating volume group vg_paas"],
        )

    control_plane = add_phase("control_plane", "Bootstrap Control Plane")
    add_step(
        control_plane, "control_plane.pki", "Generate cluster PKI from offline CA", 1.2, 2.0,
        ["loading root CA from /opt/bootstrap/pki", "issuing etcd peer certificates"],
    )
    add_step(
        control_plane, "control_plane.etcd", "Bootstrap etcd cluster", 2.0, 3.2,
        ["starting etcd on 3 seed nodes", "waiting for raft quorum"],
    )
    add_step(
        control_plane, "control_plane.apiserver", "Start API server", 1.4, 2.2,
        ["writing static pod manifest", "waiting for /healthz"],
    )

    services = add_phase("services", "Deploy Core Services")
    add_step(
        services, "services.deploy_dns", "Deploy internal DNS", 1.2, 2.4,
        ["applying coredns manifests", "verifying resolution of internal.paas"],
    )
    add_step(
        services, FAILING_STEP_ID, "Deploy container registry mirror", 1.6, 2.6,
        ["seeding registry from offline image bundle", "starting registry-mirror.internal:5000"],
    )
    add_step(
        services, "services.deploy_lb", "Deploy load balancer", 1.0, 2.0,
        ["applying haproxy config", "binding VIP 10.20.4.1"],
    )
    add_step(
        services, "services.deploy_monitoring", "Deploy monitoring stack", 1.8, 3.0,
        ["applying prometheus + grafana manifests", "importing default dashboards"],
    )

    validation = add_phase("validation", "Post-Install Validation")
    add_step(
        validation, "validation.health", "Run cluster health checks", 1.0, 1.6,
        ["checking node readiness", "checking control plane component status"],
    )
    add_step(
        validation, "validation.smoke", "Run smoke tests", 1.2, 2.0,
        ["deploying smoke-test workload", "verifying service reachability"],
    )
    add_step(
        validation, "validation.report", "Generate install report", 0.4, 0.8,
        ["writing /var/log/paas-installer/report.json"],
    )

    return plan, sim


def run_step(reporter: Reporter, sim: SimStep, fail_step_id: str | None) -> None:
    reporter.start_step(sim.step.id)
    for line in sim.output_lines:
        time.sleep(random.uniform(0.15, 0.4))
        reporter.append_output(sim.step.id, line)

    time.sleep(random.uniform(sim.min_seconds, sim.max_seconds))

    if sim.step.id == fail_step_id:
        reporter.finish_step(sim.step.id, success=False, error=FAILING_STEP_ERROR)
    else:
        reporter.finish_step(sim.step.id, success=True)


def run_phase(
    reporter: Reporter,
    phase: Phase,
    sim: dict[str, SimStep],
    parallel: bool,
    fail_step_id: str | None,
) -> None:
    reporter.log(f"── starting phase: {phase.name} ──")
    if not parallel:
        for step in phase.steps:
            run_step(reporter, sim[step.id], fail_step_id)
        return

    workers = [
        threading.Thread(
            target=run_step, args=(reporter, sim[step.id], fail_step_id), daemon=True
        )
        for step in phase.steps
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()


def install(
    reporter: Reporter,
    plan: InstallPlan,
    sim: dict[str, SimStep],
    fail_step_id: str | None,
) -> None:
    # Storage and service deployment steps are independent of each other, so
    # they fan out across threads the way parallelized ansible-runner jobs
    # would. Control-plane bootstrap has real ordering dependencies (PKI ->
    # etcd -> API server) so it stays sequential.
    parallel_phases = {"storage", "services"}
    for phase in plan.phases:
        run_phase(reporter, phase, sim, phase.id in parallel_phases, fail_step_id)

    reporter.log("bootstrap finished")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--ui",
        choices=UI_MODES,
        default="auto",
        help="tui = live dashboard, plain = line-per-event log for CI; "
        "auto (default) picks plain when CI is set or output isn't a terminal",
    )
    parser.add_argument("--junit", metavar="PATH", help="write a JUnit XML report to PATH")
    parser.add_argument(
        "--log-file",
        metavar="PATH",
        default="install.log",
        help="append a full log of the run to PATH (default: %(default)s)",
    )
    parser.add_argument(
        "--exit-when-done",
        action="store_true",
        help="close the dashboard as soon as the install finishes instead of waiting for q",
    )
    parser.add_argument(
        "--no-failure",
        action="store_true",
        help=f"don't script {FAILING_STEP_ID} to fail (demo-only)",
    )
    args = parser.parse_args()

    plan, sim = build_plan()
    fail_step_id = None if args.no_failure else FAILING_STEP_ID
    return run_installer(
        plan,
        lambda reporter: install(reporter, plan, sim, fail_step_id),
        ui=args.ui,
        junit_path=args.junit,
        log_path=args.log_file,
        exit_when_done=args.exit_when_done,
    )


if __name__ == "__main__":
    sys.exit(main())
