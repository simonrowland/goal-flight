"""Compatibility shim for the goal-flight ACP SDK wrapper.

The bespoke JSON-RPC transport was removed in 0.4.5. Import this module only for
legacy names; all implementation lives in ``goalflight_acp_client``.
"""

from goalflight_acp_client import (  # noqa: F401
    ACP_IMPORT_ERROR,
    AcpConnection,
    AcpError,
    AcpLivenessActivity,
    AcpProcessPool,
    GuardedStreamReader,
    GoalflightAcpConnection,
    GoalflightClient,
    PoolExhaustedError,
    _PIDFILE_DIR,
    _live_connections,
    _ps_meta,
    _register_connection,
    _unregister_connection,
    acp_limit_from_env,
    classify_message,
    require_acp_sdk,
    spawn_acp_connection,
)
