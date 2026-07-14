"""Test-only command-wrapper shim used by the opt-in model_sandbox_backend fixture."""
from __future__ import annotations

import base64
import subprocess
import sys


def main() -> int:
    command = base64.b64decode(sys.argv[1]).decode("utf-8")
    return subprocess.run(command, shell=True).returncode


if __name__ == "__main__":
    raise SystemExit(main())
