# Agent discovery

Letee finds coding agents through JSON status files written by the `agent-status` plugin. Install the plugin in every agent you want to see. The Claude Code and Codex plugin hooks run `python3` from PATH, so every machine running those agents, local or remote, needs Python 3.10 or newer on PATH. Letee's bundled Python is private, and installing the `agent-status` command alone isn't enough.

## Experimental native Codex

Native OSC 7501 discovery is disabled by default. Opt in with this letee config:

```toml
experimental_native_codex = true
```

This requires experimental tmux on the local or remote reporting server, tested
at pinned master revision `b2a01282069146476ceca974ff255437ff00865e`, and Codex
emitting OSC 7501. Native entries work without remote Python or an agent-status
directory. The default plugin path is unchanged for other agents and servers.

Native `working`, `blocked`, `idle`, `done`, and `error` map to `working`,
`input-required`, `idle`, `completed`, and `failed`. Unknown states remain
unknown. Entries use stable pane identity and have no fabricated timestamps,
so no native working-duration estimate is available.

Warning: tmux can retain status indefinitely after exit, suspension, SIGKILL,
and replacement by an unrelated foreground process. This experiment does not
establish reporter ownership. Real Codex lifecycle validation, including shared
daemon, clean exit and editor handoff, is still outstanding.
The kill action targets the current foreground job in the pane. Check the pane
first: killing a stale native entry can terminate an unrelated replacement job.

Only panes actually reporting Codex override duplicate plugin Codex entries.
Empty fields cannot establish tmux support. Clearing the native report removes
the native entry and restores plugin fallback, even if that plugin status is
old. Unsupported servers always retain plugin fallback. No polling interval or
background service changes are involved.

## Install the plugin

### Claude Code

Claude Code needs `python3` 3.10 or newer on PATH, on Linux or macOS.

```sh
claude plugin marketplace add julsemaan/agent-status
claude plugin install agent-status@agent-status
```

Restart Claude Code.

### Codex

Codex needs `python3` 3.10 or newer on PATH, on Linux or macOS.

```sh
codex plugin marketplace add julsemaan/agent-status
codex plugin add agent-status@agent-status
```

Start Codex, open `/hooks`, trust the hooks, and send a prompt.

### Pi

```sh
pi install npm:agent-status-pi
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
