# Integrating `installer.tui` into the CLI

One-time brief for wiring the vendored TUI into `platform-installer
deploy` (and `preflight`). **Delete this file when the integration is
done**, and update the root `CLAUDE.md` if anything below changed.

Read `CLAUDE.md` and `README.md` in this directory first.

## Current state

- `installer/tui/` is copied from the standalone `terminal-ui` prototype
  (commit `b999216`). It uses relative imports and inlined CSS. It works:
  `python scripts/tui_demo.py --ui plain` exits 0 with `--no-failure`.
- **Nothing imports it yet.** `textual` is not in `pyproject.toml` or
  `packaging/pip-cache/`, so the build, the tests and the binary are all
  unaffected for now. This stops being true once `cli.py` imports it, which
  is why step 1 comes first.

## 1. Dependencies and packaging (do first)

- Add `textual>=8.2.8` to `[project].dependencies`. `rich` is already
  listed. Check that the cached `rich` wheel satisfies Textual's pin.
- Seed the offline cache (`packaging/seed_pip_cache.sh`). These wheels are
  missing today: `textual`, `mdit_py_plugins`, `platformdirs`.
  `markdown_it_py`, `mdurl`, `pygments`, `rich` and `typing_extensions` are
  already there. `linkify-it-py` is an optional extra and isn't needed.
- `packaging/build_binary.sh`: `textual.widgets` imports widgets lazily
  through a module `__getattr__`, which PyInstaller's static analysis can
  miss. Add `--collect-submodules textual` to the `pyinstaller` call. Add
  `--collect-data textual` too if the binary errors on missing resources.
  The current smoke test (`--help`) never imports the dashboard, so it
  won't catch this. Run the built binary in a real terminal
  (`deploy --dry-run ...`) to confirm the dashboard renders.

## 2. Build the plan up front

The TUI needs every step known **before** the run starts. Right now each
`Phase.run()` decides its playbooks while it runs (`HubServicesPhase` loops
over enabled services, `VDIPhase` picks VMware or OCP-Virt, and so on).

- Mapping: installer phase → `tui.Phase(id=phase.name)`. Each playbook →
  `tui.Step(id=f"{phase.name}.{Path(playbook).stem}", phase_id=phase.name)`.
  For the name, use the playbook path or a short human label.
- Add a method to `installer.phases.base.Phase`, e.g.
  `planned_playbooks() -> list[str]`, that applies the same config logic as
  `run()`. Ideally `run()` iterates over it, so the two can't drift. At
  minimum, add a unit test asserting that they agree.
- Health checks: for phases that override `health_check()`, add a
  `<phase>.health_check` step. It's skipped under `--skip-health-checks`.
- **Name clash:** `installer.phases.base.Phase` and `installer.tui.Phase`.
  Import the package as a module (`from installer import tui`) rather than
  importing names.

## 3. Route Ansible output through the reporter

- `AnsibleRunner._execute`'s `event_handler` currently does
  `console.print(ln, markup=False)`. Route it to
  `reporter.append_output(step_id, ln)` instead.
- `run_playbook` → `start_step` at the first attempt, then a single
  `finish_step(step_id, success, error=...)` once the retries settle.
  Report retries as `append_output` lines, not as new steps. For `error=`,
  use the failed hosts plus the `res` of the last `runner_on_failed` event
  (or the tail of `collected_stdout`). This is what fills the Errors tab and
  the JUnit failure body.
- Plumbing: the step id is known in `Phase._run_playbook`
  (`self.name` + the playbook). Pass `reporter` and `step_id` down to
  `run_playbook`, or give the runner a reporter attribute. Keep it simple.
  Phases shouldn't need to know about step ids beyond that helper.
- `dry_run`: `skip_step(step_id, reason="dry run")`, so a dry run exits 0.
- Replace the other in-run `console.print` calls in `runner/ansible.py` and
  `phases/base.py` (retry notices, "→ Deploying svc", "⊘ disabled", vault
  switch) with `reporter.log(...)` or `append_output(...)`.
