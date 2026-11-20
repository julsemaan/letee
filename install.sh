#!/bin/sh
# Install letee without pip or a system Python.
#
#   curl -LsSf https://raw.githubusercontent.com/julsemaan/letee/main/install.sh | sh
#   curl -LsSf https://raw.githubusercontent.com/julsemaan/letee/main/install.sh | sh -s -- --uninstall
#
# A pinned uv is downloaded to a temp dir, used once to install letee, a
# uv-managed Python and agent-status into LETEE_HOME, then deleted.
#
# Environment:
#   LETEE_VERSION  install this letee version instead of the latest
#   LETEE_BIN_DIR  where to link letee and agent-status (default ~/.local/bin)
#   LETEE_NO_MODIFY_PATH=1  don't add LETEE_BIN_DIR to PATH in your shell config

set -eu

UV_VERSION=0.13.9
PYTHON_VERSION=3.14

LETEE_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/letee"
BIN_DIR="${LETEE_BIN_DIR:-$HOME/.local/bin}"
COMMANDS="letee agent-status"

say() {
	printf 'letee-install: %s\n' "$*"
}

# True if $1 is a link this script created.
owned() {
	[ -L "$1" ] || return 1
	case "$(readlink "$1")" in
	"$LETEE_HOME"/*) return 0 ;;
	esac
	return 1
}

uninstall() {
	for cmd in $COMMANDS; do
		link="$BIN_DIR/$cmd"
		if owned "$link"; then
			rm -f "$link"
		fi
	done
	rm -rf "$LETEE_HOME"
	say "removed letee from $LETEE_HOME"
}

install() {
	# uv replaces whatever sits at the paths in its receipt on re-runs, so
	# refuse to touch commands another tool put there.
	conflict=
	for cmd in $COMMANDS; do
		link="$BIN_DIR/$cmd"
		if { [ -e "$link" ] || [ -L "$link" ]; } && ! owned "$link"; then
			say "$link was not installed by this script" >&2
			conflict=1
		fi
	done
	if [ -n "$conflict" ]; then
		say "uninstall the existing letee first, then re-run" >&2
		exit 1
	fi

	tmp="$(mktemp -d)"
	trap 'rm -rf "$tmp"' EXIT

	say "downloading uv $UV_VERSION"
	curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" |
		env UV_UNMANAGED_INSTALL="$tmp/uv" sh >/dev/null

	export UV_TOOL_DIR="$LETEE_HOME/tools"
	export UV_PYTHON_INSTALL_DIR="$LETEE_HOME/python"
	export UV_TOOL_BIN_DIR="$BIN_DIR"
	export UV_CACHE_DIR="$tmp/cache"
	# The cache is deleted on exit, so installed files must not link into it.
	export UV_LINK_MODE=copy
	export UV_NO_CONFIG=1
	export UV_PYTHON_INSTALL_BIN=0

	say "installing letee${LETEE_VERSION:+ $LETEE_VERSION}"
	"$tmp/uv/uv" tool install --upgrade --managed-python \
		--python "$PYTHON_VERSION" \
		--with-executables-from agent-status \
		"letee${LETEE_VERSION:+==$LETEE_VERSION}"

	case ":$PATH:" in
	*":$BIN_DIR:"*) return ;;
	esac
	# uv exits non-zero when it can't detect the shell or the config already
	# has the line, which must not abort the install.
	if [ -z "${LETEE_NO_MODIFY_PATH:-}" ]; then
		"$tmp/uv/uv" tool update-shell || true
	fi
	say "to use letee in this shell, run: export PATH=\"$BIN_DIR:\$PATH\""
}

main() {
	case "${1:-}" in
	"") install ;;
	--uninstall) uninstall ;;
	*)
		say "unknown argument: $1" >&2
		exit 2
		;;
	esac
}

main "$@"
