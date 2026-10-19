# Linux smoke test for orbit-applet: Xvfb + an XEmbed tray host + the mock orbitd.
# Build from desktop/:  docker build -f tests/applet/linux/Dockerfile -t orbit-applet-linux .
# trixie: PySide6 aarch64 wheels need glibc >= 2.39
FROM python:3.12-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb xauth trayer imagemagick x11-utils dbus-x11 fonts-dejavu-core \
        libgl1 libegl1 libglib2.0-0t64 libfontconfig1 libdbus-1-3 libxkbcommon0 libxkbcommon-x11-0 \
        libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
        libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 libxcb-shm0 libxcb-sync1 \
        libxcb-xfixes0 libxcb-util1 libx11-xcb1 \
    && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --extra applet --no-dev
COPY tests/applet ./tests/applet
CMD ["bash", "tests/applet/linux/smoke.sh"]
