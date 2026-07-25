PYTHON ?= python3
PEXPECT_DIR ?= /tmp/pexpect-pkg

.PHONY: test dev-install test-e2e test-e2e-docker

test:
	$(PYTHON) -m unittest discover -s tests

# E2e tests must run in Docker to avoid tampering with host tmux sessions.
test-e2e: test-e2e-docker

# Docker is mandatory. If unavailable, stop; never run tests directly on host.
test-e2e-docker:
	PYTHONPATH=$(PEXPECT_DIR) $(PYTHON) -m pytest tests/e2e/ -v --docker

dev-install:
	$(PYTHON) -m pip install -e . --break-system-packages
