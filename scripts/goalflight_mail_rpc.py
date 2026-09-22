#!/usr/bin/env python3
"""Authenticated peek/post/relay window onto the journal host.

The journal on the host that holds ``~/.goal-flight`` stays the only mail
store. This process does not open a second inbox. It checks a bearer token,
then runs the same ``goalflight_messages.py`` commands an operator would:
``relay --new`` / ``relay --drain`` and ``post``.

Wake webhooks stay nudge-only and are not served here.

Bind defaults to loopback. ``0.0.0.0`` and ``::`` refuse to listen unless
``GOALFLIGHT_MAIL_RPC_ALLOW_PUBLIC_BIND=1``. Pin
``GOALFLIGHT_CONTROLLER_LABEL`` on the daemon so a caller cannot drain
another controller's mailbox.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Mapping
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


TOKEN_ENV = "GOALFLIGHT_MAIL_RPC_TOKEN"
BIND_ENV = "GOALFLIGHT_MAIL_RPC_BIND"
ALLOW_PUBLIC_BIND_ENV = "GOALFLIGHT_MAIL_RPC_ALLOW_PUBLIC_BIND"
URL_ENVS = ("GOALFLIGHT_MAIL_RPC_URL", "MAIL_RPC_URL")
CLIENT_TOKEN_ENVS = ("GOALFLIGHT_MAIL_RPC_TOKEN", "MAIL_RPC_TOKEN")
LABEL_HEADER = "X-Goalflight-Controller-Label"
DEFAULT_BIND = "127.0.0.1:8787"
MAX_BODY_BYTES = 256 * 1024
INVOKE_TIMEOUT_S = 60.0
MIN_TOKEN_LEN = 16
USER_AGENT = "goalflight-mail-rpc/1"
SERVICE_NAME = "goalflight-mail-rpc"

_PUBLIC_BIND_HOSTS = frozenset({"0.0.0.0", "::", "::0", "*"})
_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,63}\Z")
_TRUE_FLAGS = frozenset({"1", "true", "yes", "on"})
_FALSE_FLAGS = frozenset({"0", "false", "no", "off"})

SCRIPT_DIR = Path(__file__).resolve().parent
MESSAGES_SCRIPT = SCRIPT_DIR / "goalflight_messages.py"


class MailRpcError(Exception):
    """Client or config failure with an HTTP status."""

    status = 400
    error = "bad_request"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class Unauthorized(MailRpcError):
    status = 401
    error = "unauthorized"


class Forbidden(MailRpcError):
    status = 403
    error = "forbidden"


class PayloadTooLarge(MailRpcError):
    status = 413
    error = "payload_too_large"


@dataclass(frozen=True)
class MailRpcConfig:
    token: str
    bind_host: str
    bind_port: int
    controller_label: str
    project_root: str
    invoke_timeout_s: float = INVOKE_TIMEOUT_S


def parse_bind(raw: str, *, allow_public: bool) -> tuple[str, int]:
    """Parse ``host:port`` or ``[ipv6]:port``. Wildcard binds need an opt-in."""
    text = str(raw or "").strip()
    if not text:
        raise MailRpcError(f"{BIND_ENV} is empty")
    if text.startswith("["):
        end = text.find("]")
        if end < 2 or not text[end:].startswith("]:"):
            raise MailRpcError(f"{BIND_ENV} must be host:port or [ipv6]:port")
        host = text[1:end]
        port_text = text[end + 2 :]
    else:
        host, sep, port_text = text.rpartition(":")
        if not sep or not host or not port_text:
            raise MailRpcError(f"{BIND_ENV} must be host:port (default {DEFAULT_BIND})")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise MailRpcError(f"{BIND_ENV} port must be an integer") from exc
    if port < 1 or port > 65535:
        raise MailRpcError(f"{BIND_ENV} port must be in 1..65535")
    if host in _PUBLIC_BIND_HOSTS and not allow_public:
        raise MailRpcError(
            f"refusing public bind {host}; set {ALLOW_PUBLIC_BIND_ENV}=1 to opt in. "
            f"Bind a Tailscale or loopback address instead (default {DEFAULT_BIND})"
        )
    return host, port


def load_config() -> MailRpcConfig:
    """Read daemon config from the environment. The token is never logged."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if len(token) < MIN_TOKEN_LEN:
        raise MailRpcError(
            f"{TOKEN_ENV} must be at least {MIN_TOKEN_LEN} characters "
            "(generate with openssl rand -hex 32 on the journal host)"
        )
    allow_public = os.environ.get(ALLOW_PUBLIC_BIND_ENV, "").strip() == "1"
    host, port = parse_bind(
        os.environ.get(BIND_ENV, DEFAULT_BIND),
        allow_public=allow_public,
    )
    label = os.environ.get("GOALFLIGHT_CONTROLLER_LABEL", "").strip()
    if label and _LABEL_RE.fullmatch(label) is None:
        raise MailRpcError("GOALFLIGHT_CONTROLLER_LABEL is not a bounded identity token")
    root = os.environ.get("GOALFLIGHT_PROJECT_ROOT", "").strip()
    return MailRpcConfig(
        token=token,
        bind_host=host,
        bind_port=port,
        controller_label=label,
        project_root=root,
    )


