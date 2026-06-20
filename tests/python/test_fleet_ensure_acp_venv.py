#!/usr/bin/env python3
"""Hermetic tests for the remote fleet ACP venv repair helper."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def test_uv_repair_clears_existing_broken_venv() -> None:
    script = ROOT / "scripts" / "hosts" / "fleet" / "ensure_acp_venv.sh"
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = tmp / "home"
        fake_bin = tmp / "bin"
        log_path = tmp / "uv.log"
        venv = home / ".goal-flight" / "venvs" / "acp-0.10"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        (venv / "bin" / "python").chmod(0o755)
        fake_bin.mkdir()
        uv = fake_bin / "uv"
        uv.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$UV_LOG"
if [[ "$1" == "venv" ]]; then
  [[ "${2:-}" == "--clear" ]] || exit 23
  target="$3"
  rm -rf "$target"
  mkdir -p "$target/bin"
  cat > "$target/bin/python" <<'PY'
#!/usr/bin/env bash
if [[ "$*" == *"import acp"* ]]; then
  echo "ok: ACP venv ready"
  exit 0
fi
exit 0
PY
  chmod +x "$target/bin/python"
  exit 0
fi
if [[ "$1" == "pip" && "${2:-}" == "install" ]]; then
  exit 0
fi
exit 24
""",
            encoding="utf-8",
        )
        uv.chmod(0o755)
        env = dict(os.environ)
        env["HOME"] = str(home)
        env["PATH"] = f"{fake_bin}{os.pathsep}/usr/bin:/bin"
        env["UV_LOG"] = str(log_path)

        run = subprocess.run(
            ["bash", str(script)],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert_true("repair succeeds", run.returncode == 0)
        log = log_path.read_text(encoding="utf-8")
        assert_true("uv venv clear used", f"venv --clear {venv}" in log)
        assert_true("uv pip install used", "pip install --python" in log)


def main() -> None:
    test_uv_repair_clears_existing_broken_venv()
    print("OK: fleet ensure ACP venv tests pass")


if __name__ == "__main__":
    main()
