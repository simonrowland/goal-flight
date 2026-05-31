#!/usr/bin/env python3
"""Hermetic guard for the real-WSL smoke status validator."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("WSL smoke gate launches a bash test script")

import os
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "tests" / "bash" / "test-wsl-dispatch-smoke.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def case_wrong_status_json_fails_smoke() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        fake_bin = tmp / "bin"
        fake_bin.mkdir()
        _write_executable(fake_bin / "grep", "#!/usr/bin/env bash\nexit 0\n")
        _write_executable(
            fake_bin / "python3",
            textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                if [[ "$1" == */scripts/goalflight_dispatch.py ]]; then
                  status=""
                  while [[ "$#" -gt 0 ]]; do
                    case "$1" in
                      --status-json)
                        status="$2"
                        shift 2
                        ;;
                      *)
                        shift
                        ;;
                    esac
                  done
                  printf '%s\\n' '{{"state":"running","terminal_marker":{{"kind":"STATUS"}}}}' > "$status"
                  exit 0
                fi
                exec "{sys.executable}" "$@"
                """
            ),
        )
        env = dict(os.environ)
        env["GOALFLIGHT_WSL"] = "1"
        env["TMPDIR"] = str(tmp)
        env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
        proc = subprocess.run(
            ["bash", str(SMOKE)],
            cwd=str(ROOT),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "OK: WSL dispatch smoke passed" not in proc.stdout
    assert "expected complete" in proc.stdout or "expected complete" in proc.stderr


def main() -> None:
    case_wrong_status_json_fails_smoke()
    print("OK: WSL smoke gate tests pass")


if __name__ == "__main__":
    main()
