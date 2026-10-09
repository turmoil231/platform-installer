"""
tests/unit/runner/test_ansible_reporting.py

AnsibleRunner reports each playbook run as one progress step: start once,
retries and Ansible output as output lines, one finish with error detail.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from installer.runner import ansible as ansible_mod
from installer.runner.ansible import AnsibleRunner, PlaybookResult

ANSIBLE_DIR = Path(__file__).resolve().parents[3] / "ansible"
STEP = "preflight.preflight"


class RecordingReporter:
    def __init__(self):
        self.events: list[tuple] = []

    def start_step(self, step_id):
        self.events.append(("start", step_id))

    def finish_step(self, step_id, success, error=None):
        self.events.append(("finish", step_id, success, error))

    def skip_step(self, step_id, reason=None):
        self.events.append(("skip", step_id, reason))

    def append_output(self, step_id, line):
        self.events.append(("output", step_id, line))

    def log(self, message, level="info"):
        self.events.append(("log", message, level))

    def of(self, kind):
        return [e for e in self.events if e[0] == kind]


def make_runner(tmp_path, **kwargs) -> tuple[AnsibleRunner, RecordingReporter]:
    runner = AnsibleRunner(
        ansible_dir=ANSIBLE_DIR, private_data_dir=tmp_path / "pdd",
        container_image="platform-ansible-exec:test", vault_addr="http://vault:8200",
        retry_delay=0, **kwargs,
    )
    runner.reporter = RecordingReporter()
    return runner, runner.reporter


def test_retries_are_output_lines_within_one_step(tmp_path):
    runner, reporter = make_runner(tmp_path, max_retries=2)
    results = iter([PlaybookResult(rc=2, status="failed"), PlaybookResult(rc=0, status="successful")])
    runner._execute = lambda *args: next(results)  # type: ignore[method-assign]

    result = runner.run_playbook("preflight.yml", step_id=STEP)

    assert result.success
    assert reporter.of("start") == [("start", STEP)]
    assert reporter.of("finish") == [("finish", STEP, True, None)]
    assert any("Retry 1/2" in e[2] for e in reporter.of("output"))


def test_failure_detail_has_hosts_and_last_failed_task(tmp_path):
    runner, reporter = make_runner(tmp_path, max_retries=0)
    runner._execute = lambda *args: PlaybookResult(  # type: ignore[method-assign]
        rc=2, status="failed", failed_hosts=["esxi-01"],
        last_failure='Last failed task: ping on esxi-01\n{"msg": "unreachable"}',
    )

    assert not runner.run_playbook("preflight.yml", step_id=STEP).success

    (_, _, success, error), = reporter.of("finish")
    assert not success
    assert "Failed hosts: esxi-01" in error
    assert "Last failed task: ping on esxi-01" in error


def test_failure_detail_falls_back_to_output_tail(tmp_path):
    runner, reporter = make_runner(tmp_path, max_retries=0)
    stdout = "\n".join(f"\x1b[0;31mline {i}\x1b[0m" for i in range(100))
    runner._execute = lambda *args: PlaybookResult(rc=4, status="failed", stdout=stdout)  # type: ignore[method-assign]

    runner.run_playbook("preflight.yml", step_id=STEP)

    error = reporter.of("finish")[0][3]
    assert "line 99" in error and "line 59" not in error
    assert "\x1b" not in error


def test_dry_run_skips_the_step(tmp_path):
    runner, reporter = make_runner(tmp_path, dry_run=True)
    assert runner.run_playbook("preflight.yml", step_id=STEP).success
    assert reporter.events == [("skip", STEP, "dry run")]


def test_missing_playbook_fails_the_step_then_raises(tmp_path):
    runner, reporter = make_runner(tmp_path)
    with pytest.raises(FileNotFoundError):
        runner.run_playbook("nope.yml", step_id="preflight.nope")
    assert reporter.of("start") == [("start", "preflight.nope")]
    assert reporter.of("finish")[0][2] is False


def test_exception_during_run_fails_the_step_then_raises(tmp_path):
    runner, reporter = make_runner(tmp_path)

    def boom(*args):
        raise RuntimeError("podman exploded")

    runner._execute = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        runner.run_playbook("preflight.yml", step_id=STEP)
    assert reporter.of("finish") == [("finish", STEP, False, "RuntimeError: podman exploded")]


def test_requires_an_attached_reporter(tmp_path):
    runner, _ = make_runner(tmp_path)
    runner.reporter = None
    with pytest.raises(RuntimeError, match="reporter"):
        runner.run_playbook("preflight.yml", step_id=STEP)


def test_event_handler_routes_output_and_captures_last_failure(tmp_path, monkeypatch):
    runner, reporter = make_runner(tmp_path, max_retries=0)
    runner._image_ready = True

    def fake_run(**kwargs):
        handler = kwargs["event_handler"]
        handler({"stdout": "TASK [check] ***\nok: [h1]"})
        handler({"event": "runner_on_failed", "stdout": "fatal: [h2]: FAILED!",
                 "event_data": {"task": "ignored", "host": "h2", "ignore_errors": True, "res": {}}})
        handler({"event": "runner_on_failed", "stdout": "fatal: [h3]: FAILED!",
                 "event_data": {"task": "check", "host": "h3", "res": {"msg": "boom"}}})
        return SimpleNamespace(rc=2, status="failed", stats={"failures": {"h3": 1}})

    monkeypatch.setattr(ansible_mod.ansible_runner, "run", fake_run)
    runner.run_playbook("preflight.yml", step_id=STEP)

    lines = [e[2] for e in reporter.of("output")]
    assert "TASK [check] ***" in lines and "ok: [h1]" in lines
    error = reporter.of("finish")[0][3]
    assert "Last failed task: check on h3" in error
    assert '"msg": "boom"' in error
    assert "Failed hosts: h3" in error


def test_vault_credentials_come_from_the_file_once_written_then_env_after_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_ROLE_ID", "env-role")
    monkeypatch.setenv("VAULT_SECRET_ID", "env-secret")
    creds = tmp_path / "approle.json"
    runner, _ = make_runner(tmp_path, vault_credentials_file=creds)

    assert runner._vault_credentials() == ("env-role", "env-secret")

    creds.write_text('{"role_id": "file-role", "secret_id": "file-secret"}')
    assert runner._vault_credentials() == ("file-role", "file-secret")

    runner.switch_vault_addr("https://vault.hub")
    assert runner._vault_credentials() == ("env-role", "env-secret")


def test_execution_containers_use_host_networking(tmp_path, monkeypatch):
    runner, _ = make_runner(tmp_path, max_retries=0)
    runner._image_ready = True
    seen = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(rc=0, status="successful", stats={})

    monkeypatch.setattr(ansible_mod.ansible_runner, "run", fake_run)
    runner.run_playbook("preflight.yml", step_id=STEP)
    assert seen["container_options"] == ["--network=host"]
