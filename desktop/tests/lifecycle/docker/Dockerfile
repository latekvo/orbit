# Linux userland for mDNS / network-fault lifecycle scenarios (build context: desktop/).
FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 iptables procps \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /orbit
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . 
COPY tests/lifecycle/wrap.py /orbit/wrap.py
