"""One local API call, timed: ``call.py SOCKET METHOD [PARAMS_JSON]`` -> {"t0", "dt", "result"|"error"} on stdout."""

import asyncio
import json
import sys
import time

from orbit.client import ApiError, LocalClient


async def main():
    sock, method = sys.argv[1], sys.argv[2]
    params = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    c = await LocalClient.connect(sock)
    t0 = time.time()
    out = {"t0": t0}
    try:
        out["result"] = await asyncio.wait_for(c.call(method, params), 60)
    except ApiError as e:
        out["error"] = {"code": e.code, "message": e.message}
    except Exception as e:
        out["error"] = {"code": type(e).__name__, "message": str(e)}
    out["dt"] = round(time.time() - t0, 3)
    print(json.dumps(out))
    await c.close()


asyncio.run(main())
