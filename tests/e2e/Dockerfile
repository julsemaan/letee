FROM python:3.11-slim-trixie

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends tmux; \
    version="$(tmux -V)"; \
    dpkg --compare-versions "${version#tmux }" ge 3.4; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY letee/ letee/
RUN pip install --no-cache-dir .

CMD ["sleep", "infinity"]
