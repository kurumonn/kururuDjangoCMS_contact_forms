"""Fail CI when detect-secrets finds candidates, without printing secret values."""

from __future__ import annotations

import json
import subprocess
import sys


def main() -> int:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "detect_secrets",
            "scan",
            "--all-files",
            "--exclude-files",
            r"(^|/)(cms|\.git|build|dist|.*\.egg-info)/",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        print(completed.stderr, file=sys.stderr)
        return completed.returncode

    report = json.loads(completed.stdout)
    findings = report.get("results", {})
    if findings:
        print("Potential secrets found in:")
        for path in sorted(findings):
            print(f"- {path}")
        return 1
    print("detect-secrets: no candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
