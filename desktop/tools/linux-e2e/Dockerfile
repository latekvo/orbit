# Linux userland for exercising orbit-mpris against a real D-Bus session bus.
FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends dbus dbus-bin playerctl systemd procps \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.13.1
WORKDIR /work
