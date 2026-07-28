import unittest
from unittest.mock import patch

from mtmux import tmux


class HostEnvironmentTest(unittest.TestCase):
    def test_source_environment_is_unchanged(self):
        with patch.object(tmux.sys, "frozen", False, create=True), patch.dict(tmux.os.environ, {"PATH": "x", "LD_LIBRARY_PATH": "/host"}, clear=True):
            self.assertEqual(tmux.host_environment(), {"PATH": "x", "LD_LIBRARY_PATH": "/host"})

    def test_frozen_environment_restores_original_library_path(self):
        with patch.object(tmux.sys, "frozen", True, create=True), patch.dict(tmux.os.environ, {"LD_LIBRARY_PATH": "/bundle", "LD_LIBRARY_PATH_ORIG": "/host"}, clear=True):
            self.assertEqual(tmux.host_environment()["LD_LIBRARY_PATH"], "/host")

    def test_frozen_environment_removes_bundled_library_path_without_original(self):
        with patch.object(tmux.sys, "frozen", True, create=True), patch.dict(tmux.os.environ, {"LD_LIBRARY_PATH": "/bundle"}, clear=True):
            self.assertNotIn("LD_LIBRARY_PATH", tmux.host_environment())

    def test_tmux_launch_uses_host_environment(self):
        with patch.object(tmux, "host_environment", return_value={"PATH": "x"}), patch.object(tmux.subprocess, "run") as run:
            tmux.tmux("list-sessions")
        self.assertEqual(run.call_args.kwargs["env"], {"PATH": "x"})


if __name__ == "__main__":
    unittest.main()
