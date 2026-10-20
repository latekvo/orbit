# Linux userland for the JVM connectivity scenarios: real network namespaces (iptables, tc netem,
# the whole 127.0.0.0/8 on lo) around orbitd + the Kotlin responder harness.
FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-17-jre-headless iptables iproute2 lsof procps \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.13.1
WORKDIR /work
