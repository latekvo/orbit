# Linux network namespace for the connectivity rig: real orbitd + real Kotlin ResponderSession,
# with iptables / tc netem available for fault injection (run with --cap-add NET_ADMIN).
FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-17-jre-headless iproute2 iptables procps lsof socat \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.13.1
WORKDIR /work
