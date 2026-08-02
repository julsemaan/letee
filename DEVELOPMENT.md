# Development guide

This guide covers local development and manual integration testing for letee. See [README.md](README.md) for user-facing installation and usage instructions.

## Set up

Install letee in editable mode with its development tools:

```sh
make dev-install
```

## Verify changes

Run the checks that match your change:

```sh
make test          # unit tests
make lint          # Ruff
make coverage      # branch coverage (85% minimum)
make build-check   # build distributions and validate metadata
make check         # all quality gates
```

## End-to-end tests

WARNING: These tests are not working (yet). Only use `make test` for now.

End-to-end tests interact with tmux and must run in Docker:

```sh
make test-e2e-docker
```

Do not invoke `pytest` directly or run end-to-end tests against host tmux. Docker is required; do not replace the Docker guard with mocks or host-side workarounds.

## Manual SSH latency testing

Use `tools/ssh_latency_proxy.py` to test letee against a slow SSH connection without root access or global network shaping. Add a separate alias so normal host connections remain unaffected:

```sshconfig
Host dev-slow
    HostName dev.example.com
    User me
    ProxyCommand python3 /absolute/path/tools/ssh_latency_proxy.py --delay-ms 300 %h %p
```

Configure letee to use the delayed alias:

```toml
hosts = ["dev-slow"]
persistent_ssh = false
```

`--delay-ms` adds one-way delay, so `300ms` approximates `600ms` RTT. Set `persistent_ssh = false` so letee's OpenSSH multiplexing does not bypass the proxy after the first connection. Run a direct smoke test with:

```sh
ssh dev-slow
```
