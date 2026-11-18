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
