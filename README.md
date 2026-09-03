<p align="center">
  <img src="https://raw.githubusercontent.com/julsemaan/letee/main/logo.png" alt="letee logo" width="280">
</p>

<h1 align="center">letee</h1>

<p align="center">
  Letee, pronounced luh-tee, is a tmux cockpit for local and remote tmux sessions and coding agents.
</p>

It adds a persistent sidebar to an existing terminal. Your sessions remain ordinary tmux sessions, with your terminal, tmux configuration, keybindings, plugins, and workflows unchanged.

## Why letee?

- **Keep your setup.** Letee builds on tmux instead of replacing it. There is no new terminal app or replacement tmux setup.
- **See local and remote work together.** Sessions and their agents share one sidebar.
- **Find work that needs attention.** Agent states, alerts, and tmux bells point to sessions that need you.
- **Jump to the right pane.** Select a session or go directly to an agent's exact tmux window and pane.

## Requirements

You need Python 3.11 or newer, tmux, OpenSSH, and a terminal at least 90 columns wide.

Agent discovery is optional. To discover Claude Code, Codex, Pi, or OpenCode, install the [agent-status](https://github.com/julsemaan/agent-status) plugin wherever those agents run. `pip install letee` installs the `agent-status` Python package. A remote machine without letee still needs the plugin and package when it runs an agent.

## Install and launch

```sh
pip install letee
letee
```

Letee opens an outer tmux cockpit with the sidebar on the left and the selected session, or a startup pane, on the right. The default outer prefix is `C-s`.

## Open or add a local tmux session

1. Press `C-s s` to focus Sessions.
2. Select an existing local session with `j` and `k`, then press `Enter`.
3. To create one, press `C-s +`, select `New session`, type a name, and press `Enter`.

The new session is tracked and opens immediately. If it is missing later, select it and press `Enter` to recreate it.

## Configure agent-status

Install the plugin for each agent you want to find. These are the short setup commands.

### Claude Code

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

See the [agent integration guide](https://github.com/julsemaan/letee/blob/main/docs/agents.md) for setup details, status files, and agent behavior.

## Open the Agents view and jump to an agent

Start an agent inside a tracked session, then press `C-s a`. Select the agent with `j` and `k` and press `Enter`. Letee opens the agent's exact tmux pane. Agents outside tracked sessions do not appear.

When an agent needs attention, press `C-s !` to open Agents and jump to the first alert. See the [agent integration guide](https://github.com/julsemaan/letee/blob/main/docs/agents.md) for alert states and termination behavior.

## Essential controls

These are the default keys.

### Sessions

`C-s s` focuses Sessions. Use `j` and `k` to move, and `C-s 1` through `C-s 9` to open the first nine tracked sessions.

### Agents

`C-s a` focuses Agents. Use `j` and `k` to move, then press `Enter` to open the selected agent's pane.

### Add session

`C-s +` opens the Add session menu. Choose `New session` to create a tmux session or `Existing session` to track an untracked one.

### Open selection

`Enter` opens the selected session or agent pane.

### Help

`C-s ?` opens the built-in help.

### Quit

`C-s q` quits the outer letee cockpit. Inner tmux sessions keep running.

## The outer tmux model

Letee creates or attaches to a dedicated outer tmux server that owns the sidebar layout. Bare `letee` uses the default server. `letee -L work` uses the named `letee-work` server. The outer layer shows the sidebar on the left and attaches the selected local or remote tmux session on the right. Inner sessions keep their normal tmux prefix and bindings and remain alive when you switch away.

See the [configuration guide](https://github.com/julsemaan/letee/blob/main/docs/configuration.md), [usage reference](https://github.com/julsemaan/letee/blob/main/docs/usage.md), [agent integration guide](https://github.com/julsemaan/letee/blob/main/docs/agents.md), and [development guide](https://github.com/julsemaan/letee/blob/main/DEVELOPMENT.md).

## License

Licensed under the [Apache License 2.0](https://github.com/julsemaan/letee/blob/main/LICENSE).
