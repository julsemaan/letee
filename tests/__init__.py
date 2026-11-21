"""Isolate unit tests from the developer's running letee.

tmux resolves ``-L`` sockets under ``TMUX_TMPDIR``, so pointing it at a private
directory keeps unmocked tmux calls away from live letee servers. This runs
before any test module imports letee.
"""

import atexit
import os
import shutil
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="letee-tests-")
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)

for _name in [name for name in os.environ if name.startswith("LETEE_")] + ["TMUX", "TMUX_PANE", "XDG_STATE_HOME"]:
    os.environ.pop(_name, None)
os.environ.update(
    TMUX_TMPDIR=_SANDBOX,
    LETEE_CONFIG_DIR=os.path.join(_SANDBOX, "config"),
    AGENT_STATUS_DIR=os.path.join(_SANDBOX, "agent-status"),
)


def use_bare_inner_tmux() -> None:
    """Call from setUpModule: inner tmux resolves to bare ``tmux``, so command
    assertions carry no bundle PATH prefix and need no remote lookup."""
    import unittest
    from unittest import mock

    for name in ("letee.tmux.tmux_executable", "letee.sessions.remote_tmux", "letee.sessions.cached_remote_tmux"):
        patcher = mock.patch(name, return_value="tmux")
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


def disable_session_restore() -> None:
    """Call from setUpModule: keep resurrect calls out of command assertions
    and stop sidebars from starting a save worker."""
    import unittest
    from unittest import mock

    for name in ("letee.config.load_session_restore", "letee.sessions.load_session_restore"):
        patcher = mock.patch(name, return_value=False)
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)
