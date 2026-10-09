# Installer TUI (`installer.tui`)

A terminal dashboard for a Python/ansible-runner installer that bootstraps
an airgapped platform. This package is only the UI layer - it has no opinion
about how the real installer is structured, only about the small API it
calls to report progress.

It shows, live:

- an overall progress bar for the whole install
- every step, grouped into phases, with status icons (pending / running /
  succeeded / failed / skipped)
- what's currently running, including steps running in parallel
- a scrolling output log, and a separate **Errors** tab that collects full
  failure detail (e.g. Ansible task output) for debugging - it badges with
  a count and turns red the moment something fails

## Running the demo

From the repo root, with the installer installed in your venv
(`pip install -e .`):

```bash
python scripts/tui_demo.py
```

`scripts/tui_demo.py` stands in for the real installer: it builds a plan modeled on a
plausible airgapped bootstrap (preflight checks, storage provisioning,
control-plane bootstrap, service deployment, validation), then drives the
UI from background threads - some phases sequential, some parallel - and
scripts one step to fail with a realistic Ansible-style error so you can
see the Errors tab in action.

Keys: `q` quit, `e` jump to the Errors tab, `o` jump to the Output tab.
Phases in the step tree can be expanded/collapsed by clicking or with the
arrow keys.

Demo flags (the real installer would expose all but the last the same way):

