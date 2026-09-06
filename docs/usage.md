# Usage reference

## Start and attach to a cockpit

`letee` creates or attaches to a dedicated outer tmux server. The outer server uses the `letee` socket and session by default. A named cockpit uses the `letee-<name>` socket. Reopening the same name runs `attach -d`, moving that cockpit to the newest terminal.

The outer server owns the layout only:

- the left pane runs the letee sidebar and is 40 columns wide by default
- the right pane attaches to the selected local or remote tmux session
- outer status is off
- standard outer tmux prefix bindings are disabled
- the outer prefix is `C-s` by default

Inner local and remote sessions keep their normal tmux prefix and bindings. They remain alive when you switch away from them. `C-s C-s` forwards the outer prefix to the inner session.

`C-s h` zooms the right pane instead of killing the sidebar. The sidebar process stays alive while hidden, so selection, polling, and alerts continue. `C-s s`, `C-s a`, and `C-s +` restore the layout while focusing Sessions, focusing Agents, or opening the Add session menu.

## Inner tmux servers

Managed local and remote sessions run on a dedicated tmux server named `letee.inner` on each host. Named outer cockpits on the same host share those inner sessions. The ordinary tmux default server is outside letee's scope. Sessions there keep running but are not discovered or changed.

## Named cockpits

Use `-L <name>` to run independent outer tmux servers:

```sh
letee -L work
letee -L personal
letee list-servers
letee -L work kill-server
```

`list-servers` shows running, verified letee servers and whether each is attached. `kill-server` removes only the selected outer cockpit. Its tracked inner sessions and persisted state survive.

## CLI commands

The default prefix for these examples is `C-s`. `-L <name>` selects a named outer server and must appear before the command.

| Command | Action |
| --- | --- |
| `letee` or `letee cockpit` | Launch or attach the cockpit. |
| `letee init` | Create missing `config.toml` and `wrapper.tmux.conf` files. |
| `letee list` | List discovered local and remote targets. |
| `letee list-servers` | List running verified letee outer servers. |
| `letee focus-sidebar [region]` | Focus or reopen the sidebar and inject a sidebar action. With no region, focus Sessions. |
| `letee switch <target>` | Switch the right pane to a target. |
| `letee switch-session <1-9>` | Switch to a numbered tracked target. |
| `letee create local <session>` | Create a local tmux session, then switch to it. |
| `letee create ssh <host> <session>` | Create a remote tmux session, then switch to it. |
| `letee kill <target>` | Kill a target tmux session and remove it from the selected server's tracked list. |
| `letee rename <target> <new-session>` | Rename a tracked target and update that target in the selected server. |
| `letee kill-server` | Kill the selected outer letee server. |

The optional `region` for `focus-sidebar` is `sessions`, `agents`, `add`, `remove`, `kill`, or `alert`.

`letee sidebar` is the sidebar worker command started by the cockpit. It is not normally run by hand.

Targets use one of these forms:

```text
local:<session>
ssh:<host>:<session>
```

Hosts and session names must match `[A-Za-z0-9_.-]{1,64}`. SSH hosts must not start with `-`.

`switch` and the sidebar use outer tmux `respawn-pane` on the right pane. The real tmux sessions stay alive. `rename` requires a tracked target and updates only the selected outer server after the tmux rename succeeds. `-L <name>` applies to the cockpit, tracked-session commands, and `kill-server`; `list-servers` discovers running verified letee servers independently of the selected name.

## Sidebar keys

The keys below are the defaults. They use the default outer prefix, `C-s`.

### Outer controls

| Key | Action |
| --- | --- |
| `C-s s` | Show the sidebar and focus Sessions. Recreate it if it was quit. |
| `C-s +` | Show the sidebar and open the Add session menu. Recreate it if it was quit. |
| `C-s a` | Show the sidebar and focus Agents. Recreate it if it was quit. |
| `C-s r` | Remove the active right-pane session from letee without killing its tmux session. |
| `C-s x` | Kill and remove the active right-pane session after `y/N` confirmation. |
| `C-s !` | Show Agents and jump to the first alerted agent. If there is no alert, leave the right pane focused. |
| `C-s w` | Focus the right pane. |
| `C-s h` | Hide or show the sidebar. The sidebar remains alive while hidden. |
| `C-s q` | Quit the outer letee session. |
| `C-s ?` | Show help in the right pane. |
| `C-s d` | Detach the outer letee session. |
| `C-s C-s` | Forward `C-s` to the inner session. |
| `C-s 1` through `C-s 9` | Switch directly to a numbered tracked target. |

### Sidebar controls