def token_matches(header: str | None, token: str) -> bool:
    """Compare ``Authorization: Bearer`` without leaking the token length."""
    if not header or not token:
        return False
    scheme, sep, presented = header.strip().partition(" ")
    if not sep or scheme.lower() != "bearer":
        return False
    presented = presented.strip()
    if not presented or presented != header.strip()[len(scheme) + 1 :].strip():
        return False
    # Extra whitespace after the token is a mismatch (already stripped once).
    if " " in presented:
        return False
    presented_digest = hashlib.sha256(presented.encode("utf-8")).digest()
    token_digest = hashlib.sha256(token.encode("utf-8")).digest()
    return hmac.compare_digest(presented_digest, token_digest)


def _clean_label(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise MailRpcError("controller_label must be a string")
    return value.strip()


def resolve_identity(
    config: MailRpcConfig,
    *,
    header_label: str | None,
    body_label: object = None,
) -> str:
    """Honor the pinned daemon label, else the request header or body.

    A pinned ``GOALFLIGHT_CONTROLLER_LABEL`` is the only mailbox this
    process may relay or drain. A different requested label is forbidden.
    """
    header = _clean_label(header_label)
    body = _clean_label(body_label)
    if header and body and header != body:
        raise Forbidden("controller label header and body disagree")
    requested = header or body
    pinned = config.controller_label
    if pinned:
        if requested and requested != pinned:
            raise Forbidden("refusing mail for a different controller")
        return pinned
    if not requested:
        raise MailRpcError(
            "controller label required; set GOALFLIGHT_CONTROLLER_LABEL on the "
            f"daemon or send {LABEL_HEADER}"
        )
    if _LABEL_RE.fullmatch(requested) is None:
        raise MailRpcError("controller label is not a bounded identity token")
    return requested


def resolve_project_root(requested: object, config: MailRpcConfig) -> Path:
    """Use the request path, else ``GOALFLIGHT_PROJECT_ROOT``, else the checkout."""
    if requested is not None and not isinstance(requested, str):
        raise MailRpcError("project_root must be a string")
    raw = str(requested or "").strip() or config.project_root
    if raw:
        path = Path(raw).expanduser()
        if not path.is_dir():
            raise MailRpcError("project_root is not a directory on the journal host")
        return path
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import goalflight_journal  # noqa: F401  # puts the repo root on sys.path
    import goalflight_task

    found = goalflight_task.resolve_project_root_for_read(None)
    if found is None:
        raise MailRpcError(
            "project root is unavailable; pass project_root or set GOALFLIGHT_PROJECT_ROOT"
        )
    return found


def _optional_str(body: Mapping[str, object], key: str) -> str | None:
    if key not in body or body[key] is None:
        return None
    value = body[key]
    if not isinstance(value, str):
        raise MailRpcError(f"{key} must be a string")
    return value


def _flag_mode(value: object, *, source: str) -> str:
    if isinstance(value, bool):
        return "drain" if value else "peek"
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"peek", "drain"}:
            return text
        if text in _TRUE_FLAGS:
            return "drain"
        if text in _FALSE_FLAGS:
            return "peek"
    raise MailRpcError(f"{source} must be peek or drain")


def relay_mode(query: Mapping[str, list[str]], body: Mapping[str, object]) -> str:
    """Peek unless query or body explicitly asks to drain. Flags must agree."""
    chosen: list[str] = []
    if "mode" in query and query["mode"]:
        chosen.append(_flag_mode(query["mode"][-1], source="mode"))
    if "drain" in query and query["drain"]:
        chosen.append(_flag_mode(query["drain"][-1], source="drain"))
    if "mode" in body and body["mode"] is not None:
        chosen.append(_flag_mode(body["mode"], source="mode"))
    if "drain" in body and body["drain"] is not None:
        chosen.append(_flag_mode(body["drain"], source="drain"))
    if not chosen:
        return "peek"
    if any(mode != chosen[0] for mode in chosen):
        raise MailRpcError("peek/drain flags disagree")
    return chosen[0]


