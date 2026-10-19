from mock_orbitd import MockOrbitd


def make_mock(sock_path, **kw):
    kw.setdefault("evolve", False)
    m = MockOrbitd(sock_path, **kw)
    m.start()
    return m