| Key | Action |
| --- | --- |
| `j` / `k` or arrow keys | Move the selection pointer, `›`, in the focused region. |
| `Enter` | Switch to the selected session or exact agent pane, or activate the selected Add row. |
| `e` | Rename the selected available tracked session. |
| `r` | Remove the selected target without killing it. |
| `K` / `J` | Move the selected tracked target up or down without wrapping. |
| `x` in Sessions | Kill and remove the selected tracked session after confirmation. |
| `x` in Agents | Terminate the selected agent foreground job with `SIGTERM` after confirmation. The pane and shell survive. |
| `[` / `]` | Increase or decrease the Agents share by the configured percentage points for the current run. |
| `Left` / `Right` | Cycle agent ordering, Priority or Session, when the ordering row is selected. |
| `Esc` / `Ctrl-C` | Cancel prompts and filters. |

`›` marks keyboard selection and left-pane focus. The mint reverse highlight marks the active session independently. An unfocused sidebar hides the pointer while preserving selection, viewport, colors, and the active-session highlight.

These controls stay fixed and cannot be remapped: `Enter`, `Esc`, `Ctrl-C`, arrow keys, `Backspace`, mouse events, confirmation `y/N` prompts, internal function keys `F6` through `F11`, and session slots `1` through `9`. See [configuration](https://github.com/julsemaan/letee/blob/main/docs/configuration.md) for keybinding changes.

## Tracked sessions and ordering

The Sessions view lists tracked sessions in persisted manual order. The first nine sessions receive shortcut numbers. `K` and `J` update the order without wrapping.

Tracked sessions persist in `~/.config/letee/sessions` for the default server and `~/.config/letee/servers/<name>/sessions` for named servers. Configuration, hosts, prefix, dimensions, timeout, and SSH settings stay shared across named servers. Tracked sessions, ordering, the active target, alerts, and cockpit panes stay isolated. The same inner session can be tracked by multiple servers, so overlapping servers can show duplicate alerts.

Missing sessions remain launchers. Press `Enter` on one to recreate and attach it with tmux `new-session -A`. Prefix session actions use the active right-pane target, never the sidebar selection. Missing or untracked active targets show a status message and do nothing.

## Add session menu

The Add menu separates `New session` from `Existing session`:

- `New session` creates a fresh tmux session. With exactly one local or SSH location available, letee skips location selection. With multiple locations, it opens a location picker and then a name input.
- `Existing session` searches only untracked sessions on each host's `letee.inner` server.
- Selecting or creating a session persists it and switches to it immediately.

When the Add menu is closed, the independently navigable Agents region remains below the `AGENTS` divider.

## Mouse controls

- Left-button press on a session or agent row selects and switches immediately. Release and incidental movement do nothing.
- Right-click a tracked session to open the native tmux menu for rename, remove, or kill with confirmation. Right-click does not switch sessions.
- Click a tracked session's `↕` handle, or `:` in ASCII mode, hover the destination, then click the destination row to move it. The source and insertion target highlight while moving.
- Press `Esc`, click the background, or click the source handle again to cancel a move. Hovering `↑ more` or `↓ more` auto-scrolls.
- Right-click an agent to open the native tmux `Kill` menu. Confirmation sends `SIGTERM` to the foreground process group while the pane and shell survive. Right-click does not switch the session or agent pane.
- Click `＋ add`, an Add choice, or an available location row to activate the same flow as `Enter`.
- Click `‹ back`, or `< back` in ASCII mode, in the Add-session top bar to go back one level, like `Esc`.
- Wheel over Sessions or Agents scrolls the region under the pointer without keyboard focus or changing selection.
- Right-pane mouse events are forwarded by outer tmux to mouse-aware applications.
- Live border dragging is disabled, so text selection can cross the sidebar divider without resizing it.

Tmux mouse capture may require holding `Shift` for terminal-native text selection.

## ASCII mode

Set `LETEE_ASCII=1` for text-only labels, icons, spinner frames, cursors, and ellipses. Letee also selects ASCII output when the preferred terminal encoding is not UTF-8.

## Debug logging

Set `LETEE_DEBUG_LOG` to a file path to write opt-in JSONL diagnostics. Logging is off when the variable is unset.

```sh
letee kill-server
LETEE_DEBUG_LOG="$HOME/.local/state/letee/click-debug.jsonl" letee
```

For a named server:

```sh
letee -L work kill-server
LETEE_DEBUG_LOG="$HOME/.local/state/letee/work-click-debug.jsonl" letee -L work
```

The startup record and any ncurses mouse decode failure include the relevant tmux and terminal mouse state. A `mouse_recovery_candidate` record links a failed mouse decode to a later button release. Eligible candidates are replayed as left-button activations for sidebar rows. After reproducing the missed click, stop or detach letee and preserve the log.
