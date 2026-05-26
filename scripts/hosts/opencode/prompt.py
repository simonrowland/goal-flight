#!/usr/bin/env python3
"""Reliable one-shot OpenCode prompt via the HTTP API.

OpenCode's `run` subcommand can hang for minutes on large repos while snapshot
cleanup runs, and concurrent `serve` + `run` processes deadlock on opencode.db.
This helper keeps a single headless server (or attaches to one) and sends the
prompt through POST /session/{id}/message.

Usage:
  source ~/.config/rpp/litellm.env
  ./scripts/hosts/opencode/prompt.py -m litellm/nano "Reply with one word: pong"
  ./scripts/hosts/opencode/prompt.py --routing-test
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PORT = 4096
DEFAULT_MODEL = "litellm/frontier-coder"
ROUTING_PROMPT = (
    "You are testing Goal Flight skill routing. Reply with exactly three lines:\n"
    "(1) first file to load for goal-flight per AGENTS.md\n"
    "(2) canonical workflow path name\n"
    "(3) model id you are using"
)
EXPECTED_ROUTING = ("AGENTS.md", "SKILL.md")


def _load_litellm_env() -> None:
    if os.environ.get("LITELLM_API_KEY") or os.environ.get("LITELLM_MASTER_KEY"):
        return
    env_file = Path.home() / ".config/rpp/litellm.env"
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def _parse_model(model: str) -> tuple[str, str]:
    if "/" in model:
        provider, model_id = model.split("/", 1)
        return provider, model_id
    return "litellm", model


def _request(base: str, directory: str, method: str, path: str, body: dict | None = None, timeout: float = 120) -> object:
    query = urllib.parse.urlencode({"directory": str(directory)})
    url = f"{base}{path}?{query}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return None
        return json.loads(raw)


def _health_ok(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/global/health", timeout=2) as resp:
            payload = json.loads(resp.read())
        return payload.get("healthy") is True
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return False


def _start_server(port: int, directory: Path, log_path: Path) -> subprocess.Popen[bytes]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab")
    cmd = [
        "opencode",
        "serve",
        "--port",
        str(port),
        "--hostname",
        "127.0.0.1",
    ]
    env = os.environ.copy()
    return subprocess.Popen(
        cmd,
        cwd=str(directory),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )


def _wait_for_health(base: str, timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _health_ok(base):
            return
        time.sleep(0.5)
    raise RuntimeError(f"OpenCode server at {base} did not become healthy within {timeout_s:.0f}s")


def _collect_texts(messages: list[dict]) -> list[str]:
    texts: list[str] = []
    for message in messages:
        for part in message.get("parts", []):
            if part.get("type") == "text" and part.get("text"):
                texts.append(part["text"])
    return texts


def _latest_assistant_reply(messages: list[dict]) -> str | None:
    for message in reversed(messages):
        if message.get("info", {}).get("role") != "assistant":
            continue
        texts = [
            part.get("text", "")
            for part in message.get("parts", [])
            if part.get("type") == "text" and part.get("text")
        ]
        if texts:
            return "\n".join(texts).strip()
    texts = _collect_texts(messages)
    return texts[-1].strip() if texts else None


def _wait_for_reply(
    base: str,
    directory: str,
    session_id: str,
    *,
    timeout_s: float,
    poll_s: float,
    previous_count: int,
) -> str:
    deadline = time.time() + timeout_s
    last_text = ""
    while time.time() < deadline:
        messages = _request(base, directory, "GET", f"/session/{session_id}/message")
        if not isinstance(messages, list):
            time.sleep(poll_s)
            continue
        reply = _latest_assistant_reply(messages)
        if reply and len(messages) > previous_count and reply != last_text:
            status = _request(base, directory, "GET", "/session/status")
            session_status = status.get(session_id, {}) if isinstance(status, dict) else {}
            if not isinstance(session_status, dict) or session_status.get("type") in (None, "idle", "completed"):
                return reply
            if session_status.get("type") == "busy":
                last_text = reply
                time.sleep(poll_s)
                continue
            return reply
        time.sleep(poll_s)
    raise RuntimeError(f"Timed out after {timeout_s:.0f}s waiting for assistant reply")


def prompt_once(
    message: str,
    *,
    directory: Path,
    model: str,
    port: int,
    boot_timeout_s: float,
    reply_timeout_s: float,
    keep_server: bool,
    log_path: Path,
) -> str:
    base = f"http://127.0.0.1:{port}"
    provider_id, model_id = _parse_model(model)
    started_server = False
    server_proc: subprocess.Popen[bytes] | None = None

    if not _health_ok(base):
        server_proc = _start_server(port, directory, log_path)
        started_server = True
        try:
            _wait_for_health(base, boot_timeout_s)
        except RuntimeError:
            if server_proc.poll() is None:
                os.killpg(server_proc.pid, signal.SIGTERM)
            raise

    try:
        session = _request(
            base,
            str(directory),
            "POST",
            "/session",
            {
                "title": message.splitlines()[0][:80],
                "model": {"id": model_id, "providerID": provider_id},
            },
        )
        if not isinstance(session, dict) or "id" not in session:
            raise RuntimeError(f"Unexpected session response: {session!r}")
        session_id = session["id"]

        before = _request(base, str(directory), "GET", f"/session/{session_id}/message")
        previous_count = len(before) if isinstance(before, list) else 0

        _request(
            base,
            str(directory),
            "POST",
            f"/session/{session_id}/message",
            {"parts": [{"type": "text", "text": message}]},
            timeout=max(reply_timeout_s, 120),
        )

        return _wait_for_reply(
            base,
            str(directory),
            session_id,
            timeout_s=reply_timeout_s,
            poll_s=1.0,
            previous_count=previous_count,
        )
    finally:
        if started_server and not keep_server and server_proc is not None and server_proc.poll() is None:
            os.killpg(server_proc.pid, signal.SIGTERM)
            try:
                server_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(server_proc.pid, signal.SIGKILL)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send one prompt to OpenCode via HTTP API")
    parser.add_argument("message", nargs="*", help="Prompt text")
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL, help=f"provider/model (default: {DEFAULT_MODEL})")
    parser.add_argument("--directory", "-C", default=str(REPO_ROOT), help="Project directory")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"OpenCode serve port (default: {DEFAULT_PORT})")
    parser.add_argument("--boot-timeout", type=float, default=120.0, help="Seconds to wait for server health")
    parser.add_argument("--timeout", type=float, default=180.0, help="Seconds to wait for model reply")
    parser.add_argument("--keep-server", action="store_true", help="Leave opencode serve running after success")
    parser.add_argument("--routing-test", action="store_true", help="Run Goal Flight routing smoke test")
    parser.add_argument("--log", default=str(Path("/tmp/opencode-serve.log")), help="Serve log path when auto-starting")
    args = parser.parse_args()

    _load_litellm_env()
    directory = Path(args.directory).resolve()
    message = ROUTING_PROMPT if args.routing_test else " ".join(args.message).strip()
    if not message:
        parser.error("message is required unless --routing-test is set")

    try:
        reply = prompt_once(
            message,
            directory=directory,
            model=args.model,
            port=args.port,
            boot_timeout_s=args.boot_timeout,
            reply_timeout_s=args.timeout,
            keep_server=args.keep_server,
            log_path=Path(args.log),
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(reply)
    if args.routing_test:
        lines = [line.strip() for line in reply.splitlines() if line.strip()]
        if len(lines) < 2 or EXPECTED_ROUTING[0] not in lines[0] or EXPECTED_ROUTING[1] not in lines[1]:
            print(
                f"ERROR: routing test failed; expected {EXPECTED_ROUTING[0]} and {EXPECTED_ROUTING[1]}",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
