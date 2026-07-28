PYTHON ?= python3
PEXPECT_DIR ?= /tmp/pexpect-pkg

.PHONY: test lint coverage build-check binary binary-check binary-docker check dev-install test-e2e test-e2e-docker bump-version

test:
	$(PYTHON) -m unittest discover -s tests

lint:
	$(PYTHON) -m ruff check mtmux tests

coverage:
	$(PYTHON) -m coverage run -m unittest discover -s tests
	$(PYTHON) -m coverage report

build-check:
	rm -rf build dist
	$(PYTHON) -m build
	$(PYTHON) -m twine check dist/*

binary:
	rm -rf build/pyinstaller dist/mtmux
	rm -rf build/pyinstaller dist/mtmux
	$(PYTHON) -m PyInstaller --onefile --name mtmux --distpath dist --workpath build/pyinstaller --specpath build tools/mtmux_entrypoint.py

binary-check: binary
	./dist/mtmux --help >/dev/null

binary-docker:
	docker run --rm -v "$(PWD):/io" -w /io quay.io/pypa/manylinux_2_28_x86_64:latest sh -euxc '/usr/bin/python3.12 -m ensurepip && /usr/bin/python3.12 -m pip install pyinstaller && make PYTHON=/usr/bin/python3.12 binary'
	docker run --rm -v "$(PWD)/dist/mtmux:/usr/local/bin/mtmux:ro" debian:bookworm-slim sh -euxc '! command -v python3 && mtmux --help >/dev/null'

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
