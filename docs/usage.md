# Usage reference

## Start and attach to a cockpit

`letee` creates or attaches to a dedicated outer tmux server. The outer server uses the `letee@v1` socket and `letee` session by default. A named cockpit uses the `letee@v1-<name>` socket. Reopening the same name runs `attach -d`, moving that cockpit to the newest terminal.

The cockpit uses letee's bundled tmux when available. Inner local and remote sessions use the tmux installed on their host.

The outer server owns the layout only:

- the left pane runs the letee sidebar and is 40 columns wide by default
- the right pane attaches to the selected local or remote tmux session
- outer status is off
- standard outer tmux prefix bindings are disabled
- the outer prefix is `C-s` by default

Inner local and remote sessions keep their normal tmux prefix and bindings. They remain alive when you switch away from them. `C-s C-s` forwards the outer prefix to the inner session.

`C-s h` toggles between the expanded sidebar and a five-column compact strip, plus tmux's divider. The sidebar process stays alive, so selection, polling, and alerts continue. Set `sidebar_collapse_mode = "hidden"` to retain the old right-pane zoom behavior. Fresh cockpits start expanded.

The compact strip shows session numbers, including numbers beyond 9, and agents as a status icon, a space, and two display columns of their name. Working agents keep their spinner even when selected. `!` marks alerts. Click entries to switch without expanding; wheel-scroll each section to reach overflow entries. Titles, metadata, management controls, and agent ordering are omitted.

`C-s h` and the top `»` button, or `>` in ASCII mode, expand and focus the right pane. `C-s s` and `C-s a` expand and focus Sessions or Agents. Management actions expand before showing controls, menus, or prompts. Focus alone does not expand the strip, and returning to the right pane does not collapse it. Session shortcuts, alert jumps, and agent cycling preserve compact mode.

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
| `letee version` | Print the letee version. |
| `letee init` | Create missing `config.toml` and `wrapper.tmux.conf` files. |
| `letee list` | List discovered local and remote targets. |
| `letee find-work` | Open Find in an existing cockpit. |
| `letee list-servers` | List running verified letee outer servers. |
| `letee focus-sidebar [region]` | Focus or reopen the sidebar and inject a sidebar action. With no region, focus Sessions. |
| `letee switch <target>` | Switch the right pane to a target. |
| `letee switch-session <1-9>` | Switch to a numbered tracked target. |
| `letee create local <session>` | Create a local tmux session, then switch to it. |
| `letee create ssh <host> <session>` | Create a remote tmux session, then switch to it. |
| `letee kill <target>` | Kill a target tmux session and remove it from the selected server's tracked list. |
| `letee rename <target> <new-session>` | Rename a tracked target and update that target in the selected server. |
| `letee kill-server` | Kill the selected outer letee server. |

The optional `region` for `focus-sidebar` is `sessions`, `agents`, `add`, `remove`, `kill`, `alert`, `previous_agent`, `next_agent`, `previous_session`, or `next_session`.

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
| `C-s s` | Show the sidebar and select the active session in Sessions without switching. Recreate it if it was quit. |
| `C-s +` | Show the sidebar and open the Add session menu. Recreate it if it was quit. |
| `C-s a` | Show the sidebar and select the active agent in Agents without switching. Recreate it if it was quit. |
| `C-s f` | Open Find without changing the sidebar layout. |
| `C-s Left` / `C-s Right` | Jump to the previous or next agent in displayed order across tracked sessions, wrapping at both ends. |
| `C-s Up` / `C-s Down` | Switch to the previous or next tracked session in persisted manual order, wrapping at both ends. |
| `C-s r` | Remove the active right-pane session from letee without killing its tmux session. |
| `C-s x` | Kill and remove the active right-pane session after `y/N` confirmation. The right pane returns to the startup help screen. |
| `C-s j` | Jump to the first agent in the current Agents ordering with a stored bell alert or an `input-required`, `auth-required`, `failed`, or `rejected` state, including across sessions. A healthy compact or hidden sidebar stays in its current mode. If recovery recreates an invalid cockpit, the normal split layout may return. With no eligible agent, report `no agents need attention` and leave the right pane focused. |
| `C-s w` | Focus the right pane. |
| `C-s h` | Collapse or expand the sidebar and focus the right pane. Set `sidebar_collapse_mode = "hidden"` for hide/show behavior. |
| `C-s q` | Quit the outer letee session. |
| `C-s ?` | Show help in the right pane. |
| `C-s d` | Detach the outer letee session. |
| `C-s C-s` | Forward `C-s` to the inner session. |
| `C-s 1` through `C-s 9` | Switch directly to a numbered tracked target. |

Session and agent cycling preserve compact or hidden mode and do not focus the sidebar. Recovery of an invalid cockpit may restore the split layout. Without an active listed agent, Right opens the first agent and Left the last. With no agents, neither changes the current pane.

Without an active tracked session, Down opens the first tracked session and Up the last. With no tracked sessions, neither changes the current pane. Session cycling includes missing-session launchers and sessions beyond the first nine slots.

