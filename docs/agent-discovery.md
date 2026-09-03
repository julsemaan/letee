# Agent discovery

Letee finds coding agents through JSON status files written by the `agent-status` plugin. Install the plugin in every agent you want to see. `pip install letee` installs the Python package too. A remote machine that runs an agent without letee needs the package and plugin there as well.

## Install the plugin

### Claude Code

Claude Code needs Python 3.10 or newer on Linux or macOS.

```sh
claude plugin marketplace add julsemaan/agent-status
claude plugin install agent-status@agent-status
```

Restart Claude Code.

### Codex

```sh
codex plugin marketplace add julsemaan/agent-status
codex plugin add agent-status@agent-status
```

Start Codex, open `/hooks`, trust the hooks, and send a prompt.

### Pi

```sh
pi install git:github.com/julsemaan/agent-status@v0.1.26
```

Run `/reload` or restart pi.

### OpenCode

Add this to `opencode.json`, then restart OpenCode:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["agent-status-opencode"]
}
```

## How discovery works

Letee reads status files from these locations, in order:

1. `$AGENT_STATUS_DIR`
2. `$XDG_STATE_HOME/agent-status`
3. `~/.local/state/agent-status`

It accepts running agents updated within 60 seconds and matches each record to its exact tmux socket and pane ID. Only agents inside tracked sessions appear. Press `C-s a` to open the Agents view.

For agent states, ordering, alerts, exact-pane navigation, and termination, see the [agent reference](https://github.com/julsemaan/letee/blob/main/docs/agents.md).