| flag | effect |
|---|---|
| `--ui {auto,tui,plain}` | `auto` (default) shows the dashboard in a terminal and plain log output otherwise - see [Running in CI](#running-in-ci) |
| `--junit PATH` | write a JUnit XML report of the run to `PATH` |
| `--log-file PATH` | append a full log of the run to `PATH` (default `install.log`) - see [Log file](#log-file) |
| `--exit-when-done` | close the dashboard when the install finishes instead of waiting for `q` |
| `--no-failure` | demo only: don't script the registry-mirror failure, so the run exits 0 |

## Running in CI

The same installer runs unattended in a pipeline. With `--ui auto` (the
default) it switches to plain output by itself whenever `CI` is set (GitLab,
GitHub Actions, CircleCI and most others set it), `TERM=dumb`, or stdin/stdout
isn't a terminal (Jenkins, piped/redirected output). In plain mode:

- **Output is a streaming, line-per-event log** - timestamped, flushed every
  line, with ASCII status markers so it's greppable:

  ```
  [21:07:42] START services.deploy_registry_mirror: Deploy container registry mirror
  [21:07:43]       services.deploy_registry_mirror | starting registry-mirror.internal:5000
  [21:07:45] FAIL  services.deploy_registry_mirror (00:02)
             | TASK [registry_mirror : wait for registry TLS listener] ******
             | fatal: [paas-svc-01]: FAILED! => {...}
  ```

  Failure detail (what the dashboard puts in the Errors tab) is printed in
  full, right where the step failed.
- **The run ends by itself**, with an install summary as the last thing in
  the log.
- **The exit code gates the pipeline:** `0` only if every step succeeded or
  was skipped; `1` if any step failed, any step never ran or never finished,
  or the installer itself raised; `130` on Ctrl-C. The dashboard returns the
  same codes when it closes.
- **`--junit PATH` writes a JUnit XML report** (one `<testsuite>` per phase,
  one `<testcase>` per step, failure detail and captured output included,
  ANSI colour codes stripped), so failures show up in the CI's test view.
  It's written however the run ends, including on failure.

GitLab CI example:

```yaml
bootstrap:
  script:
    - python scripts/tui_demo.py --junit reports/install.xml --log-file reports/install.log
  artifacts:
    when: always
    paths:
      - reports/install.log
    reports:
      junit: reports/install.xml
```

`--ui plain` forces plain mode anywhere, and `--ui tui --exit-when-done`
gives a dashboard that still closes on its own, for unattended runs that
have a terminal attached.

## Log file

With a log path set (`--log-file` in the demo, `log_path=` on
`run_installer`), every event is also written to a file, in either mode.
This matters most for the dashboard: its Output and Errors tabs are gone
the moment it closes, and the file is what's left to investigate or attach
to a support ticket. It holds:

- the same lines as plain mode's stdout - every step start/finish/skip, all
  step output, and full failure detail - but with full dates in the
  timestamps and ANSI colour codes stripped, so it reads cleanly in an
  editor or `less`;
- the install summary at the end, and the traceback if the installer
  itself crashed;
- everything up to the moment the run stopped, however it stopped: each
  line is flushed as it's written, and an event is written to the file
  before it's sent to the dashboard.

The file is **appended to**, with a header line marking the start of each
run, so re-running after a failure doesn't overwrite the log of the run that
failed. If the file can't be opened, the install refuses to start rather
than running with no record.

The real installer should default this to somewhere durable, e.g.
`/var/log/<installer>/install.log`, rather than leaving it opt-in.

## Layout

```
installer/tui/
  models.py      StepStatus, Step, Phase, InstallPlan - plain dataclasses
  app.py          InstallerApp(App) - the Textual dashboard itself
  controller.py  InstallerUIController - thread-safe Reporter that drives the dashboard
  reporter.py    Reporter interface; LogWriter (line-per-event log), PlainReporter
                 (LogWriter on stdout that also records state), TeeReporter
  runner.py      run_installer() - picks dashboard vs plain, returns the exit code
  junit.py       write_junit_report() - JUnit XML from a finished InstallPlan
  (dashboard CSS is inlined in app.py so PyInstaller builds need no data files)
scripts/tui_demo.py  Simulated installer driving the UI (not part of the real installer)
```

`InstallPlan`/`Phase`/`Step` are just data - build the whole plan up front
from however your installer represents its playbooks/roles, and pass it
into `InstallerApp(plan)`. From then on, nothing needs to touch the plan
directly; a reporter is the only thing the installer talks to.

## Wiring it to the real installer

Write the orchestration as a function that takes a `Reporter` and hand it
to `run_installer`, which decides between the dashboard and plain output,
runs it, prints the summary, writes the JUnit report and returns the exit
code:

```python
import sys
from installer.tui import Reporter, run_installer

def install(reporter: Reporter) -> None:
    ...  # run playbooks, reporting progress through `reporter`

plan = build_plan()                      # plan built from your playbooks/roles
sys.exit(run_installer(
    plan, install, ui=args.ui, junit_path=args.junit, log_path=args.log_file
))
```

`Reporter` has five methods: `start_step`, `finish_step`, `skip_step`,
`append_output`, `log`. That's the entire surface the orchestration layer
needs to know about. In dashboard mode it's an `InstallerUIController`
(`run_installer` runs `install` on a background thread, since the
dashboard owns the main thread); in plain mode it's a `PlainReporter`. With
a log file, either one is wrapped in a `TeeReporter` that writes each event
to the file's `LogWriter` first. All of them are safe to call from any
thread.

### ansible-runner callbacks

Textual widgets aren't thread-safe, and `ansible_runner.run_async()`
fires its callbacks from a worker thread it manages - so route them
straight through the reporter, which handles that for you (the dashboard's
controller marshals each call onto the UI's own thread):

```python
def make_callbacks(reporter, step_id):
    def event_handler(data):
        # per-task output as it streams
        if data.get("stdout"):
            reporter.append_output(step_id, data["stdout"])
        if data.get("event") == "runner_on_failed":
            res = data.get("event_data", {}).get("res", {})
            error_detail = res.get("stderr") or res.get("msg") or str(res)
            reporter.append_output(step_id, f"FAILED: {error_detail}")

    def status_handler(status_data, runner_config):
        status = status_data.get("status")
        if status == "running":
            reporter.start_step(step_id)
        elif status in ("successful", "failed", "timeout", "canceled"):
            reporter.finish_step(step_id, success=(status == "successful"))

    return event_handler, status_handler

event_handler, status_handler = make_callbacks(reporter, step_id="control_plane.etcd")
ansible_runner.run_async(
    private_data_dir=...,
    playbook="bootstrap_etcd.yml",
    event_handler=event_handler,
    status_handler=status_handler,
)
```

If you want richer failure detail in the Errors tab than a one-line
status, accumulate it from `event_handler` (e.g. the last `runner_on_failed`
event's `res`) and pass it as `error=` to `finish_step` once the job
settles - that's exactly what `scripts/tui_demo.py`'s scripted failure does, just with
a canned string instead of a real Ansible result.

## Design notes / why some things look the way they do

- **`InstallerUIController` is the only thread-safe entry point.** Every
  `InstallerApp` method it wraps is only ever called on the app's own
  thread - either directly (if you're already on it) or via
  `App.call_from_thread` (from anywhere else). Don't call `InstallerApp`
  methods directly from a worker thread.
- **The output/error logs render everything as plain text, not markup.**
  Real installer output - Ansible task names, JSON blobs, IPs - routinely
  contains `[brackets]`, and Rich's markup parser treats `[...]` as a style
  tag and silently swallows it. Colored lines are built with `rich.text.Text`
  objects instead of markup strings so arbitrary output can't corrupt the
  display (or, worse, throw).
- **The running-step spinner uses a plain ASCII `|/-\` cycle, not a
  Unicode braille spinner.** This is a bootstrap console for bare-metal/
  airgapped hardware, plausibly reached over a serial or KVM console with a
  limited font - so the spinner sticks to characters that are essentially
  guaranteed to render anywhere.

## Known gaps (it's a prototype)

- No pause/cancel/resume controls - the real installer likely wants a
  `--resume-from <step>` flag of its own (see the demo's error message)
  rather than the UI trying to control execution.
- No log rotation - the log file grows by one run per invocation. Fine for
  an installer that runs a handful of times; use logrotate if that changes.
- No search/filter over the output log.
