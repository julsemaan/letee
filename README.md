<p align="center">
  <img src="logo.png" alt="mtmux logo" width="280">
</p>

<h1 align="center">mtmux</h1>

<p align="center">
  Every tmux session, local or remote, at your fingertips.
</p>

`mtmux` runs inside your existing terminal and works on top of tmux. No new terminal app, no replacement tmux setup, no retraining your keyboard muscle memory. Keep your terminal, tmux configuration, keybindings, plugins, and workflows while adding a persistent sidebar for finding, opening, and switching between sessions across your machine and SSH hosts. Your sessions remain ordinary tmux sessions; mtmux simply puts them within reach.

Track important sessions, see at a glance which ones need attention, and jump between local and remote work without hunting through terminal tabs.

## Why mtmux?

- **Keep your terminal and tmux setup**: unlike cmux, mtmux runs inside your current terminal and builds on tmux instead of forcing a new terminal app. Your configuration, keybindings, plugins, and workflows keep working.
- **One view across machines**: local and remote sessions live in the same sidebar.
- **Fast context switches**: see which sessions need your attention via tmux bells, then jump straight to them.

## Quick start

Requires Python 3.11+, tmux, and OpenSSH. Automatic coding-agent discovery additionally requires [astatus](https://github.com/julsemaan/astatus) locally and on configured remote hosts.

```sh
pip install mtmux
mtmux
```

That opens an outer tmux workspace with the mtmux sidebar on the left and your selected session on the right. Press `Enter` on a session to step into it; press `q` to close the sidebar, then `C-s s` to reopen it on Sessions.

## How it works

`mtmux` creates or attaches to a dedicated outer tmux server. That outer layer owns only the layout:

- outer prefix: `C-s`
- focus/open Sessions: `C-s s`
- add session: `C-s +`
- focus/open Agents: `C-s a`
- hide sidebar: `C-s h`
- quit cockpit: `C-s q`
- show help: `C-s ?`
- detach cockpit: `C-s d`
- forward outer prefix to inner session: `C-s C-s`
- standard outer tmux prefix bindings: disabled
- outer status: off
- left pane: `mtmux` sidebar, 40 columns by default
- right pane: selected local/remote tmux attach client

Inner local and remote sessions keep their normal tmux prefix and bindings, and remain alive when you switch away. Only mtmux's outer `prefix` key table is restricted.

## Configuration

Files live in `~/.config/mtmux/`:

```toml
hosts = ["my-remote-machine"]
prefix = "C-s"
sidebar_width = 40
status_timeout = 5
persistent_ssh = true
```

### Prefix

`prefix` accepts one non-empty, printable tmux key token without whitespace. `sidebar_width` sets left pane width in columns. `status_timeout` controls how many seconds sidebar feedback remains visible. Both numeric settings must be positive integers. Restart sidebar by rerunning `mtmux` after changing these values.

`C-s` normally sends XOFF when terminal `IXON` flow control is enabled. Attached tmux disables flow control on outer tty, so outer prefix works without global `stty` changes. Readline, Emacs, or Vim `C-s` commands require `C-s C-s` to forward literal `C-s`; inner tty may still treat it as XOFF, in which case `C-q` resumes output.

To restore old prefix, set `prefix = "C-g"` and rerun `mtmux`.

### Remote hosts

Hosts are SSH aliases only. Keep host-specific users, ports, keys, proxies, IPv6, and other connection settings in `~/.ssh/config`.

By default, mtmux makes OpenSSH reuse one authenticated transport per host with `ControlMaster=auto`, `ControlPersist=10m`, and `ControlPath=~/.ssh/mtmux-%C`. Later discovery polls, switches, creates, and kills avoid repeating TCP setup, key exchange, and authentication. Control sockets remain for 10 minutes after last use.

Every mtmux SSH connection also uses `ServerAliveInterval=60` and `ServerAliveCountMax=3` to detect dead connections and keep idle sessions active through network and NAT timeouts. Keepalive remains enabled when `persistent_ssh` is disabled.

To omit mtmux's persistence options, set:

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
mtmux list
mtmux switch local:<session>
mtmux switch ssh:<host>:<session>
mtmux switch-session <1-9>
mtmux create local <session>
mtmux create ssh <host> <session>
mtmux kill local:<session>
mtmux kill ssh:<host>:<session>
```

Switching uses outer tmux `respawn-pane` on right pane. Real tmux sessions stay alive.

## Sidebar keys

- `C-s s`: focus Sessions; recreates sidebar if quit
- `C-s +`: focus Sessions and open Add session menu
- `C-s a`: focus Agents; recreates sidebar if quit
- `C-s h`: hide sidebar
- `C-s q`: quit outer mtmux
- `C-s ?`: show help in right pane
- `C-s d`: detach outer mtmux
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

`›` marks keyboard selection; mint reverse highlight marks active cockpit session. Both appear independently while sidebar is focused. Unfocused sidebar hides pointer and keeps active session highlighted and visible.

Normal sidebar lists sessions in persisted order. Add menu separates `New session` from `Existing session`. New-session flow skips location selection when exactly one local/SSH location is available; multiple locations use dedicated picker, then dedicated name input. Existing-session search lists only untracked sessions. Selecting or creating one persists it and switches immediately. Independently navigable Agents region remains visible below `AGENTS` divider when Add menu is closed. First nine sessions receive shortcut numbers; `K`/`J` updates order. Missing sessions remain launchers: `Enter` uses tmux `new-session -A` to recreate and attach. Sessions persist in `~/.config/mtmux/sessions`. Only tracked sessions trigger sidebar bell indicators and beeps. Set `MTMUX_ASCII=1` for text-only labels and ellipses.

## Agent discovery

Mtmux discovers coding agents by reading [astatus](https://github.com/julsemaan/astatus) JSON status files. The astatus plugin for your coding agent is **required** for detection — install it in each agent you want mtmux to discover. See the [astatus documentation](https://github.com/julsemaan/astatus) for full setup instructions and configuration.

### Getting started with coding agents

Agent discovery requires the astatus plugin for your coding agent. The plugin emits JSON status files that mtmux reads to show agent state in the sidebar.

#### Codex (OpenAI Codex CLI)

```bash
pip install agent-status
codex plugin marketplace add julsemaan/astatus
codex plugin add agent-status@astatus
```

Start Codex in any repo, open `/hooks`, and trust the agent-status hooks. First prompt starts a detached sidecar that emits 20-second heartbeats.

#### Pi (pi-coding-agent)

```bash
pi install git:github.com/julsemaan/astatus
```

Then run `/reload` or restart pi.

Run `mtmux` — agents appear automatically in the Agents sidebar (`C-s a`).

### How agent discovery works

Agent records are read from `$AGENT_STATUS_DIR`, `$XDG_STATE_HOME/agent-status`, or `~/.local/state/agent-status`, in that order. Local and remote running agents updated within 60 seconds are correlated by exact tmux socket and pane ID. Selecting agent navigates to exact server, window, and pane; active agent name and location remain orange independently of keyboard selection. Working agents show `for <duration>`; other states show no duration. Working durations prefer `task.status_timestamp` and fall back to `runtime.updated_at`; unusable optional timestamps omit duration. Each agent row starts with a semantic status icon; working agents use an animated Braille spinner. Focused selection replaces that icon with `›`, and moving focus away restores it. Status icon and text share semantic color; selection cursor stays orange. `MTMUX_ASCII=1` uses ASCII icons, spinner frames, and `>` cursor. Attention states remain bold and idle/canceled remain dim without color. Agents are discovered automatically, but only agents in tracked sessions appear. Agents cannot be added, removed, reordered, or killed as favorites.

### Agent alerts

When a tracked agent changes from `working` to `idle`, `completed`, `input-required`, `auth-required`, `failed`, `rejected`, or `canceled`, sidebar beeps once and marks that exact pane/agent with `🔔` (`BELL` in ASCII mode). Initial discovery does not alert. Marker survives later state changes until exact pane opens, is already active during discovery, disappears from discovery, or sidebar restarts.

## Mouse controls

- click session row: select and switch
- click `＋ add`, Add choice, or available location row: activate same flow as `Enter`
- wheel over sidebar: navigate selectable session and host rows
- right-pane mouse events: forwarded by outer tmux to mouse-aware applications
- live border dragging: disabled so text selection can cross the sidebar divider without resizing it

Tmux mouse capture may require holding `Shift` for terminal-native text selection.

## Clipboard

Native tmux copy mode forwards copied text through nested sessions using OSC 52. Physical terminal must support and enable OSC 52 clipboard access. mtmux declares inner clients as `clipboard` capable and sets outer server option `set-clipboard on`; inner tmux configuration remains unchanged, including explicit `set-clipboard off`.

**Security:** `set-clipboard on` permits processes in local and remote panes to set system clipboard through OSC 52. Only connect to trusted hosts and run trusted pane processes.

## Recovery

Press `C-s s` to restart/focus Sessions, `C-s a` to restart/focus Agents, or `C-s +` to open Add session menu. Or rerun:

```sh
mtmux
```

It reuses valid cockpit, repairs broken window, and respawns missing sidebar.

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

Missing cockpit for switch/create prints:

```text
No valid mtmux. Run: mtmux
```
