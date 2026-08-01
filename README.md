<p align="center">
  <img src="logo.png" alt="letee logo" width="280">
</p>

<h1 align="center">letee</h1>

<p align="center">
  Letee, pronounced luh-tee.<br>
  Every tmux session. Every coding agent. Local or remote. In one place.
</p>

`letee` is a tmux cockpit that allows to run many sessions and coding agents. It adds a persistent sidebar to your existing terminal, bringing local and SSH work into one unified focused view.

Your sessions remain ordinary tmux sessions. Keep your terminal, tmux configuration, keybindings, plugins, and workflows. `letee` only makes them easier to see and reach.

## Why letee?

- **Keep your setup**: letee builds on tmux instead of replacing it. No new terminal app. No replacement tmux setup. No retraining your muscle memory.
- **See everything in one place**: local and remote sessions and their agents share one sidebar.
- **Know what needs you**: live agent states, agent alerts, and tmux bells surface work that needs attention.
- **Jump straight to the work**: select a session or go directly to an agent's exact tmux window and pane.
- **Manage sessions without breaking focus**: find, create, track, reorder, remove, kill, or recreate sessions with the keyboard or mouse.

## Quick start

Requires Python 3.11+, tmux, and OpenSSH. Automatic discovery for Claude Code, Codex, Pi, and OpenCode is optional and requires [agent-status](https://github.com/julsemaan/agent-status) wherever those agents run.

```sh
pip install letee
letee
```

That opens an outer tmux workspace with the sidebar on the left and your selected session on the right. Press `C-s s` for Sessions, `C-s a` for Agents, or `C-s +` to add a session. Press `Enter` to open the selected session or jump to the selected agent's exact pane.

## How it works

`letee` creates or attaches to a dedicated outer tmux server. That outer layer owns only the layout:

- outer prefix: `C-s`
- focus/open Sessions: `C-s s`
- add session: `C-s +`
- focus/open Agents: `C-s a`
- focus active session: `C-s w`
- hide sidebar: `C-s h`
- quit cockpit: `C-s q`
- show help: `C-s ?`
- detach letee: `C-s d`
- forward outer prefix to inner session: `C-s C-s`
- standard outer tmux prefix bindings: disabled
- outer status: off
- left pane: `letee` sidebar, 40 columns by default
- right pane: selected local/remote tmux attach client

Inner local and remote sessions keep their normal tmux prefix and bindings, and remain alive when you switch away. Only letee's outer `prefix` key table is restricted.

## Configuration

Files live in `~/.config/letee/`:

```toml
hosts = ["my-remote-machine"]
prefix = "C-s"
sidebar_width = 40
status_timeout = 5
persistent_ssh = true
```

### Prefix

`prefix` accepts one non-empty, printable tmux key token without whitespace. `sidebar_width` sets left pane width in columns. `status_timeout` controls how many seconds sidebar feedback remains visible. Both numeric settings must be positive integers. Restart sidebar by rerunning `letee` after changing these values.

`C-s` normally sends XOFF when terminal `IXON` flow control is enabled. Attached tmux disables flow control on outer tty, so outer prefix works without global `stty` changes. Readline, Emacs, or Vim `C-s` commands require `C-s C-s` to forward literal `C-s`; inner tty may still treat it as XOFF, in which case `C-q` resumes output.

To restore old prefix, set `prefix = "C-g"` and rerun `letee`.

### Remote hosts

Hosts are SSH aliases only. Keep host-specific users, ports, keys, proxies, IPv6, and other connection settings in `~/.ssh/config`.

By default, letee makes OpenSSH reuse one authenticated transport per host with `ControlMaster=auto`, `ControlPersist=10m`, and `ControlPath=~/.ssh/letee-%C`. Later discovery polls, switches, creates, and kills avoid repeating TCP setup, key exchange, and authentication. Control sockets remain for 10 minutes after last use.

Every letee SSH connection also uses `ServerAliveInterval=60` and `ServerAliveCountMax=3` to detect dead connections and keep idle sessions active through network and NAT timeouts. Keepalive remains enabled when `persistent_ssh` is disabled.

To omit letee's persistence options, set:

```toml
persistent_ssh = false
```

SSH config still applies, so this opt-out does not disable multiplexing configured there.

Names of the hosts must match:

```text
[A-Za-z0-9_.-]{1,64}
```

## CLI commands

```sh
letee list
letee switch local:<session>
letee switch ssh:<host>:<session>
letee switch-session <1-9>
letee create local <session>
letee create ssh <host> <session>
letee kill local:<session>
letee kill ssh:<host>:<session>
```

Switching uses outer tmux `respawn-pane` on right pane. Real tmux sessions stay alive.

## Sidebar keys

- `C-s s`: focus Sessions; recreates sidebar if quit
- `C-s +`: focus Sessions and open Add session menu
- `C-s a`: focus Agents; recreates sidebar if quit
- `C-s w`: focus right pane
- `C-s h`: hide sidebar
- `C-s q`: quit outer letee
- `C-s ?`: show help in right pane
- `C-s d`: detach outer letee
- `C-s C-s`: forward `C-s` to inner session
- `C-s 1`–`C-s 9`: switch directly to numbered tracked target
- `j` / `k` or arrows: move selection pointer (`›`) in focused region
- `[` / `]`: give Agents/Sessions region more rows for current run
- `h` / `l`: cycle agent ordering mode (Priority / Session) when ordering row is selected
- `Enter`: switch selected session or exact agent pane, or activate selected Add row
- `a`: open Add session menu from Sessions or Agents
- `r`: remove selected target without killing it
- `K` / `J`: move selected tracked target up/down without wrapping
- `x`: kill and remove selected tracked session (asks first)
- `/`: open existing-session search directly
- `?`: open help in right pane
- `q`: quit sidebar only

`›` marks keyboard selection and left-pane focus; mint reverse highlight marks active session independently. Unfocused sidebar hides pointer, leaves sidebar colors unchanged, and keeps active session highlighted and visible.

Normal sidebar lists sessions in persisted order. Add menu separates `New session` from `Existing session`. New-session flow skips location selection when exactly one local/SSH location is available; multiple locations use dedicated picker, then dedicated name input. Existing-session search lists only untracked sessions. Selecting or creating one persists it and switches immediately. Independently navigable Agents region remains visible below `AGENTS` divider when Add menu is closed. First nine sessions receive shortcut numbers; `K`/`J` updates order. Missing sessions remain launchers: `Enter` uses tmux `new-session -A` to recreate and attach. Sessions persist in `~/.config/letee/sessions`. Only tracked sessions trigger sidebar bell indicators and beeps. Set `LETEE_ASCII=1` for text-only labels and ellipses.

## Agent discovery

Letee discovers coding agents by reading [agent-status](https://github.com/julsemaan/agent-status) JSON status files. The agent-status plugin for your coding agent is **required** for detection — install it in each agent you want letee to discover. See the [agent-status documentation](https://github.com/julsemaan/agent-status) for full setup instructions and configuration.

### Getting started with coding agents

Agent discovery requires the agent-status plugin for your coding agent. The plugin emits JSON status files that letee reads to show agent state in the sidebar.

#### Claude Code

Requires Python 3.10+ and Linux or macOS.

```bash
pip install agent-status
claude plugin marketplace add julsemaan/agent-status
claude plugin install agent-status@agent-status
```

Start or restart Claude Code. The plugin uses hooks only—no model tools or MCP server—and emits 20-second heartbeats while the session runs.

#### Codex (OpenAI Codex CLI)

```bash
pip install agent-status
codex plugin marketplace add julsemaan/agent-status
codex plugin add agent-status@agent-status
```

Start Codex in any repo, open `/hooks`, and trust the agent-status hooks. First prompt starts a detached sidecar that emits 20-second heartbeats.

#### Pi (pi-coding-agent)

```bash
pi install git:github.com/julsemaan/agent-status@v0.1.26
```

Then run `/reload` or restart pi.

#### OpenCode

Add the agent-status plugin to `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["agent-status-opencode"]
}
```

Then start or restart OpenCode. OpenCode installs the plugin from npm automatically.

Run `letee` — agents appear automatically in the Agents sidebar (`C-s a`).

### How agent discovery works

Agent records are read from `$AGENT_STATUS_DIR`, `$XDG_STATE_HOME/agent-status`, or `~/.local/state/agent-status`, in that order. Local and remote running agents updated within 60 seconds are correlated by exact tmux socket and pane ID. Selecting agent navigates to exact server, window, and pane; active agent name and location remain orange independently of keyboard selection. Working agents show `for <duration>`; other states show no duration. Working durations prefer `task.status_timestamp` and fall back to `runtime.updated_at`; unusable optional timestamps omit duration. Each agent row starts with a semantic status icon; working agents use an animated Braille spinner. Focused selection replaces that icon with `›`, and moving focus away restores it. Status icon and text share semantic color; selection cursor stays orange. `LETEE_ASCII=1` uses ASCII icons, spinner frames, and `>` cursor. Attention states remain bold and idle/canceled remain dim without color. Agent second lines show `<session> · <window name>`; host stays available for targeting but is not rendered. Agents are discovered automatically, but only agents in tracked sessions appear. Agents cannot be added, removed, reordered, or killed as favorites.

### Agent alerts

When a tracked agent changes from `working` to `idle`, `completed`, `input-required`, `auth-required`, `failed`, `rejected`, or `canceled`, sidebar beeps once and marks that exact pane/agent with `🔔` (`BELL` in ASCII mode). Initial discovery does not alert. Marker survives later state changes until exact pane opens, is already active during discovery, disappears from discovery, or sidebar restarts.

## Mouse controls

- click session row: select and switch
- right-click tracked session: open native tmux menu to remove it or kill it with confirmation; right-click does not switch sessions
- drag tracked session: reorder it; hovering `↑ more` or `↓ more` auto-scrolls, and leaving sidebar drops at current insertion line
- click `＋ add`, Add choice, or available location row: activate same flow as `Enter`
- wheel over sidebar: navigate selectable session and host rows
- right-pane mouse events: forwarded by outer tmux to mouse-aware applications
- live border dragging: disabled so text selection can cross the sidebar divider without resizing it

Tmux mouse capture may require holding `Shift` for terminal-native text selection.

## Clipboard

Native tmux copy mode forwards copied text through nested sessions using OSC 52. Physical terminal must support and enable OSC 52 clipboard access. letee declares inner clients as `clipboard` capable and sets outer server option `set-clipboard on`; inner tmux configuration remains unchanged, including explicit `set-clipboard off`.

**Security:** `set-clipboard on` permits processes in local and remote panes to set system clipboard through OSC 52. Only connect to trusted hosts and run trusted pane processes.

## Development

```sh
make dev-install
make test          # unit tests
make lint          # Ruff
make coverage      # branch coverage (85% minimum)
make build-check   # build distributions and validate metadata
make check         # all quality gates
```

## License

Licensed under the [Apache License 2.0](LICENSE).

