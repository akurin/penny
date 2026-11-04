# Penny image: Python 3.13 + uv deps + Camoufox/Firefox + Xvfb.
#
# Build on the target machine so the Camoufox download matches its arch:
#   docker build -t penny:latest -f deploy/Dockerfile .
#   docker save penny:latest | sudo k3s ctr images import -
FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    DEBIAN_FRONTEND=noninteractive

# Firefox/Camoufox runtime, Xvfb for Lenta's WebGL check, Mesa for WebGL,
# and fonts so pages render like a desktop.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb xauth ca-certificates curl \
        libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2 libx11-xcb1 libxcb1 \
        libxcomposite1 libxcursor1 libxdamage1 libxfixes3 libxi6 libxrandr2 \
        libxrender1 libxss1 libxtst6 libdrm2 libgbm1 libnss3 libnspr4 \
        libatk1.0-0 libatk-bridge2.0-0 libcups2 libpango-1.0-0 \
        libpangocairo-1.0-0 libxkbcommon0 libxshmfence1 \
        libegl1 libgl1-mesa-dri \
        fonts-liberation fonts-dejavu-core fontconfig \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

# Branch config is mounted here at runtime from a Secret; the image holds none.
RUN mkdir -p /app/config

# Dependency layer first so code edits do not reinstall the world.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

COPY . .
RUN uv sync --locked --no-dev \
    && .venv/bin/python -m camoufox fetch

ENV PATH="/app/.venv/bin:$PATH"

# Telegram long polling only; no ports.
CMD ["penny"]
