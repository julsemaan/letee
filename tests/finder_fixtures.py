"""Post-prerequisite workspace data, without a tmux server or discovery poller."""

from letee.names import PaneTarget, Target
from letee.workspace import Agent, Session, Window, Workspace


def sample_workspace() -> Workspace:
    local = Target("local", "letee")
    remote = Target("ssh", "letee", "dev")
    other = Target("local", "tools")

    def agent(target, socket, window, pane, name, state="working"):
        return Agent(PaneTarget(target, window, pane, socket), "agent-" + pane[1:], name, state, 1)

    sessions = (
        Session(local, "$1", "/local", (
            Window("@1", 0, "editor"),
            Window("@2", 1, "fix/123", (agent(local, "/local", "@2", "%3", "Claude", "input-required"),)),
        )),
        Session(remote, "$1", "/remote", (
            Window("@1", 0, "fix/123", (agent(remote, "/remote", "@1", "%1", "Codex"),)),
            Window("@2", 1, "deploy"),
        )),
        Session(other, "$2", "/local", (
            Window("@3", 0, "fix/456", (agent(other, "/local", "@3", "%4", "Claude"),)),
        )),
    )
    return Workspace(sessions, current=sessions[0].key + ("@2", "%3", "agent-3"))
