"""Simulated Orbit phone (Responder) for developing and conformance-testing the desktop side.

    phone = FakePhone(tmp_path, port=0, mdns=False, auto_accept=True)
    await phone.start()
    ...
    await phone.wait_event("paired")
    await phone.stop()

The ``orbit-fakephone`` command (``orbit.fakephone.main``) runs one from the terminal.
"""

from .phone import DEFAULT_PORT, FakePhone, Session

__all__ = ["DEFAULT_PORT", "FakePhone", "Session"]
