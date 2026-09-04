# Agent integrations

## Requirement

Letee discovers coding agents by reading JSON status files written by the `agent-status` plugin. The plugin is required for detection. Install it in every coding agent you want letee to discover.

`pip install letee` also installs the `agent-status` Python package used by Claude Code and Codex. For those agents on a remote machine without letee, install the package and plugin there.

For a concise setup and discovery checklist, see the local [agent discovery guide](https://github.com/julsemaan/letee/blob/main/docs/agent-discovery.md). The rest of this file documents how discovered agents are displayed and controlled.

## Set up a supported agent

### Claude Code

Claude Code requires Python 3.10 or newer on Linux or macOS.

```sh
claude plugin marketplace add julsemaan/agent-status
claude plugin install agent-status@agent-status
```

Start or restart Claude Code. The plugin uses hooks only. It does not add model tools or an MCP server. It emits 20-second heartbeats while the session runs.

### Codex

```sh
codex plugin marketplace add julsemaan/agent-status
codex plugin add agent-status@agent-status
```

Start Codex in a repository, open `/hooks`, and trust the agent-status hooks. The first prompt starts a detached sidecar that emits 20-second heartbeats.

### Pi

```sh
pi install npm:agent-status-pi
```

Run `/reload` or restart pi.

### OpenCode

Add the agent-status plugin to `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["agent-status-opencode"]
}
```

Start or restart OpenCode. OpenCode installs the plugin from npm automatically.

Run `letee`. Agents appear in the Agents sidebar after their status files are available. Press `C-s a` to open that view. Only agents in tracked sessions appear.

For the first-run workflow, see the [README](https://github.com/julsemaan/letee/blob/main/README.md). For all navigation and session controls, see the [usage reference](https://github.com/julsemaan/letee/blob/main/docs/usage.md).

## Discovery

Letee reads agent-status JSON files from these locations, in order:

1. `$AGENT_STATUS_DIR`
2. `$XDG_STATE_HOME/agent-status`
3. `~/.local/state/agent-status`

Local and remote agents are considered active when their running status was updated within 60 seconds. Letee correlates each agent with an exact tmux socket and pane ID. This keeps two agents with the same name or ID in different panes separate.

Agents are discovered automatically, but only agents in tracked sessions appear. Agents cannot be added, removed, or reordered as favorites.

## Agent rows and ordering

An agent row shows the agent name and a second line in the form `<session> · <window name>`. The host remains available for targeting but is not rendered. The active agent name and location stay orange independently of keyboard selection.

Working agents show `for <duration>`. The duration uses `task.status_timestamp` when it is usable and falls back to `runtime.updated_at`. If neither optional timestamp is usable, letee omits the duration. Other states do not show a duration.

The default Priority order groups known non-idle states before idle and unknown states. Within each group, the most recently observed state transition comes first, followed by tracked session order, numeric window and pane IDs, and agent ID. A bell uses the same recency event as the transition that produced it. Acknowledging the bell clears the marker without changing the order.

Session order ignores transition recency. It sorts by tracked session order, numeric window and pane IDs, and agent ID. Press `Left` or `Right` on the ordering row to switch between Priority and Session.

Each row starts with a semantic status icon. Working uses an animated Braille spinner. Focused selection replaces the icon with `›`, and moving focus away restores it. The icon and status text share their semantic color; the selection cursor stays orange. Attention states remain bold. Idle and canceled states remain dim without color.

| State | Unicode icon | ASCII icon |
| --- | --- | --- |
| `working` | animated Braille spinner | animated `|/-\\` spinner |
| `submitted` | `◷` | `.` |
| `idle` | `○` | `o` |
| `completed` | `✓` | `+` |
| `input-required` | `?` | `?` |
| `auth-required` | `⚿` | `@` |
| `failed` | `✕` | `x` |
| `rejected` | `⊘` | `!` |
| `canceled` | `−` | `-` |
| `unknown` | `?` | `?` |

Set `LETEE_ASCII=1` to use ASCII icons, spinner frames, the `>` selection cursor, and `BELL` alert markers. Letee also selects ASCII output when the preferred terminal encoding is not UTF-8.

## Exact-pane navigation

Select an agent and press `Enter` to switch to its exact tmux server, window, and pane. Letee uses the recorded tmux socket and pane ID, so it does not rely on the selected session's current window.

`C-s !` opens Agents and jumps to the first alerted agent using the current Agents ordering. It clears only that agent's alert after a successful exact-pane switch. If there is no alert, letee reports `no agent alerts` in Agents and leaves the right pane focused.

## Terminating an agent

With Agents focused, press `x` on the selected agent. Confirm the action. Letee sends `SIGTERM` to the foreground process group in the exact pane and leaves the pane and its interactive shell alive. The same action is available from the agent's native tmux `Kill` menu with a right-click.

Idle panes are protected. If the foreground process group is the pane shell's group, letee refuses the action instead of terminating the shell. The same exact-pane and shell protection rules apply to local and remote agents.

## Agent alerts

When a tracked agent changes from `working` to one of these states, letee beeps once and marks that exact pane and agent:

- `idle`
- `completed`
- `input-required`
- `auth-required`
- `failed`
- `rejected`
- `canceled`

The marker is `🔔` in normal mode and `BELL` in ASCII mode. Initial discovery does not create an alert.

The marker survives later state changes until one of these happens:

- the exact pane opens
- the pane is already active during discovery
- the agent disappears from discovery
- the sidebar restarts

Alerts are tied to the exact pane and agent ID. Two panes with the same agent ID remain separate.
