PYTHON ?= uv run python
PEXPECT_DIR ?= /tmp/pexpect-pkg

.PHONY: test lint coverage fetch-tmux build-check check dev-install test-e2e test-e2e-docker bump-version

test:
	$(PYTHON) -m unittest discover -s tests -t .

lint:
	$(PYTHON) -m ruff check letee tests

coverage:
	$(PYTHON) -m coverage run -m unittest discover -s tests -t .
	$(PYTHON) -m coverage report

fetch-tmux:
	$(PYTHON) tools/fetch_tmux.py

build-check: fetch-tmux
	rm -rf build dist
	uv build
	$(PYTHON) -m twine check dist/*
	$(PYTHON) tools/check_dist.py dist

check: lint coverage build-check

# E2e tests must run in Docker to avoid tampering with host tmux sessions.
test-e2e: test-e2e-docker

# Docker is mandatory. If unavailable, stop; never run tests directly on host.
test-e2e-docker: fetch-tmux
	PYTHONPATH=$(PEXPECT_DIR) $(PYTHON) -m pytest tests/e2e/ -v --docker

dev-install: fetch-tmux
	uv sync
	uv tool install --editable . --with-executables-from agent-status

bump-version:
	uv run --no-project python tools/bump_version.py
