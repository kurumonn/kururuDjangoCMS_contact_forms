"""Fail CI when detect-secrets finds candidates, without printing secret values."""

from __future__ import annotations

import json
import os
import subprocess
import sys


def main() -> int:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    tracked_files = [os.fsdecode(path) for path in tracked if path]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "detect_secrets",
            "scan",
            *tracked_files,
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