def _query_map(path: str) -> dict[str, list[str]]:
    parsed = urllib_parse.urlparse(path)
    return urllib_parse.parse_qs(parsed.query, keep_blank_values=False)


def _child_env(identity: str) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_CONTROLLER_LABEL"] = identity
    # A leftover worker id would change post addressing. This RPC is a controller.
    env.pop("GOALFLIGHT_DISPATCH_ID", None)
    return env


def run_messages(
    argv: list[str],
    *,
    identity: str,
    cwd: Path,
    timeout_s: float,
) -> tuple[int, str, str]:
    """Run the messages CLI. Callers map its exit code; they do not reimplement SQL."""
    if not MESSAGES_SCRIPT.is_file():
        raise MailRpcError("goalflight_messages.py is not beside the mail RPC")
    try:
        completed = subprocess.run(
            [sys.executable, str(MESSAGES_SCRIPT), *argv],
            cwd=str(cwd),
            env=_child_env(identity),
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MailRpcError("messages CLI timed out") from exc
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def cli_payload(code: int, stdout: str, stderr: str) -> tuple[int, dict[str, object]]:
    text = stdout.strip()
    parsed: object = None
    if text:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = {"stdout": text[:2000]}
    if code == 0:
        status = 200
    elif code == 1:
        status = 422
    elif code == 3:
        status = 409
    elif code == 2:
        status = 400
    else:
        status = 500
    body: dict[str, object] = {"ok": code == 0, "exit_code": code}
    if parsed is not None:
        body["result"] = parsed
    if code != 0:
        if code == 3:
            body["error"] = "conflict"
        elif code in {1, 2}:
            body["error"] = "refused"
        else:
            body["error"] = "failed"
        detail = stderr.strip()
        if detail:
            body["detail"] = detail[:2000]
    return status, body


def _write_text_file(text: str) -> str:
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix="goalflight-mail-rpc-",
        suffix=".txt",
        delete=False,
    )
    try:
        handle.write(text)
        handle.close()
    except Exception:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
        raise
    return handle.name


def build_relay_argv(
    *,
    mode: str,
    project_root: Path,
    identity: str,
    body: Mapping[str, object],
) -> list[str]:
    argv = [
        "relay",
        "--json",
        "--project-root",
        str(project_root),
        "--controller-label",
        identity,
    ]
    if mode == "drain":
        argv.append("--drain")
        return argv
    argv.append("--new")
    summary = body.get("summary_only")
    if summary is True:
        argv.append("--summary-only")
    elif summary not in {None, False}:
        raise MailRpcError("summary_only must be a boolean")
    since = _optional_str(body, "since")
    if since:
        argv.extend(["--since", since])
    return argv


def build_post_argv(
    *,
    project_root: Path,
    body: Mapping[str, object],
    text_file: str | None,
) -> list[str]:
    dispatch_id = _optional_str(body, "dispatch_id")
    msg_type = _optional_str(body, "type")
    if not dispatch_id:
        raise MailRpcError("dispatch_id is required")
    if not msg_type:
        raise MailRpcError("type is required")
    text = _optional_str(body, "text")
    payload = body.get("payload", None)
    if text is not None and payload is not None:
        raise MailRpcError("text and payload are mutually exclusive")
    if payload is not None and not isinstance(payload, dict):
        raise MailRpcError("payload must be a JSON object")
    argv = [
        "post",
        "--json",
        "--dispatch-id",
        dispatch_id,
        "--type",
        msg_type,
        "--transport",
        "controller",
    ]
    to_controller = _optional_str(body, "to_controller")
    if to_controller:
        argv.extend(["--to-controller", to_controller])
    controller_root = _optional_str(body, "controller_project_root")
    argv.extend(
        [
            "--controller-project-root",
            controller_root or str(project_root),
        ]
    )
    subject = _optional_str(body, "subject")
    if subject:
        argv.extend(["--subject", subject])
    node = _optional_str(body, "node")
    if node:
        argv.extend(["--node", node])
    adapter = _optional_str(body, "adapter")
    if adapter:
        argv.extend(["--adapter", adapter])
    if payload is not None:
        encoded = json.dumps(payload, sort_keys=True)
        if len(encoded) > 32_000:
            raise MailRpcError("payload is too large; post text instead")
        argv.extend(["--payload", encoded])
    elif text_file is not None:
        argv.extend(["--text-file", text_file])
    else:
        argv.extend(["--text", ""])
    return argv


