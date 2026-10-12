# Configuration

## Configuration files

Letee stores its configuration in `~/.config/letee/`:

- `config.toml` contains the shared configuration
- `wrapper.tmux.conf` contains the small wrapper used to start the outer tmux server

Letee creates both files on first run. To create them without launching the cockpit, run:

```sh
letee init
```

A complete configuration file with the defaults is:

```toml
hosts = []
prefix = "C-s"
sidebar_width = 40
sidebar_collapse_mode = "compact"
status_timeout = 5
agent_panel_resize_step = 5
persistent_ssh = true
tmux_config_overlay = true

[timing]
ssh_connect_timeout = 2
remote_command_timeout = 3
local_command_timeout = 2
ssh_auth_timeout = 60
ssh_master_check_timeout = 0.5
ssh_keepalive_interval = 5
ssh_keepalive_count_max = 3
retry_initial_delay = 1
retry_max_delay = 5
```

The `LETEE_CONFIG_DIR` environment variable can override `~/.config/letee` when you need a different configuration directory.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `hosts` | `[]` | List of SSH aliases to discover. |
| `prefix` | `"C-s"` | Outer tmux prefix. It must be one non-empty, printable, whitespace-free tmux key token, such as `C-s`, `C-g`, or `F1`. |
| `sidebar_width` | `40` | Expanded sidebar width in columns. It must be a positive integer. |
| `sidebar_collapse_mode` | `"compact"` | `"compact"` makes the toggle switch between expanded and a fixed five-column strip. `"hidden"` retains expanded/hidden toggling by zooming the right pane. |
| `status_timeout` | `5` | Number of seconds that sidebar feedback remains visible. It must be a positive integer. |
| `agent_panel_resize_step` | `5` | Percentage points changed by `[` and `]`. It must be an integer from 1 through 100. |
| `persistent_ssh` | `true` | Enables letee's OpenSSH connection reuse options. |
| `tmux_config_overlay` | `true` | Sources letee's tmux settings on managed local and remote tmux servers. |

`agent_panel_resize_step` affects the current sidebar process only. With the default, `[` and `]` change the Agents share from `40%` to `45%` to `50%`. Rerun `letee` after changing configuration.

