# letee

## TL;DR

Letee is short for "Multiple tmux" and it allows to manage multiple local and remote tmux sessions in a single terminal window and switch between them. It features bell support in order to raise awareness that a session requires attention which is primarily used for coding agent driven development.

## Documentation

The README must be kept simple and focused on information that is aimed at end users of letee. There are additional files in docs/ that can be used for advanced details for end users.

To document information for letee developers (for example: release instructions, development environment setup, etc), use DEVELOPMENT.md 

## Development rules

- Follow Python best practices
- Introduce 3rd party dependencies if this results in better maintainability
- You can install any Python dependencies you need to accomplish your work
- Run the tests (`make test`) as part of the definition of done
- Don't run tests/e2e (`make test-e2e-docker`) as part of development as the suite is not stable yet
- E2e tests (`tests/e2e`) are Docker-only. Run them with `make test-e2e-docker`; never invoke pytest directly or run them against host tmux.
- Docker is a hard requirement for e2e work. If Docker is missing or unavailable, stop and report that requirement. Do not install host test dependencies, alter test guards, mock Docker, or devise a non-Docker workaround.

## Protecting the user's tmux servers

The developer's live letee may run on the host while you work. Its sockets are `letee@v1*` (the cockpit) and `letee.inner` (every tracked session), under `${TMUX_TMPDIR:-/tmp}/tmux-$UID/`. Killing or reconfiguring either server destroys the user's sessions and running agents.

- Never run `letee` or `python -m letee` on the host. `letee -L <name>` does not isolate you: every letee server shares `letee.inner`.
- Never run `tmux -L letee…`, `tmux kill-server`, or any tmux command without an explicit socket. Never point `TMUX_TMPDIR` at the default directory.
- Unit tests (`make test`) fake tmux and are safe. For anything that needs real tmux, start a throwaway server with an absolute socket path in a scratch directory, e.g. `tmux -S "$scratch/sock" -f /dev/null new-session -d`, and pass the same `-S` on every call. Stop it with `tmux -S "$scratch/sock" kill-server`.
- To check a UI change visually, call the drawing functions (`sidebar._draw`, `finder_ui._Popup.render`) against a fake or `pyte` screen, as the tests do. Don't launch the app.
- After any run that used real tmux, check that `ls /tmp/tmux-$UID/` still lists only the sockets that were there before you started.
