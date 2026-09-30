"""Pure math for the slide rule: orbital mechanics, rocket and flight mechanics, Lambert, a safe calculator.

No kRPC imports and no body constants live here: callers pass GM, radii and masses measured from the
live game. Everything is SI. Heuristic models are labeled MODEL in their docstrings.

* :mod:`astra.physics.orbits` — vis-viva, apsis changes, Hohmann, ejection/capture, anomalies, state
  vectors, Kepler propagation, handedness-safe vector helpers.
* :mod:`astra.physics.rocket` — rocket equation, burn timing, TWR/hover, braking, drag, ascent models.
* :mod:`astra.physics.lambert` — Izzo Lambert solver and a porkchop transfer-window search.
* :mod:`astra.physics.calc` — whitelisted expression evaluator.
"""

from astra.physics import calc, lambert, orbits, rocket
from astra.physics.rocket import G0

__all__ = ["G0", "calc", "lambert", "orbits", "rocket"]
