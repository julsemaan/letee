PYTHON ?= python3
PEXPECT_DIR ?= /tmp/pexpect-pkg

.PHONY: test dev-install test-e2e test-e2e-docker

test:
	$(PYTHON) -m unittest discover -s tests

# Default: run e2e tests directly on host (no isolation).
# Requires: tmux and mtmux on host, pexpect + pytest in $(PEXPECT_DIR).
test-e2e:
	PYTHONPATH=$(PEXPECT_DIR) $(PYTHON) -m pytest tests/e2e/ -v

# Run e2e tests inside Docker containers (full isolation).
# Requires: docker, pexpect + pytest in $(PEXPECT_DIR).
test-e2e-docker:
	PYTHONPATH=$(PEXPECT_DIR) $(PYTHON) -m pytest tests/e2e/ -v --docker

dev-install:
	$(PYTHON) -m pip install -e . --break-system-packages
