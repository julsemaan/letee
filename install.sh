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

set -eu

UV_VERSION=0.13.9
PYTHON_VERSION=3.14

LETEE_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/letee"
BIN_DIR="${LETEE_BIN_DIR:-$HOME/.local/bin}"
COMMANDS="letee agent-status"

say() {
	printf 'letee-install: %s\n' "$*"
}

uninstall() {
	for cmd in $COMMANDS; do
		link="$BIN_DIR/$cmd"
		if [ -L "$link" ]; then
			case "$(readlink "$link")" in
			"$LETEE_HOME"/*) rm -f "$link" ;;
			esac
		fi
	done
	rm -rf "$LETEE_HOME"
	say "removed letee from $LETEE_HOME"
}

install() {
	tmp="$(mktemp -d)"
	trap 'rm -rf "$tmp"' EXIT

	say "downloading uv $UV_VERSION"
	curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" |
		env UV_UNMANAGED_INSTALL="$tmp/uv" sh >/dev/null

	export UV_TOOL_DIR="$LETEE_HOME/tools"
	export UV_PYTHON_INSTALL_DIR="$LETEE_HOME/python"
	export UV_TOOL_BIN_DIR="$BIN_DIR"
	export UV_CACHE_DIR="$tmp/cache"
	export UV_NO_CONFIG=1
	export UV_PYTHON_INSTALL_BIN=0

	say "installing letee${LETEE_VERSION:+ $LETEE_VERSION}"
	"$tmp/uv/uv" tool install --upgrade --force --managed-python \
		--python "$PYTHON_VERSION" \
		--with-executables-from agent-status \
		"letee${LETEE_VERSION:+==$LETEE_VERSION}"

	case ":$PATH:" in
	*":$BIN_DIR:"*) ;;
	*) say "add $BIN_DIR to your PATH to run letee" ;;
	esac
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
