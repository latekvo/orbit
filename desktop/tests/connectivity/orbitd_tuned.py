"""orbitd with Daemon keyword overrides: ``python orbitd_tuned.py '<json kwargs>' <orbitd args>``.

Same as the ``orbitd`` entry point (orbit.daemon.main) except for the timing knobs, e.g. to act as
a peer on the older 20 s / 60 s keepalive.
"""

import json
import sys

from orbit.daemon import core, main

overrides = json.loads(sys.argv.pop(1))
_Daemon = core.Daemon


class Tuned(_Daemon):
    def __init__(self, *args, **kwargs):
        kwargs.update(overrides)
        super().__init__(*args, **kwargs)


main.Daemon = Tuned
main.main()
