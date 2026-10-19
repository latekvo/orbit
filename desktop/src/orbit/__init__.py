"""Orbit desktop."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("orbit-desktop")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0"
