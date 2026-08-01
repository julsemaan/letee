PYTHON ?= python3
PEXPECT_DIR ?= /tmp/pexpect-pkg

.PHONY: test lint coverage build-check check dev-install test-e2e test-e2e-docker bump-version do-release

test:
	$(PYTHON) -m unittest discover -s tests

lint:
	$(PYTHON) -m ruff check letee tests

coverage:
	$(PYTHON) -m coverage run -m unittest discover -s tests
	$(PYTHON) -m coverage report

build-check:
	rm -rf build dist
	$(PYTHON) -m build
	$(PYTHON) -m twine check dist/*

check: lint coverage build-check

# E2e tests must run in Docker to avoid tampering with host tmux sessions.
test-e2e: test-e2e-docker

# Docker is mandatory. If unavailable, stop; never run tests directly on host.
test-e2e-docker:
	PYTHONPATH=$(PEXPECT_DIR) $(PYTHON) -m pytest tests/e2e/ -v --docker

dev-install:
	$(PYTHON) -m pip install -e ".[dev]" --break-system-packages

bump-version:
	$(PYTHON) tools/bump_version.py

do-release:
	@$(MAKE) bump-version $(if $(VERSION),VERSION="$(VERSION)") && \
	VERSION=$$($(PYTHON) -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])') && \
	git commit -m "Release version $$VERSION" -- pyproject.toml letee/__init__.py && \
	git tag -a "v$$VERSION" -m "Release version $$VERSION" && \
	git push --atomic origin HEAD "v$$VERSION"