def handle_relay(
    config: MailRpcConfig,
    *,
    header_label: str | None,
    query: Mapping[str, list[str]],
    body: Mapping[str, object],
) -> tuple[int, dict[str, object]]:
    identity = resolve_identity(
        config,
        header_label=header_label,
        body_label=body.get("controller_label"),
    )
    mode = relay_mode(query, body)
    project_root = resolve_project_root(body.get("project_root"), config)
    argv = build_relay_argv(
        mode=mode,
        project_root=project_root,
        identity=identity,
        body=body,
    )
    code, stdout, stderr = run_messages(
        argv,
        identity=identity,
        cwd=project_root,
        timeout_s=config.invoke_timeout_s,
    )
    return cli_payload(code, stdout, stderr)


def handle_post(
    config: MailRpcConfig,
    *,
    header_label: str | None,
    body: Mapping[str, object],
) -> tuple[int, dict[str, object]]:
    identity = resolve_identity(
        config,
        header_label=header_label,
        body_label=body.get("controller_label"),
    )
    project_root = resolve_project_root(body.get("project_root"), config)
    text = _optional_str(body, "text")
    text_file = _write_text_file(text) if text is not None else None
    try:
        argv = build_post_argv(project_root=project_root, body=body, text_file=text_file)
        code, stdout, stderr = run_messages(
            argv,
            identity=identity,
            cwd=project_root,
            timeout_s=config.invoke_timeout_s,
        )
    finally:
        if text_file is not None:
            Path(text_file).unlink(missing_ok=True)
    return cli_payload(code, stdout, stderr)


class MailRpcServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], config: MailRpcConfig) -> None:
        super().__init__(server_address, MailRpcHandler)
        self.config = config


class MailRpcHandler(BaseHTTPRequestHandler):
    server: MailRpcServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        # Request lines only. Never log headers; the bearer token is one.
        sys.stderr.write(f"{SERVICE_NAME} {self.address_string()} {fmt % args}\n")

    def do_GET(self) -> None:  # noqa: N802
        path = urllib_parse.urlparse(self.path).path
        if path != "/v1/health":
            self._send(404, {"ok": False, "error": "not_found"})
            return
        self._send(200, {"ok": True, "service": SERVICE_NAME})

    def do_POST(self) -> None:  # noqa: N802
        path = urllib_parse.urlparse(self.path).path
        if path not in {"/v1/relay", "/v1/post"}:
            self._send(404, {"ok": False, "error": "not_found"})
            return
        config = self.server.config
        try:
            self._require_auth(config)
            body = self._read_body()
            header = self.headers.get(LABEL_HEADER)
            if path == "/v1/relay":
                status, payload = handle_relay(
                    config,
                    header_label=header,
                    query=_query_map(self.path),
                    body=body,
                )
            else:
                status, payload = handle_post(config, header_label=header, body=body)
        except MailRpcError as exc:
            status = exc.status
            payload = {"ok": False, "error": exc.error}
            if exc.status != 401 and exc.detail:
                payload["detail"] = exc.detail
        except Exception as exc:
            status = 500
            payload = {
                "ok": False,
                "error": "failed",
                "detail": type(exc).__name__,
            }
        self._send(status, payload)

    def _require_auth(self, config: MailRpcConfig) -> None:
        if not token_matches(self.headers.get("Authorization"), config.token):
            raise Unauthorized("unauthorized")

    def _read_body(self) -> dict[str, object]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or raw_length.strip() == "":
            length = 0
        else:
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise MailRpcError("Content-Length must be an integer") from exc
        if length < 0:
            raise MailRpcError("Content-Length must be non-negative")
        if length > MAX_BODY_BYTES:
            raise PayloadTooLarge(f"body exceeds {MAX_BODY_BYTES} bytes")
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            decoded = raw.decode("utf-8")
            parsed = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MailRpcError("body must be a JSON object") from exc
        if not isinstance(parsed, dict):
            raise MailRpcError("body must be a JSON object")
        return parsed

    def _send(self, status: int, payload: dict[str, object]) -> None:
        raw = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except BrokenPipeError:
            return


