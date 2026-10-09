# installer/tui — Claude Code context

Progress UI for the installer: a Textual dashboard when a person is at a
terminal, and a plain, streaming log in CI or piped output. It also writes a
log file and a JUnit report, and sets the exit code. **Read `README.md` in this
directory before changing anything here.** It covers the API, the threading
model, the CI behaviour and the design reasons.

It was developed as a standalone prototype (`terminal-ui` repo, commit
`b999216`) and vendored here. It is now owned by this repo, so change it
freely. The upstream repo is no longer maintained.

It's wired into `deploy`/`preflight` through `installer/cli.py`
(`build_plan`, `make_install`). See the root `CLAUDE.md`.

## Boundary

- The installer talks to the UI **only** through the `Reporter` protocol
  (`start_step`, `finish_step`, `skip_step`, `append_output`, `log`).
  Phases and the Ansible runner must not import `app.py`/`controller.py` or
  know which mode is active.
- `run_installer(plan, install, ...)` is the single entry point. It picks
  the mode, runs `install(reporter)`, prints the summary, writes the JUnit
  report and log file, and **returns the exit code**.
- The `InstallPlan` is built up front, before anything runs. Step ids are
  stable strings such as `"<phase>.<playbook stem>"`. An unknown id raises
  `KeyError`.
- This package must not import anything from the rest of `installer/`. Keep
  it self-contained, with relative imports only.

## Invariants — don't break these

- **Thread safety:** only `InstallerUIController` (and the reporters) may be
  called from worker threads. Never call `InstallerApp` methods directly from
  a worker thread.
- **While the dashboard is up, it owns the terminal.** Anything else that
  writes to stdout/stderr or reads stdin during the run (`console.print`,
  `print`, `click.confirm`, `input`) corrupts the display or hangs. All
  in-run output goes through the reporter.
- **`install(reporter)` must not call `sys.exit()`.** In dashboard mode it
  runs on a background thread. `SystemExit` isn't an `Exception`, so
  `_run_tui` doesn't catch it: the thread dies silently and the dashboard
  sits there with a step stuck "running". Return or raise instead.
  `run_installer` works out the exit code from the plan.
- **Exit codes:** `0` only if every step succeeded or was skipped. `1` if any
  step failed, stayed pending/running, or `install` raised. `130` on Ctrl-C.
  Steps that intentionally won't run (already complete in the state store,
  not selected by `--phase`/`--from-phase`/`--to-phase`) must be
  `skip_step`ped, or the run reports as failed.
- **Output is rendered as plain text, never Rich markup.** Ansible output
  is full of `[brackets]`. Build coloured lines with `rich.text.Text`.
- **ASCII spinner** (`|/-\`). Serial and KVM consoles have limited fonts.
- **The log file is mandatory once it's requested.** If it can't be opened,
  the run refuses to start. Every event is written to the file before it
  goes to the dashboard.
- **The CSS is inlined in `app.py` (`CSS = """..."""`), not `CSS_PATH`**, so
  the PyInstaller `--onefile` binary has no data file to bundle. Keep it
  that way.

## Trying it

`python scripts/tui_demo.py` (add `--ui plain`, `--no-failure`, `--junit
PATH`) drives the UI with a simulated install. Use it to check UI changes
without real infrastructure.