- **`ensure_ready()` is called lazily from `_execute`**, and it prints while
  loading the Ansible image (which can take minutes), which would happen
  under the dashboard. Call `runner.ensure_ready()` explicitly in `deploy`
  **before** `run_installer` (unless `--dry-run`), so image loading prints
  normally and finishes before the dashboard starts.

## 4. Turn the `deploy` loop into `install(reporter)`

Move the `for p in run_list:` body into an `install(reporter)` closure and
call `sys.exit(tui.run_installer(plan, install, ui=..., junit_path=...,
log_path=..., exit_when_done=...))`. Inside it:

- **No `sys.exit()`.** On a phase failure, exception or failed health check:
  keep the `ctx.fail(...)` state-store calls, `finish_step` the failing
  step, and `return`. The remaining steps stay pending, which correctly
  gives exit 1 and shows as "not run" in the summary. Let unexpected
  exceptions propagate. `run_installer` logs the traceback and exits 1.
- **Skip what won't run.** Phases already complete in the state store →
  `skip_step(..., reason="already complete")` for each step. Phases outside
  the `--phase` / `--from-phase` / `--to-phase` window are also in the plan,
  so skip them with `reason="not selected"`. (Alternatively, leave them out
  of the plan. Showing them gives the operator the whole picture.)
- The state store stays the source of truth for resume. The reporter is
  display only. Mirror `store.log_event` messages to `reporter.log` where
  useful.
- `_print_phase_table` after failure or completion: drop it, or call it
  after `run_installer` returns. `run_installer` already prints a summary.
- **Approval checkpoints (`click.confirm`) are an open decision. Ask the
  user.** You can't prompt while the dashboard owns the terminal, and in CI
  there's nobody to answer. Options:
  1. **Stop at the checkpoint** *(suggested)*: the run ends cleanly before
     the gated phase, the remaining steps are skipped with
     `reason="awaiting approval - re-run to continue"`, and the operator
     re-runs (resume skips the completed phases). This fits the existing
     resume model. Decide whether that exit code is 0 or a distinct
     non-zero value.
  2. Require `--auto-approve` or `fully_automated` whenever the UI isn't
     plain and checkpoints would fire.
  3. Add a modal confirm to the dashboard. This is the most work, it needs a
     thread-safe request/response through the controller, and it still
     doesn't solve CI.

## 5. CLI options (`deploy` and `preflight`)

- `--ui [auto|tui|plain]` (default `auto`; `tui.UI_MODES` holds the choices).
- `--junit PATH`
- `--log-file PATH`: the README recommends defaulting to a durable path.
  `<state-dir>/install.log` is always writable and lives next to
  `state.db`. `/var/log/platform-installer/install.log` is the alternative
  if the installer runs as root. Confirm with the user.
- `--exit-when-done` (dashboard closes by itself when the run finishes).

Config loading and validation errors happen before the dashboard starts, so
the existing `console.print` + `sys.exit(1)` there is fine as it is.

## 6. Tests and CI

- `tests/unit/` (the build runs these): plan building matches
  `planned_playbooks()` for representative configs; the `install` closure
  with a fake `Reporter` (a recording stub) and a mocked runner covers
  success, failure (exit 1, later steps pending), resume (completed phases
  skipped, exit 0) and dry run.
- `run_installer(..., ui="plain")` works without a terminal, so tests can
  call it end to end and assert on the return code.
- `.gitlab-ci.yml` only needs `--junit` + `artifacts:reports:junit` for jobs
  that actually run `deploy`/`preflight`. The README has an example.

## Done when

- `deploy` and `preflight` run under the dashboard in a terminal and as a
  plain log under `--ui plain` / CI, with correct exit codes in both.
- The binary built by `build_binary.sh` shows the dashboard.
- No `console.print` / `click.confirm` / `sys.exit` is reachable from inside
  `install(reporter)`.
- The root `CLAUDE.md` describes the final wiring, and this file is deleted.