def serve(config: MailRpcConfig) -> None:
    server = MailRpcServer((config.bind_host, config.bind_port), config)
    sys.stderr.write(
        f"{SERVICE_NAME} listening on {config.bind_host}:{config.bind_port}\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return
    finally:
        server.server_close()


def _env_first(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def client_endpoint() -> tuple[str, str]:
    """URL and token from ``GOALFLIGHT_MAIL_RPC_*`` or Grok ``MAIL_RPC_*``."""
    url = _env_first(URL_ENVS).rstrip("/")
    token = _env_first(CLIENT_TOKEN_ENVS)
    if not url or not token:
        raise MailRpcError(
            "set GOALFLIGHT_MAIL_RPC_URL (or MAIL_RPC_URL) and "
            "GOALFLIGHT_MAIL_RPC_TOKEN (or MAIL_RPC_TOKEN)"
        )
    parsed = urllib_parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise MailRpcError("mail RPC URL must be http or https")
    return url, token


class _NoRedirect(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def client_call(
    path: str,
    body: Mapping[str, object] | None = None,
    *,
    method: str = "POST",
    label: str | None = None,
    timeout_s: float = INVOKE_TIMEOUT_S,
) -> tuple[int, dict[str, object]]:
    """Call the journal-host RPC. Does not follow redirects (token stays put)."""
    url, token = client_endpoint()
    target = url + path
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib_request.Request(target, data=data, method=method)
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    sender = label if label is not None else os.environ.get("GOALFLIGHT_CONTROLLER_LABEL", "")
    sender = sender.strip()
    if sender:
        request.add_header(LABEL_HEADER, sender)
    opener = urllib_request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout_s) as response:
            status = int(response.status)
            raw = response.read()
    except urllib_error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read()
    except urllib_error.URLError as exc:
        raise MailRpcError(f"mail RPC unreachable: {exc.reason}") from exc
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MailRpcError("mail RPC returned non-JSON") from exc
    if not isinstance(parsed, dict):
        raise MailRpcError("mail RPC returned a non-object")
    return status, parsed


def _print_client_result(status: int, payload: dict[str, object]) -> int:
    print(json.dumps(payload, sort_keys=True))
    return 0 if status == 200 and payload.get("ok") is True else 1


def cmd_health() -> int:
    status, payload = client_call("/v1/health", method="GET")
    return _print_client_result(status, payload)


def cmd_relay(args: argparse.Namespace) -> int:
    body: dict[str, object] = {"drain": bool(args.drain)}
    if args.project_root:
        body["project_root"] = args.project_root
    if args.summary_only:
        body["summary_only"] = True
    if args.since:
        body["since"] = args.since
    status, payload = client_call("/v1/relay", body)
    return _print_client_result(status, payload)


def cmd_post(args: argparse.Namespace) -> int:
    body: dict[str, object] = {
        "dispatch_id": args.dispatch_id,
        "type": args.type,
    }
    if args.to_controller:
        body["to_controller"] = args.to_controller
    if args.text is not None:
        body["text"] = args.text
    if args.subject:
        body["subject"] = args.subject
    if args.project_root:
        body["project_root"] = args.project_root
    if args.controller_project_root:
        body["controller_project_root"] = args.controller_project_root
    status, payload = client_call("/v1/post", body)
    return _print_client_result(status, payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Journal-host mail RPC (serve) and Grok controller client"
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="listen (default when no subcommand is given)")
    sub.add_parser("health", help="GET /v1/health using MAIL_RPC_URL")
    relay = sub.add_parser("relay", help="POST /v1/relay (peek unless --drain)")
    relay.add_argument("--drain", action="store_true")
    relay.add_argument("--project-root", default=None)
    relay.add_argument("--summary-only", action="store_true")
    relay.add_argument("--since", default=None)
    post = sub.add_parser("post", help="POST /v1/post")
    post.add_argument("--dispatch-id", required=True)
    post.add_argument("--type", required=True)
    post.add_argument("--to-controller", default=None)
    post.add_argument("--text", default=None)
    post.add_argument("--subject", default=None)
    post.add_argument("--project-root", default=None)
    post.add_argument("--controller-project-root", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.cmd or "serve"
    try:
        if command == "serve":
            try:
                serve(load_config())
            except OSError as exc:
                print(f"mail-rpc: {exc}", file=sys.stderr)
                return 1
            return 0
        if command == "health":
            return cmd_health()
        if command == "relay":
            return cmd_relay(args)
        if command == "post":
            return cmd_post(args)
    except MailRpcError as exc:
        print(f"mail-rpc: {exc.detail}", file=sys.stderr)
        return 2
    parser.error(f"unknown command {command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
