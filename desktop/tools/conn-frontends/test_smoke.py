from harness import media_id


async def test_pair_and_state(paired):
    w = paired
    d = await w.device()
    assert d["state"] == "connected" and d["paired"]
    a = await w.applet()
    s = await a.until(lambda s: s["cards"])
    print(s)
    print(await a.shot("smoke"))
