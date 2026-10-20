# Linux network namespace for LAN fault injection: orbitd / orbit-fakephone with iptables, tc netem, faketime.
FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 iptables procps tcpdump iputils-ping openjdk-17-jre-headless \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.13.1
COPY pyproject.toml uv.lock /deps/
# Dependencies only; the source is mounted read-only at /src at run time (PYTHONPATH), so edits need no rebuild.
RUN cd /deps && mkdir -p src/orbit && touch src/orbit/__init__.py README.md \
    && UV_PROJECT_ENVIRONMENT=/venv uv sync --frozen --no-group qt --no-install-project --quiet
ENV PATH=/venv/bin:$PATH PYTHONPATH=/src PYTHONUNBUFFERED=1
WORKDIR /tmp
