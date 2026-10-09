"""JUnit XML report for an install run.

Every mainstream CI system (GitLab, Jenkins, Azure DevOps, GitHub Actions via
a test-report action) renders JUnit XML, so writing one turns each install
step into a test case in the pipeline's test view - failures show up with
their full error detail without anyone digging through the job log.

Mapping:
    phase -> <testsuite name="Phase name">
    step  -> <testcase classname="phase.id" name="step.id">
             FAILED            -> <failure> with the error detail
             SKIPPED           -> <skipped> with the reason
             never started     -> <skipped message="not run">
             started, never ended (install aborted mid-step)
                               -> <error message="step did not finish">
             captured output   -> <system-out>

Test case names use the step id rather than its display name so CI test
history keeps tracking a step across runs even if its label is reworded.

The report is always derived from the ``InstallPlan`` after the run, so it
works the same whichever reporter (dashboard or plain) drove the install.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .models import InstallPlan, StepStatus
from .reporter import strip_ansi

# ANSI colour escapes are noise in a test report, and the raw ESC byte isn't
# legal XML 1.0 at all - leaving it in produces a file CI parsers reject
# outright. Other control characters are stripped for the same reason.
_INVALID_XML_CHARS = re.compile("[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")

_MAX_MESSAGE_LEN = 200


def _clean(text: str) -> str:
    return _INVALID_XML_CHARS.sub("", strip_ansi(text))


def _summary_line(text: str | None, fallback: str) -> str:
    """First non-blank line, for the one-line ``message`` attribute."""
    for line in _clean(text or "").splitlines():
        if line.strip():
            line = line.strip()
            return line if len(line) <= _MAX_MESSAGE_LEN else line[: _MAX_MESSAGE_LEN - 3] + "..."
    return fallback


def write_junit_report(plan: InstallPlan, path: str | Path, name: str = "install") -> None:
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0.0}
    root = ET.Element("testsuites", name=name)

    for phase in plan.phases:
        counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0.0}
        suite = ET.SubElement(root, "testsuite", name=phase.name, id=phase.id)

        for step in phase.steps:
            duration = step.duration or 0.0
            counts["tests"] += 1
            counts["time"] += duration
            case = ET.SubElement(
                suite, "testcase", classname=phase.id, name=step.id, time=f"{duration:.3f}"
            )

            if step.status == StepStatus.FAILED:
                counts["failures"] += 1
                failure = ET.SubElement(
                    case,
                    "failure",
                    message=_summary_line(step.error, "step failed"),
                    type="StepFailed",
                )
                failure.text = _clean(step.error or "(no error detail supplied)")
            elif step.status == StepStatus.SKIPPED:
                counts["skipped"] += 1
                ET.SubElement(case, "skipped", message=_summary_line(step.error, "skipped"))
            elif step.status == StepStatus.PENDING:
                counts["skipped"] += 1
                ET.SubElement(case, "skipped", message="not run")
            elif step.status == StepStatus.RUNNING:
                counts["errors"] += 1
                ET.SubElement(
                    case,
                    "error",
                    message="step did not finish (install aborted or crashed)",
                    type="StepIncomplete",
                )

            if step.output:
                ET.SubElement(case, "system-out").text = _clean("\n".join(step.output))

        _set_counts(suite, counts)
        for key in totals:
            totals[key] += counts[key]

    _set_counts(root, totals)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(root)
    ET.indent(tree)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def _set_counts(element: ET.Element, counts: dict[str, float]) -> None:
    for key in ("tests", "failures", "errors", "skipped"):
        element.set(key, str(int(counts[key])))
    element.set("time", f"{counts['time']:.3f}")
