"""Fail-closed parsing for evaluator verdicts."""

import re

_VERDICT = re.compile(r"^VERDICT:\s*(PASS|FIX_REQUIRED|INSUFFICIENT_EVIDENCE|FAIL)$", re.IGNORECASE)


def terminal_verdict(report: str) -> str | None:
    """Return one unambiguous verdict only when it is the final non-empty line."""
    lines = [line.strip() for line in report.splitlines() if line.strip()]
    matches = [match for line in lines if (match := _VERDICT.fullmatch(line))]
    if len(matches) != 1 or not lines:
        return None
    final_match = _VERDICT.fullmatch(lines[-1])
    if final_match is None:
        return None
    return final_match.group(1).upper()
