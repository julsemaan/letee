#!/usr/bin/env bash

CURRENT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

PANE_PID="$1"

exit_safely_if_empty_ppid() {
	if [ -z "$PANE_PID" ]; then
		exit 0
	fi
}

full_command() {
	ps -ao "ppid,args" |
		sed "s/^ *//" |
		grep "^${PANE_PID} " |
		cut -d' ' -f2-
}

# letee: restore replays this string into a shell, so drop commands with
# characters the shell could interpret.
safe_command() {
	local command
	command="$(full_command)"
	if [ -n "$command" ] && ! printf '%s' "$command" | LC_ALL=C grep -q '[^[:alnum:] _@%+=:,./-]'; then
		printf '%s\n' "$command"
	fi
}

main() {
	exit_safely_if_empty_ppid
	safe_command
}
main