The focus commands reveal the selected row even after scrolling. Without a matching active entry, Sessions selects its first row or Add when empty, and Agents selects the ordering row. Add-menu focus behavior is unchanged.

### Sidebar controls

| Key | Action |
| --- | --- |
| `j` / `k` or arrow keys | Move the selection pointer, `›`, in the focused region. |
| `Enter` | Switch to the selected session or exact agent pane, or activate the selected Add row. |
| `e` | Rename the selected available tracked session. |
| `r` | Remove the selected target without killing it. |
| `K` / `J` | Move the selected tracked target up or down without wrapping. |
| `x` in Sessions | Kill and remove the selected tracked session after confirmation. Killing the active session returns the right pane to the startup help screen. |
| `x` in Agents | Terminate the selected agent foreground job with `SIGTERM` after confirmation. The pane and shell survive. |
| `[` / `]` | Increase or decrease the Agents share by the configured percentage points for the current run. |
| `Left` / `Right` | Cycle agent ordering, Priority or Session, when the ordering row is selected. |
| `Esc` / `Ctrl-C` | Cancel prompts and filters. |

`›` marks keyboard selection and left-pane focus. The mint reverse highlight marks the active session independently. An unfocused sidebar hides the pointer while preserving selection, viewport, colors, and the active-session highlight.

