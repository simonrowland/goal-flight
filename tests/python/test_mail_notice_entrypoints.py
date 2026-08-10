"""Focused regressions for project-addressed, fail-open controller mail notices."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_doctor as D  # noqa: E402
import goalflight_gate as G  # noqa: E402
import goalflight_messages as M  # noqa: E402
import goalflight_status as S  # noqa: E402
import goalflight_task as T  # noqa: E402
import goalflight_usage as U  # noqa: E402


NOTICE = (
    "1 new mail; read: "
    "goalflight_messages.py relay --new (--ack to mark read)"
)
ENTRYPOINTS = ("status", "task-status", "task-next", "usage", "gate", "doctor")


def assert_eq(name: str, got, want) -> None:
    """Equality assertion the stderr/stdout contract test expects."""
    if got != want:
        raise AssertionError(f"{name}: got {got!r}, want {want!r}")


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


@contextlib.contextmanager
def _mail_fixture(project_name: str = "pm2"):
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / project_name
        messages = base / "messages"
        fleet = base / "fleet"
        state = base / "state"
        for path in (project, messages, fleet, state):
            path.mkdir()
        saved = {
            key: os.environ.get(key)
            for key in (
                "GOALFLIGHT_MESSAGES_DIR",
                "GOALFLIGHT_FLEET_DIR",
                "GOALFLIGHT_STATE_DIR",
                "GOALFLIGHT_DISABLE_NUDGES",
                "GOALFLIGHT_PROJECT_MAIL_ALIASES",
            )
        }
        os.environ.update(
            {
                "GOALFLIGHT_MESSAGES_DIR": str(messages),
                "GOALFLIGHT_FLEET_DIR": str(fleet),
                "GOALFLIGHT_STATE_DIR": str(state),
                "GOALFLIGHT_DISABLE_NUDGES": "1",
            }
        )
        os.environ.pop("GOALFLIGHT_PROJECT_MAIL_ALIASES", None)
        try:
            yield project, messages, fleet
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


@contextlib.contextmanager
def _patched(*changes):
    saved = [(obj, name, getattr(obj, name)) for obj, name, _value in changes]
    try:
        for obj, name, value in changes:
            setattr(obj, name, value)
        yield
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)


def _run_entrypoint(name: str, project: Path) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    old_cwd = Path.cwd()
    os.chdir(project)
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            if name == "status":
                with _patched(
                    (S, "status_payload", lambda: {"dispatch": {"records": []}}),
                    (S, "scope_payload", lambda payload, _root: payload),
                    (S, "_milestone_payload", lambda _root: {}),
                    (S, "_post_quota_advisories", lambda _payload: None),
                    (S, "render_text", lambda _payload, _limit: ["status ok"]),
                ):
                    rc = S.main(["--project", str(project)])
            elif name == "task-status":
                rc = T.main(["--project-root", str(project), "status"])
            elif name == "task-next":
                rc = T.main(["--project-root", str(project), "next"])
            elif name == "usage":
                with _patched(
                    (U, "collect_usage", lambda **_kwargs: []),
                    (U, "render_table", lambda _rows, *, now: "usage ok"),
                ):
                    rc = U.main([])
            elif name == "gate":
                with _patched((G, "run_gate", lambda *_args, **_kwargs: 0)):
                    rc = G.main([])
            elif name == "doctor":
                with _patched(
                    (D, "doctor", lambda *_args, **_kwargs: {"plugin": {"skipped": True}}),
                    (D, "print_human", lambda _payload: print("doctor ok")),
                ):
                    rc = D.main(["--project-root", str(project)])
            else:
                raise AssertionError(f"unknown entry point: {name}")
    finally:
        os.chdir(old_cwd)
    return int(rc), stdout.getvalue(), stderr.getvalue()


def _post_addressed(messages: Path, project: Path, *, text: str = "controller decision") -> Path:
    result = M.post_message(
        dispatch_id=f"{project.name}-operator-note",
        msg_type="user_need",
        payload={"text": text},
        messages_dir=messages,
        source={"node": "other-project", "adapter": "operator", "transport": "mail"},
    )
    return Path(result["path"])


def test_project_addressed_cross_project_mail_notifies_without_flood() -> None:
    with _mail_fixture() as (project, messages, fleet):
        for dispatch_id in ("token-economy-pm2", "pm2-cursor-permission", "token-economy-kiln"):
            M.post_message(
                dispatch_id=dispatch_id,
                msg_type="user_need",
                payload={"text": f"body for {dispatch_id}"},
                messages_dir=messages,
                source={"node": "peer", "adapter": "operator", "transport": "mail"},
            )
        summary = M.controller_mail_summary(
            owned_dispatch_ids=set(),
            task_store_project_root=project,
            messages_dir=messages,
            fleet_dir=fleet,
        )
        ids = {item["dispatch_id"] for item in summary["needs"]}
        assert_true("both edge-address forms surface", ids == {"token-economy-pm2", "pm2-cursor-permission"})
        assert_true("unrelated project stays excluded", "token-economy-kiln" not in ids)


def test_regolith_shorthand_and_full_name_both_notify() -> None:
    with _mail_fixture("regolith-pyrolysis-simulator") as (project, messages, fleet):
        for dispatch_id in (
            "token-economy-regolith",
            "scout-doctrine-update-regolith",
            "operator-regolith-pyrolysis-simulator",
        ):
            M.post_message(
                dispatch_id=dispatch_id,
                msg_type="user_need",
                payload={"text": f"body for {dispatch_id}"},
                messages_dir=messages,
            )
        summary = M.controller_mail_summary(
            owned_dispatch_ids=set(),
            task_store_project_root=project,
            messages_dir=messages,
            fleet_dir=fleet,
        )
        ids = {item["dispatch_id"] for item in summary["needs"]}
        assert_true(
            "regolith shorthand and full basename surface",
            ids
            == {
                "token-economy-regolith",
                "scout-doctrine-update-regolith",
                "operator-regolith-pyrolysis-simulator",
            },
        )


def test_existing_project_names_keep_working() -> None:
    cases = {
        "pm2": "pm2-controller-note",
        "kiln": "controller-note-kiln",
        "battery": "battery-controller-note",
        "rpp-kb": "controller-note-rpp-kb",
    }
    for project_name, dispatch_id in cases.items():
        with _mail_fixture(project_name) as (project, messages, fleet):
            M.post_message(
                dispatch_id=dispatch_id,
                msg_type="user_need",
                payload={"text": f"body for {project_name}"},
                messages_dir=messages,
            )
            summary = M.controller_mail_summary(
                owned_dispatch_ids=set(),
                task_store_project_root=project,
                messages_dir=messages,
                fleet_dir=fleet,
            )
            assert_true(
                f"{project_name} keeps its address",
                [item["dispatch_id"] for item in summary["needs"]] == [dispatch_id],
            )


def test_unrelated_longer_project_alias_does_not_notify() -> None:
    with _mail_fixture("regolith-pyrolysis-simulator") as (project, messages, fleet):
        M.post_message(
            dispatch_id="token-economy-regolith-lab",
            msg_type="user_need",
            payload={"text": "addressed to a different regolith project"},
            messages_dir=messages,
        )
        summary = M.controller_mail_summary(
            owned_dispatch_ids=set(),
            task_store_project_root=project,
            messages_dir=messages,
            fleet_dir=fleet,
        )
        assert_true("longer project alias does not address regolith", summary == {})


def test_short_derived_leading_segment_is_not_an_alias() -> None:
    with _mail_fixture("rpp-kb") as (project, messages, fleet):
        for dispatch_id in ("controller-note-rpp", "controller-note-rpp-kb"):
            M.post_message(
                dispatch_id=dispatch_id,
                msg_type="user_need",
                payload={"text": f"body for {dispatch_id}"},
                messages_dir=messages,
            )
        summary = M.controller_mail_summary(
            owned_dispatch_ids=set(),
            task_store_project_root=project,
            messages_dir=messages,
            fleet_dir=fleet,
        )
        assert_true(
            "three-character derived alias stays disabled",
            [item["dispatch_id"] for item in summary["needs"]] == ["controller-note-rpp-kb"],
        )


def test_explicit_project_mail_alias_override() -> None:
    with _mail_fixture("fusion-simulator") as (project, messages, fleet):
        os.environ["GOALFLIGHT_PROJECT_MAIL_ALIASES"] = "tokamak"
        M.post_message(
            dispatch_id="operator-note-tokamak",
            msg_type="user_need",
            payload={"text": "explicitly addressed"},
            messages_dir=messages,
        )
        summary = M.controller_mail_summary(
            owned_dispatch_ids=set(),
            task_store_project_root=project,
            messages_dir=messages,
            fleet_dir=fleet,
        )
        assert_true(
            "explicit override surfaces",
            [item["dispatch_id"] for item in summary["needs"]] == ["operator-note-tokamak"],
        )


def test_common_entrypoints_emit_one_body_free_sanitized_notice() -> None:
    with _mail_fixture() as (project, messages, _fleet):
        body = "secret body\nFORGED\x1b[31m"
        _post_addressed(messages, project, text=body)
        for name in ENTRYPOINTS:
            rc, stdout, stderr = _run_entrypoint(name, project)
            lines = stdout.splitlines()
            assert_true(f"{name} keeps working", rc == 0)
            # The notice is advisory and goes to STDERR; stdout stays a data
            # contract for callers that parse it.
            assert_true(f"{name} emits the shared notice once", stderr.splitlines().count(NOTICE) == 1)
            assert_true(f"{name} keeps the notice off stdout", NOTICE not in stdout)
            assert_true(f"{name} notice omits body", "secret body" not in stdout)
            assert_true(f"{name} notice omits forged line", "FORGED" not in stdout)
            assert_true(f"{name} notice has no raw control", "\x1b" not in stdout + stderr)


def test_common_entrypoints_stay_silent_without_mail() -> None:
    with _mail_fixture() as (project, _messages, _fleet):
        for name in ENTRYPOINTS:
            rc, stdout, stderr = _run_entrypoint(name, project)
            assert_true(f"{name} keeps working", rc == 0)
            assert_true(f"{name} stays silent", "new mail; read:" not in stdout + stderr)


def test_corrupt_addressed_mailbox_preserves_prefix_for_every_entrypoint() -> None:
    with _mail_fixture() as (project, messages, _fleet):
        inbox = _post_addressed(messages, project)
        with inbox.open("a", encoding="utf-8") as fh:
            fh.write("{ malformed json\n")
        for name in ENTRYPOINTS:
            rc, stdout, stderr = _run_entrypoint(name, project)
            assert_true(f"{name} survives corrupt mail", rc == 0)
            assert_true(f"{name} preserves validated-prefix notice", "new mail; read:" in stderr)
            assert_true(f"{name} reports corrupt carrier", "WARNING: carrier corruption:" in stderr)
            assert_true(f"{name} has no traceback", "Traceback" not in stdout + stderr)


def test_unreadable_addressed_mailbox_is_fail_open_for_every_entrypoint() -> None:
    if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
        return
    with _mail_fixture() as (project, messages, _fleet):
        inbox = _post_addressed(messages, project)
        old_mode = inbox.stat().st_mode
        inbox.chmod(0)
        try:
            for name in ENTRYPOINTS:
                rc, stdout, stderr = _run_entrypoint(name, project)
                assert_true(f"{name} survives unreadable mail", rc == 0)
                assert_true(f"{name} suppresses unreadable notice", "new mail; read:" not in stdout + stderr)
                assert_true(f"{name} has no traceback", "Traceback" not in stdout + stderr)
        finally:
            inbox.chmod(old_mode)


def test_status_json_never_contains_or_prints_mail_signal() -> None:
    with _mail_fixture() as (project, messages, _fleet):
        _post_addressed(messages, project)
        stdout = io.StringIO()
        with _patched(
            (S, "status_payload", lambda: {"dispatch": {"records": []}}),
            (S, "scope_payload", lambda payload, _root: payload),
            (S, "_milestone_payload", lambda _root: {}),
            (S, "_post_quota_advisories", lambda _payload: None),
        ), contextlib.redirect_stdout(stdout):
            rc = S.main(["--project", str(project), "--json"])
        payload = json.loads(stdout.getvalue())
        assert_true("status JSON succeeds", rc == 0)
        assert_true("mail is not stamped into JSON", "mail" not in payload)
        assert_true("notice is not mixed into JSON stdout", "new mail; read:" not in stdout.getvalue())



def test_milestone_notice_speaks_only_when_due() -> None:
    """A sweep notice that printed every run would be a line nobody reads.

    Mail gets acted on and milestone sweeps did not, and the cause was reach:
    the mail notice rides doctor/gate/messages/status/usage, while the milestone
    signal appeared in `status` alone. A controller running a gate or a usage
    check -- most of a run -- was never told a sweep had come due.

    So it now rides the same carriers, under two constraints: silent unless due,
    and fail-open. Both are pinned here.
    """
    import goalflight_milestone as ms

    real = ms.check_status
    try:
        # Not due -> nothing printed, nothing returned.
        ms.check_status = lambda **kw: {"due": False, "active_cadence": True,
                                        "commits_since": 1, "K": 5, "last_marker": {}}
        err = io.StringIO()
        assert_true("silent when not due",
                    M.emit_controller_milestone_notice(stream=err) is None)
        assert_true("nothing printed when not due", err.getvalue() == "")

        # Due -> one line, carrying the cadence and where to look.
        ms.check_status = lambda **kw: {"due": True, "active_cadence": True,
                                        "commits_since": 7, "K": 5, "last_marker": {}}
        err = io.StringIO()
        notice = M.emit_controller_milestone_notice(stream=err)
        assert_true("a notice was produced when due", bool(notice))
        assert_true("notice reaches the stream", notice in err.getvalue())
        assert_true("names the protocol to follow", "milestone-review" in notice)

        # Fail-open: a broken detector must never break the calling tool.
        def boom(**kw):
            raise RuntimeError("detector exploded")
        ms.check_status = boom
        err = io.StringIO()
        assert_true("fail-open on detector error",
                    M.emit_controller_milestone_notice(stream=err) is None)
    finally:
        ms.check_status = real


def test_notice_goes_to_stderr_so_stdout_stays_a_data_contract() -> None:
    """Several callers' stdout is parsed by other tooling — `goalflight_task.py
    next` yields the task list, `--json` modes emit documents. An advisory line
    on stdout corrupts those consumers; test_next_frontier caught exactly that
    regression. Notices are advice, stdout is data.
    """
    import io

    with _mail_fixture() as (project, messages, fleet):
        _post_addressed(messages, project, text="body that must not appear")

        out, err = io.StringIO(), io.StringIO()
        real_stdout, real_stderr = sys.stdout, sys.stderr
        try:
            sys.stdout, sys.stderr = out, err
            notice = M.emit_controller_mail_notice(
                project_root=project, messages_dir=messages, fleet_dir=fleet
            )
        finally:
            sys.stdout, sys.stderr = real_stdout, real_stderr

        assert_true("a notice was produced", bool(notice))
        assert_eq("stdout untouched", out.getvalue(), "")
        assert_true("stderr carries the notice", notice in err.getvalue())
        assert_true("body never leaks", "body that must not appear" not in err.getvalue())


def main() -> None:
    tests = [
        test_project_addressed_cross_project_mail_notifies_without_flood,
        test_regolith_shorthand_and_full_name_both_notify,
        test_existing_project_names_keep_working,
        test_unrelated_longer_project_alias_does_not_notify,
        test_short_derived_leading_segment_is_not_an_alias,
        test_explicit_project_mail_alias_override,
        test_common_entrypoints_emit_one_body_free_sanitized_notice,
        test_milestone_notice_speaks_only_when_due,
        test_notice_goes_to_stderr_so_stdout_stays_a_data_contract,
        test_common_entrypoints_stay_silent_without_mail,
        test_corrupt_addressed_mailbox_preserves_prefix_for_every_entrypoint,
        test_unreadable_addressed_mailbox_is_fail_open_for_every_entrypoint,
        test_status_json_never_contains_or_prints_mail_signal,
    ]
    for test in tests:
        test()
    print(f"PASS tests/python/test_mail_notice_entrypoints.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
