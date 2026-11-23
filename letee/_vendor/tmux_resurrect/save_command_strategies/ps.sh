#!/usr/bin/env bash

CURRENT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

PANE_PID="$1"

exit_safely_if_empty_ppid() {
	if [ -z "$PANE_PID" ]; then
		exit 0
	fi
}

# letee: restore replays the saved string into the pane's shell, so only save
# commands whose argv can be recovered exactly.
only_child_pid() {
	ps -Ao "ppid=,pid=" | awk -v ppid="$PANE_PID" '$1 == ppid { n++; pid = $2 } END { if (n == 1) print pid }'
}

full_command() {
	local pid command
	pid="$(only_child_pid)"
	[ -n "$pid" ] || return 0
	if [ -r "/proc/${pid}/cmdline" ]; then
		# Shell-quote each argument; %q also escapes newlines and tabs.
		command="$(xargs -0r bash -c 'printf "%q " "$0" "$@"' < "/proc/${pid}/cmdline" 2>/dev/null)"
		[ -n "$command" ] && printf '%s\n' "${command% }"
	else
		# `ps args` loses argv boundaries, so keep single-word commands only.
		command="$(ps -o "args=" -p "$pid")"
		if [ -n "$command" ] && ! printf '%s' "$command" | LC_ALL=C grep -q '[^[:alnum:]_@%+=:,./-]'; then
			printf '%s\n' "$command"
		fi
	fi
}

main() {
	exit_safely_if_empty_ppid
	full_command
}
main
