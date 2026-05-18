# ACP Quickstart

All examples assume:
```python
from pathlib import Path
repo = Path(__file__).resolve().parent.parent  # adjust for your script location
```

## 1. Install ACP Adapters
Install adapter CLIs on `PATH`: `codex-acp`, `claude-code-cli-acp`, etc.
```python
agents_config = {"codex": {"command": "codex-acp", "acp_args": [], "working_dir": str(repo)}}
```

## 2. Spawn Via Managed Pool
```python
async with managed_pool(agents_config, env_caveats_path=repo / "docs-private" / "env-caveats.md") as pool:
    conn = await pool.get_or_create("codex", "quickstart", cwd=str(repo))
```

## 3. Send Prompt With Run Prompt
```python
prompt = "STATUS: starting\nCOMPLETE: quickstart checked"
result = await run_prompt(conn, prompt, idle_timeout=180)
```

## 4. Extract Markers From Result Text
```python
markers = extract_markers(result.text)
complete_lines = markers.get("COMPLETE", [])
```

## 5. Cleanup On Context Exit
```python
# Leaving managed_pool context calls pool.shutdown(); no manual kill needed.
```
