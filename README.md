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

You need Python 3.11 or newer, tmux and OpenSSH.

## Install and launch

```sh
pip install letee
letee
```

Letee opens an outer tmux cockpit with the sidebar on the left and the selected session, or a startup pane, on the right. The default outer prefix is `C-s`. Your tmux prefix and keybindings stay the same if you have any. Use your mouse if that's what you're most comfortable with.

## Open or add a local tmux session

1. Use your mouse and click "+ add" or use the keyboard equivalent:
  1. Press `C-s +`, select `New session`, type a name, and press `Enter`.

## Configure agent discovery

Use the [local agent discovery guide](https://github.com/julsemaan/letee/blob/main/docs/agent-discovery.md) to install agent-status for Claude Code, Codex, Pi, or OpenCode. It also explains where letee reads status files and how it matches agents to tmux panes.

## Open the Agents view and jump to an agent

Start an agent inside a session, then press `C-s a`. Select the agent with `j` and `k` and press `Enter`. Letee opens the agent's exact tmux pane. Agents outside tracked sessions do not appear.

When an agent needs attention, press `C-s !` to open Agents and jump to the first alert. See the [agent integration guide](https://github.com/julsemaan/letee/blob/main/docs/agents.md) for alert states and termination behavior.

## Essential controls

Everything in letee aims to support mouse clicks, use this if you're most comfortable with a mouse.

For keyboard enthusiasts, these are the default keys.

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

### Quit or Detach

`C-s q` quits the outer letee cockpit. Inner tmux sessions keep running.

`C-s d` detaches the outer letee cockpit. Inner tmux sessions keep running and getting back into letee will be faster.

## Looking for more?

See:

- [Configuration Guide](https://github.com/julsemaan/letee/blob/main/docs/configuration.md)
- [Usage Reference](https://github.com/julsemaan/letee/blob/main/docs/usage.md)
- [Agent Integration Guide](https://github.com/julsemaan/letee/blob/main/docs/agents.md)
- [Development Guide](https://github.com/julsemaan/letee/blob/main/DEVELOPMENT.md).

## License

Licensed under the [Apache License 2.0](https://github.com/julsemaan/letee/blob/main/LICENSE).
