from __future__ import annotations

import os
import subprocess
import sys
import time

import pexpect
import pytest


class TmuxTestClient:
    """Drives mtmux via pexpect + subprocess.

    Direct mode (default): runs mtmux/tmux on the host.
    Docker mode (--docker): runs inside a Docker container.
    """

    def __init__(self, container_name: str | None = None):
        self.container = container_name
        self._pexpect: pexpect.spawn | None = None
        self._config_dir: str | None = None

    # -- CLI (subprocess) --

    def cli(self, *args: str, env: dict[str, str] | None = None) -> str:
        """Run mtmux CLI command, return stdout+stderr."""
        if self.container:
            cmd = ["docker", "exec"]
            if env:
                for k, v in env.items():
                    cmd += ["-e", f"{k}={v}"]
            cmd += [self.container, "mtmux"] + list(args)
        else:
            cmd = [sys.executable, "-m", "mtmux"] + list(args)
        merged_env = dict(os.environ)
        if env:
            merged_env.update(env)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=merged_env)
        return result.stdout + result.stderr

    def _tmux_cmd(self, *args: str) -> list[str]:
        """Build the tmux command list."""
        if self.container:
            return ["docker", "exec", self.container, "tmux", "-L", "mtmux"] + list(args)
        return ["tmux", "-L", "mtmux"] + list(args)

    def tmux(self, *args: str) -> str:
        """Run tmux command, return stdout (or stderr on failure)."""
        cmd = self._tmux_cmd(*args)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0 and result.stderr:
            return result.stderr.strip()
        return result.stdout.strip()

    def tmux_ok(self, *args: str) -> bool:
        """Run tmux command, return True if exit code 0."""
        cmd = self._tmux_cmd(*args)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
        return result.returncode == 0

    # -- Cockpit (pexpect) --

    def start_cockpit(
        self,
        env: dict[str, str] | None = None,
        cols: int = 90,
        rows: int = 24,
    ) -> None:
        """Spawn mtmux cockpit in a pseudo-terminal via pexpect."""
        if self._pexpect is not None:
            self.stop_cockpit()

        env_vars = dict(env or {})
        env_vars.setdefault("MTMUX_ASCII", "1")
        if "MTMUX_CONFIG_DIR" not in env_vars:
            self._config_dir = f"/tmp/mtmux-e2e-{os.urandom(4).hex()}"
            env_vars["MTMUX_CONFIG_DIR"] = self._config_dir
        else:
            self._config_dir = env_vars["MTMUX_CONFIG_DIR"]

        merged_env = dict(os.environ)
        merged_env.update(env_vars)
        merged_env["COLUMNS"] = str(cols)
        merged_env["LINES"] = str(rows)

        if self.container:
            cmd = ["docker", "exec"]
            for k, v in env_vars.items():
                cmd += ["-e", f"{k}={v}"]
            cmd += ["-e", f"COLUMNS={cols}", "-e", f"LINES={rows}"]
            cmd += ["-it", self.container, "mtmux", "cockpit"]
            spawn_cmd = cmd[0]
            spawn_args = cmd[1:]
        else:
            spawn_cmd = sys.executable
            spawn_args = ["-m", "mtmux", "cockpit"]

        self._pexpect = pexpect.spawn(
            spawn_cmd, spawn_args,
            encoding="utf-8",
            dimensions=(rows, cols),
            timeout=30,
            env=merged_env,
        )

    def send_keys(self, keys: str) -> None:
        """Send keystrokes to the cockpit pty."""
        if self._pexpect is None:
            raise RuntimeError("Cockpit not started")
        self._pexpect.send(keys)

    def send_special(self, key: str) -> None:
        """Send special key by name."""
        key_map: dict[str, str] = {
            "Enter": "\r",
            "Escape": "\x1b",
            "Up": "\x1b[A",
            "Down": "\x1b[B",
            "Left": "\x1b[D",
            "Right": "\x1b[C",
            "Backspace": "\x7f",
            "C-s": "\x13",
        }
        if key in key_map:
            self.send_keys(key_map[key])
        else:
            raise ValueError(f"Unknown special key: {key}")

    # -- Pane inspection --

    def _capture_pane(self, pane_spec: str) -> str:
        """Capture pane content. Returns empty string on error."""
        cmd = self._tmux_cmd("capture-pane", "-t", pane_spec, "-p")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
        if result.returncode != 0:
            return ""
        return result.stdout

    def sidebar_text(self) -> str:
        """capture-pane of left pane (sidebar). Returns empty string if server not ready."""
        return self._capture_pane("mtmux:cockpit.0")

    def right_pane_text(self) -> str:
        """capture-pane of right pane. Returns empty string if server not ready."""
        return self._capture_pane("mtmux:cockpit.1")

    def wait_for_sidebar_text(self, text: str, timeout: float = 5.0) -> bool:
        """Poll sidebar_text() until text appears or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pane = self.sidebar_text()
            if pane and text in pane:
                return True
            time.sleep(0.1)
        return False

    # -- Session management --

    def create_session(self, name: str) -> None:
        """tmux new-session -d -s <name>"""
        self.tmux("new-session", "-d", "-s", name)

    def kill_session(self, name: str) -> None:
        """tmux kill-session -t <name>"""
        self.tmux("kill-session", "-t", name)

    # -- Cleanup --

    def stop_cockpit(self) -> None:
        """Detach/kill the pexpect cockpit session."""
        if self._pexpect is None:
            return
        try:
            if self._pexpect.isalive():
                self._pexpect.sendcontrol("s")
                time.sleep(0.1)
                self._pexpect.send("d")
                time.sleep(0.3)
        except Exception:
            pass
        try:
            self._pexpect.close(force=True)
        except Exception:
            pass
        self._pexpect = None

        self._kill_mtmux_server()

    def _kill_mtmux_server(self) -> None:
        """Kill the mtmux tmux server."""
        subprocess.run(
            self._tmux_cmd("kill-server"),
            capture_output=True, check=False, timeout=10,
        )

    def cleanup_tmux(self) -> None:
        """Kill all tmux sessions on the mtmux socket."""
        self._kill_mtmux_server()


def pytest_addoption(parser):
    parser.addoption(
        "--docker",
        action="store_true",
        default=False,
        help="Run e2e tests inside Docker containers (default: direct on host)",
    )


@pytest.fixture(scope="module")
def container(request):
    """Build and start Docker container for the test module. Cleans up after.

    Only active when --docker flag is passed.
    """
    if not request.config.getoption("--docker"):
        yield None
        return

    name = f"mtmux-e2e-{os.urandom(4).hex()}"
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    subprocess.run(
        ["docker", "build", "-t", "mtmux-e2e", "-f", "tests/e2e/Dockerfile", "."],
        check=True, cwd=project_root,
    )
    subprocess.run(["docker", "run", "-d", "--name", name, "mtmux-e2e"], check=True)
    yield name
    subprocess.run(["docker", "rm", "-f", name], check=False)


@pytest.fixture(scope="module")
def client(container):
    """Return TmuxTestClient. Cleans up between tests."""
    c = TmuxTestClient(container)
    yield c
    c.stop_cockpit()
    c.cleanup_tmux()