These controls stay fixed and cannot be remapped: `Enter`, `Esc`, `Ctrl-C`, arrow keys, `Backspace`, mouse events, confirmation `y/N` prompts, internal function keys `F3` through `F12`, and session slots `1` through `9`. See [configuration](https://github.com/julsemaan/letee/blob/main/docs/configuration.md) for keybinding changes.

The cockpit sends internal `F6` then `F3` for previous session and `F6` then `F4` for next session. Agent cycling uses `F7` then `F12` or `F5`. These sequences are independent of custom outer bindings.

## Find work

Press `C-s f`, click Find in the sidebar, or run `letee find-work` to open the finder popup. For a named cockpit, use `letee -L work find-work`. Find requires an existing cockpit and leaves an expanded, compact, or hidden sidebar unchanged. Canceling restores the original focus; opening a destination focuses the right pane.

The tree groups sessions, their windows, and agents. Type to filter by name or full path, including the host and parent session. Matching ignores case and matches letters in order within each space-separated term. Terms can appear in either order: `letee fix/123` and `fix/123 letee` find the same destinations. Exact and contiguous matches rank ahead of scattered letters.

| Control | Action |
| --- | --- |
| `Up` / `Down` | Select a row. |
| `Tab` | Expand or collapse the selected branch. |
| `Ctrl-T` | Cycle All, Sessions, Windows, and Agents scopes. |
| `Ctrl-F` or click the filter summary | Open the filter checklist. |
| `Enter` | Open the selected destination. |
| `Left` / `Right`, `Home` / `End` | Move the query cursor. |
| `Backspace` / `Delete` | Edit the query. |
| `Ctrl-U` | Clear the query. |
| `Esc` / `Ctrl-C` | Cancel. |

The filter summary always shows the active filters, or `none`. In the checklist, use `Up` / `Down` and `Space`, or click checkboxes, to select agent states and toggle **Has bell**. Scroll to reach more choices or **Clear filters**. Changes apply immediately. `Enter` or `Esc` returns to results without changing the query; `Ctrl-C` cancels Find.

Selected states match any of those states; no selected state means all states. Has bell matches a session bell or a stored agent alert. With both filters active, the same agent must match the state and have an alert, unless its session has a bell. Sessions and windows qualify through matching agents, but unrelated branches stay hidden. Without a state filter, a session bell includes its entire subtree. Filters combine with text search and every scope. Find remembers scope and filters per cockpit, even after cancellation.

Live updates keep the selected destination visible even if it stops matching. Changing the query, scope, or filters chooses a fresh match. Clearing filters restores normal browsing expansion.

Missing destinations are unavailable in Find and cannot be opened there. The Sessions sidebar retains its existing missing-session recovery: select a missing session and press `Enter` to recreate it.

## Tracked sessions and ordering

The Sessions view lists tracked sessions in persisted manual order. The first nine sessions receive shortcut numbers. `K` and `J` update the order without wrapping.

Tracked sessions persist in `~/.config/letee/sessions` for the default server and `~/.config/letee/servers/<name>/sessions` for named servers. Configuration, hosts, prefix, dimensions, timeout, and SSH settings stay shared across named servers. Tracked sessions, ordering, the active target, alerts, and cockpit panes stay isolated. The same inner session can be tracked by multiple servers, so overlapping servers can show duplicate alerts.

Missing sessions remain launchers. Press `Enter` on one to recreate and attach it with tmux `new-session -A`. Prefix remove/kill actions use the active right-pane target, never the sidebar selection. For these actions, missing or untracked active targets show a status message and do nothing.

## Add session menu

The Add screen searches discovered sessions and creates new ones:

- With exactly one local or SSH location available, `C-s +` opens the search directly. With multiple locations, it opens a location picker first and the search afterwards.
- Typing searches every discovered session on the chosen host's `letee.inner` server, including tracked sessions. A `＋` row appears when the typed name is free and valid.
- `Enter` on a tracked session switches to it without changing tracked-session state.
- `Enter` on an untracked session tracks it and switches to it.
- `Enter` on the `＋` row creates, tracks, and switches to a new session. `Esc` goes back one level, or closes the Add screen.

When the Add menu is closed, the independently navigable Agents region remains below the `AGENTS` divider.

## Mouse controls

- Left-button press on a session or agent row selects and switches immediately. Release and incidental movement do nothing.
- Right-click a tracked session to open the native tmux menu for rename, remove, or kill with confirmation. Right-click does not switch sessions.
- Click a tracked session's `↕` handle, or `:` in ASCII mode, hover the destination, then click the destination row to move it. The source and insertion target highlight while moving.
- Press `Esc`, click the background, or click the source handle again to cancel a move. Hovering `↑ more` or `↓ more` auto-scrolls.
- Right-click an agent to open the native tmux `Kill` menu. Confirmation sends `SIGTERM` to the foreground process group while the pane and shell survive. Right-click does not switch the session or agent pane.
- Click `＋ add`, a location row, an untracked session row, or the `＋` create row to activate the same flow as `Enter`.
- Click `‹ back`, or `< back` in ASCII mode, in the Add-session top bar to go back one level, like `Esc`.
- Wheel over Sessions or Agents scrolls the region under the pointer without keyboard focus or changing selection.
- Right-pane mouse events are forwarded by outer tmux to mouse-aware applications.
- Live border dragging is disabled, so text selection can cross the sidebar divider without resizing it.

Tmux mouse capture may require holding `Shift` for terminal-native text selection.

## ASCII mode

Set `LETEE_ASCII=1` for text-only labels, icons, spinner frames, cursors, and ellipses. Letee also selects ASCII output when the preferred terminal encoding is not UTF-8.

## Debug logging

Set `LETEE_DEBUG_LOG` to an absolute file path to enable opt-in JSONL diagnostics. Logging is off when unset. For a fresh default or named cockpit:

```sh
LETEE_DEBUG_LOG="$HOME/.local/state/letee/debug.jsonl" letee
LETEE_DEBUG_LOG="$HOME/.local/state/letee/work-debug.jsonl" letee -L work
```

An existing tmux server keeps its environment and running sidebar. Setting the variable on an attach command alone does not enable that sidebar. The simplest option is to recreate only the outer cockpit:

```sh
letee -L work kill-server
LETEE_DEBUG_LOG="$HOME/.local/state/letee/work-debug.jsonl" letee -L work
```

Omit `-L work` for the default cockpit. Inner sessions and tracked-session files survive. To keep the existing outer cockpit instead, use the same tmux executable as the cockpit, set its environment, then restart its sidebar:

```sh
socket=letee@v1-work                  # use letee@v1 for the default cockpit
log="$HOME/.local/state/letee/work-debug.jsonl"
tmux -L "$socket" set-environment -g LETEE_DEBUG_LOG "$log"
left=$(tmux -L "$socket" show-options -qv -t letee @letee_sidebar_pane)
tmux -L "$socket" respawn-pane -k -t "$left"
LETEE_DEBUG_LOG="$log" letee -L work  # reattach and install the diagnostic exit hook
```

Restarting the sidebar resets its transient UI state. Reattaching updates the native pane-death hook; restarting the sidebar alone does not. New CLI commands also need `LETEE_DEBUG_LOG` in their own environment. To disable collection, unset it in both environments, restart the sidebar and reattach without it, or recreate the outer cockpit without it.

Reproduce the problem, note the time, cockpit name, target and action, then preserve the log with the letee version and relevant timing settings. Detaching does not stop collection. Logs append across runs, so use a new path per reproduction. Include events before and after the failure, not just `error` records. Input, effects, discovery, status samples, rendering and attachments carry correlation IDs. Status results map each source's discovery request to the accepted snapshot; `sidebar_entry_draw` identifies visible entries. Error IDs link observations and exception chains. `attachment_started` means tmux accepted the respawn, not that SSH connected successfully. Mouse startup state and `mouse_recovery_candidate` events help diagnose missed clicks. An unpaired release inside the sidebar is recovered as a click, so it appears as an `activate` decision without a preceding press.

Logs contain host/session names, agent IDs, sidebar keystrokes, mouse coordinates and terminal metadata. They omit raw subprocess output, attachment shell commands, agent-status payloads, exception messages and locals. Error records retain exception types and source coordinates. Review logs before sharing; they are not anonymous. Files are owner-readable/writable, but parent-directory permissions are not tightened.

The asynchronous logger queues at most 4096 items and drops excess records rather than blocking the application. Later records report the cumulative `dropped_records` count. Flush and shutdown each wait at most one second. Capacity, timeout and sink failures share one warning per logger; records may be lost, including on abrupt termination. Logs have no rotation or file-size limit, so stop collection after reproducing. See [Development guide](../DEVELOPMENT.md#diagnostic-coverage) for event coverage and limits.