Configuration, hosts, prefix, dimensions, timeout, and SSH settings are shared by named cockpits. Tracked sessions, their order, active target, alerts, and cockpit panes are isolated per outer server. See the [usage reference](https://github.com/julsemaan/letee/blob/main/docs/usage.md) for the server and persistence model.

## Timing

The optional `[timing]` table uses the defaults shown above. Omitted keys keep their defaults. Settings load once per process; restart letee, including its sidebar, after editing them.

All values are seconds except `ssh_keepalive_count_max`, which counts unanswered keepalive messages. Durations must be positive and finite. `ssh_connect_timeout`, `ssh_keepalive_interval`, and `ssh_keepalive_count_max` must be positive integers. Booleans, unknown timing keys, and `retry_initial_delay` greater than `retry_max_delay` are rejected.

`remote_command_timeout` limits automated remote commands and batch startup checks. Interactive startup authentication uses `ssh_auth_timeout` instead. `local_command_timeout` limits local tmux commands, SSH utilities, and agent probes/startup commands, including tmux and process queries in the remote kill-agent helper. `ssh_master_check_timeout` limits control-master checks. Deadlines apply to each subprocess, not the entire multi-command operation. Remote kill-agent gives the enclosing SSH process `remote_command_timeout + 2 * local_command_timeout` to cover both helper queries plus SSH overhead; each helper query still uses `local_command_timeout`. Attached sessions and interactive menus have no total runtime limit. Sidebar feedback still uses the separate `status_timeout` setting.

Failed discovery requests retry independently per host. Failed automatic restoration of the active session has separate retry state, including when overlay upload fails but discovery is healthy. Delays start after each attempt finishes and double up to `retry_max_delay`: by default, 1, 2, 4, then 5 seconds. Success resets the corresponding backoff. Explicit discovery refresh resets discovery backoff, and changing the active host can bypass its delay once. Explicit navigation bypasses restoration backoff; changing the target discards it. Create, rename, kill, and authentication operations are not automatically retried.

Healthy discovery still polls local and active hosts every 0.5 seconds and inactive hosts every 10 seconds.

## Prefix and terminal flow control

`C-s` normally sends XOFF when terminal `IXON` flow control is enabled. Attached tmux disables flow control on the outer tty, so the outer prefix works without global `stty` changes. Readline, Emacs, or Vim `C-s` commands require `C-s C-s` to forward a literal `C-s`. The inner tty may still treat it as XOFF, in which case `C-q` resumes output.

To use the old prefix instead, set:

```toml
prefix = "C-g"
```

Rerun `letee` after changing it.

## Keybinding customization

The `[keybindings]` table customizes the outer prefix layer. The `[sidebar_keybindings]` table customizes keys handled inside the sidebar. Partial tables merge with the defaults, so omitted actions keep their default values.

```toml
[keybindings]
focus_agents = "prefix+a"
focus_sessions = "prefix+s"
previous_agent = "prefix+Left"
next_agent = "prefix+Right"
add_session = "prefix++"
remove_active = "prefix+r"
kill_active = "prefix+x"
jump_alert = "prefix+j"
focus_right = "prefix+w"
toggle_sidebar = "prefix+h"
quit = "prefix+q"
help = "prefix+?"
detach = "prefix+d"

[sidebar_keybindings]
navigate_down = "j"
navigate_up = "k"
rename = "e"
remove = "r"
kill = "x"
move_up = "K"
move_down = "J"
resize_inc = "["
resize_dec = "]"
```

Outer values are tmux key tokens such as `a`, `+`, `C-a`, `M-x`, `Left`, `Right`, `M-Left`, or `F1`. Prefix a value with `prefix+` to require the outer prefix. For example, `prefix+a` means `C-s` then `a` when `prefix = "C-s"`, while `prefix+C-a` means the outer prefix followed by `C-a`. A value without `prefix+`, such as `C-a`, binds globally without `C-s` using tmux `bind-key -n`.

`previous_agent` and `next_agent` cycle through displayed agents across tracked sessions. `focus_agents` and `focus_sessions` select the active entry without switching targets, falling back to the top row when no active entry matches.

Sidebar values are single printable ASCII characters such as `j` or `[`. The following action aliases are also accepted for compatibility:

- outer: `agents`, `sessions`, `add`, `remove`, `kill`, `alert`, `right`, `toggle`, `hide`, and `hide_sidebar`
- sidebar: `down`, `up`, `reorder_up`, `reorder_down`, `increase`, `decrease`, `resize_increase`, and `resize_decrease`

Prefer the canonical action names shown in the configuration example.

### Validation rules

- Unknown keys in either table fail with an error naming the offending action.
- Two actions in the same table may not use the same binding. In the outer table, `a` and `prefix+a` are different bindings because one is global and one uses the prefix table.
- Outer and sidebar bindings may not use the reserved session slots `1` through `9`.
- An outer binding may not use the effective key that equals the configured `prefix`, such as `prefix+C-s` when `prefix = "C-s"`.
- Outer bindings must be a non-empty, printable, whitespace-free tmux key token such as `a`, `+`, `C-a`, `Left`, `Right`, or `F1`, optionally prefixed with `prefix+`.
- Sidebar bindings must be exactly one printable ASCII character.

These inputs stay fixed and cannot be remapped: `Enter`, `Esc`, `Ctrl-C`, arrow keys, `Backspace`, mouse events, confirmation `y/N` prompts, internal function keys `F5` through `F12`, and session slots `1` through `9`.

Bindings load once at startup. After editing `~/.config/letee/config.toml`, rerun `letee` to apply outer and sidebar changes. Sidebar changes also require a sidebar restart. `letee` does that automatically when it recreates the cockpit.

## Remote hosts

`hosts` contains SSH aliases only. Keep host-specific users, ports, keys, proxies, IPv6, and other connection settings in `~/.ssh/config`.

For example:

```toml
hosts = ["my-remote-machine"]
```

By default, letee makes OpenSSH reuse one authenticated transport per host with `ControlMaster=auto`, `ControlPersist=10m`, and `ControlPath=~/.ssh/letee-%C`. Later discovery polls, switches, creates, and kills avoid repeating TCP setup, key exchange, and authentication. Control sockets remain for 10 minutes after the last use.

Interactive startup blocks before opening the cockpit while it groups hosts by effective `IdentityAgent`, or by `IdentityFile` when no agent applies. It warms one host from each group sequentially, trying the next group host after a warm-up failure, then probes remaining hosts together without sharing the terminal. Failed probes retry through sequential interactive SSH checks, so enter passphrases when OpenSSH asks. Preflight disables multiplexing, so an existing control connection cannot bypass authentication. Hosts that still fail remain unavailable, and diagnostics suggest running `ssh <host>` directly. Non-TTY startup skips these SSH checks and reports why. With no configured hosts, letee prints no SSH setup output.

Letee reuses a reachable inherited `SSH_AUTH_SOCK`. When none is usable, it starts or reuses the persistent fallback agent socket `~/.ssh/letee-agent.sock`. Unlocked keys survive later letee launches while that agent stays alive. Shared SSH options include `AddKeysToAgent=yes`, so successfully used file-backed keys are cached in the agent. Hardware-backed keys and keys requiring confirmation can still prompt for each use. Grouping reduces concurrent prompts but cannot override token or agent confirmation policy.

New letee SSH connections use `ConnectTimeout=2`, `ServerAliveInterval=5`, and `ServerAliveCountMax=3` by default. The `[timing]` settings above control these values. Keepalive detects dead connections and keeps idle sessions active through network and NAT timeouts; it remains enabled when `persistent_ssh` is disabled. Existing persistent masters retain their previous keepalive settings until replaced, so restarting letee alone does not change those connections.

To omit letee's persistence options, set:

```toml
persistent_ssh = false
```

SSH configuration still applies, so this opt-out does not disable multiplexing configured there.

Host names must match:

```text
[A-Za-z0-9_.-]{1,64}
```

SSH hosts must not start with `-`.

## Tmux configuration overlay

By default, letee layers a small tmux configuration over your normal tmux configuration on its dedicated `letee.inner` server on each local and remote host:

```toml
tmux_config_overlay = true
```

Your normal system and user tmux configuration loads first, exactly as without letee. Letee never uses `tmux -f` for managed inner sessions. The overlay then sets only:

- Catppuccin Mocha colors for existing status elements, pane borders, messages, popups, menus, and copy mode
- `mouse on`
- `set-clipboard on`
- `allow-passthrough on`
- bell monitoring with audible bells and no visual bell

No keybindings, prefix, indexes, history settings, or `status-left`/`status-right` content are changed. The overlay is a plain tmux configuration file with native settings only. It uses no TPM plugins, fonts, scripts, or network access. The full effect needs tmux 3.4 or newer. Older tmux reports an error for the few newer options and applies the rest.

Tmux configuration is server-wide. Once applied to `letee.inner`, the overlay covers all sessions there, including sessions created outside letee. It does not apply to the ordinary tmux default server. For local servers the packaged file is sourced directly. For SSH hosts it is copied to `~/.config/letee/tmux-overlay.conf` on the remote machine with private permissions, then sourced on create, attach, and exact-pane jumps.

Like `set-clipboard on` generally, the overlay permits processes in local and remote panes to set the system clipboard through OSC 52. See the clipboard security warning below.

To opt out:

```toml
tmux_config_overlay = false
```

Disabling the overlay stops future application but cannot undo settings already loaded into a running tmux server. Restart that server, or source your own tmux configuration in it, to restore previous behavior.

## Moving from older letee versions

Older letee versions used the ordinary tmux default server for inner sessions. Those sessions keep running, but current letee neither discovers nor changes them. The Add session screen searches all sessions discovered on each host's `letee.inner`, including sessions already tracked by the current server. A tracked name from an older version appears as missing in Sessions; selecting it creates a new session with that name on `letee.inner` and switches to it. Overlay settings previously applied to the default server remain there until that server restarts or reloads its configuration.

## Clipboard

Native tmux copy mode forwards copied text through nested sessions using OSC 52. The physical terminal must support and enable OSC 52 clipboard access. Letee declares inner clients as `clipboard` capable and sets the outer server option `set-clipboard on`. With the config overlay enabled, letee also sources `set-clipboard on` on managed tmux servers after your configuration loads, so it overrides an explicit `set-clipboard off` there. Set `tmux_config_overlay = false` to keep your inner configuration's `set-clipboard off` effective.

> **Security:** `set-clipboard on` permits processes in local and remote panes to set the system clipboard through OSC 52. Only connect to trusted hosts and run trusted pane processes.
