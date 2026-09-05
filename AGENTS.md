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
