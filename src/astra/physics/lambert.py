"""Lambert's problem (Izzo 2015) and a transfer-window search over caller-supplied ephemerides.

The solver is a port of Izzo's algorithm (as in lamberthub/poliastro), including the Battin
hypergeometric series near the parabolic limit and multi-revolution existence checks.

Branch selection: the old solver decided "prograde" from the z-component of ``r1 x r2``, which is
wrong in kRPC's y-up, left-handed frames (it silently returned retrograde transfers in about half of
all geometries). Here the caller passes ``normal``: the angular-momentum axis the transfer must
share, computed in the SAME frame (normally the departure body's ``cross(r, v)``). Because the
normal is built with the same cross-product formula as the transfer's own, the test is independent
of frame handedness and axis order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from astra.physics.orbits import StateFn, Vec3, vadd, vcross, vdot, vnorm, vscale, vsub, vunit

# ---------------------------------------------------------------------------------------------
# Izzo's non-dimensional time-of-flight machinery


def _y(x: float, ll: float) -> float:
    return math.sqrt(1.0 - ll * ll * (1.0 - x * x))


def _hyp2f1b(x: float) -> float:
    """Gauss hypergeometric 2F1(3, 1; 5/2; x) by its series (Battin)."""
    if x >= 1.0:
        return math.inf
    res, term, i = 1.0, 1.0, 0
    while True:
        term *= (3.0 + i) * (1.0 + i) / (2.5 + i) * x / (i + 1.0)
        prev = res
        res += term
        if res == prev or i > 1000:
            return res
        i += 1


def _tof(x: float, ll: float, revs: int) -> float:
    """Non-dimensional time of flight T(x)."""
    y = _y(x, ll)
    if revs == 0 and math.sqrt(0.6) < x < math.sqrt(1.4):
        eta = y - ll * x
        s1 = (1.0 - ll - x * eta) * 0.5
        q = 4.0 / 3.0 * _hyp2f1b(s1)
        return (eta ** 3 * q + 4.0 * ll * eta) * 0.5
    if -1.0 <= x < 1.0:
        psi = math.acos(max(-1.0, min(1.0, x * y + ll * (1.0 - x * x))))
    elif x > 1.0:
        psi = math.asinh((y - x * ll) * math.sqrt(x * x - 1.0))
    else:
        psi = 0.0
    return ((psi + revs * math.pi) / math.sqrt(abs(1.0 - x * x)) - x + ll * y) / (1.0 - x * x)


def _derivatives(x: float, y: float, t: float, ll: float) -> tuple[float, float, float]:
    omx2 = 1.0 - x * x
    if abs(omx2) < 1e-15:  # exactly parabolic: nudge off the removable singularity
        omx2 = math.copysign(1e-15, omx2 or 1.0)
    d1 =(3.0 * t * x - 2.0 + 2.0 * ll ** 3 * x / y) / omx2
    d2 = (3.0 * t + 5.0 * x * d1 + 2.0 * (1.0 - ll * ll) * ll ** 3 / y ** 3) / omx2
    d3 = (7.0 * x * d2 + 8.0 * d1 - 6.0 * (1.0 - ll * ll) * ll ** 5 * x / y ** 5) / omx2
    return d1, d2, d3


def _householder(x: float, t0: float, ll: float, revs: int, maxiter: int = 50) -> float:
    for _ in range(maxiter):
        y = _y(x, ll)
        t = _tof(x, ll, revs)
        f = t - t0
        d1, d2, d3 = _derivatives(x, y, t, ll)
        denom = d1 * (d1 * d1 - f * d2) + d3 * f * f / 6.0
        if denom == 0:
            break
        xn = x - f * (d1 * d1 - f * d2 / 2.0) / denom
        if abs(xn - x) < 1e-13 * abs(x) + 1e-14:
            return xn
        x = xn
    # Out of iterations (or a flat step): accept only a genuine root, never a wandering iterate.
    if not (math.isfinite(x) and -1.0 < x and abs(_tof(x, ll, revs) - t0) <= 1e-9 * max(1.0, t0)):
        raise ValueError("Lambert iteration did not converge")
    return x


def _t_min(ll: float, revs: int) -> float:
    """Minimum non-dimensional time of flight for ``revs`` full revolutions (Halley on dT/dx = 0)."""
    x = 0.1
    for _ in range(50):
        y = _y(x, ll)
        t = _tof(x, ll, revs)
        d1, d2, d3 = _derivatives(x, y, t, ll)
        denom = 2.0 * d2 * d2 - d1 * d3
        if d2 == 0 or denom == 0:
            break
        xn = x - 2.0 * d1 * d2 / denom
        if abs(xn - x) < 1e-13:
            x = xn
            break
        x = xn
    return _tof(x, ll, revs)


def _initial_guess(t: float, ll: float, revs: int, low_path: bool) -> float:
    if revs == 0:
        t0 = math.acos(ll) + ll * math.sqrt(1.0 - ll * ll)
        t1 = 2.0 * (1.0 - ll ** 3) / 3.0
        if t >= t0:
            return (t0 / t) ** (2.0 / 3.0) - 1.0
        if t < t1:
            return 2.5 * t1 / t * (t1 - t) / (1.0 - ll ** 5) + 1.0
        return math.exp(math.log(2.0) * math.log(t / t0) / math.log(t1 / t0)) - 1.0
    a = ((revs * math.pi + math.pi) / (8.0 * t)) ** (2.0 / 3.0)
    b = ((8.0 * t) / (revs * math.pi)) ** (2.0 / 3.0)
    x_left, x_right = (a - 1.0) / (a + 1.0), (b - 1.0) / (b + 1.0)
    return max(x_left, x_right) if low_path else min(x_left, x_right)


def lambert(mu: float, r1: Vec3, r2: Vec3, tof: float, *, normal: Vec3, prograde: bool = True,
            revs: int = 0, low_path: bool = True) -> tuple[Vec3, Vec3]:
    """Velocities (v1 at r1, v2 at r2) of the conic from ``r1`` to ``r2`` in ``tof`` seconds.

    ``normal`` fixes the sense of motion: with ``prograde=True`` the solution's angular momentum
    ``cross(r1, v1)`` has a positive component along ``normal``. Pass the departure body's
    ``cross(r, v)`` in the same frame. ``revs`` > 0 selects multi-revolution solutions (``low_path``
    picks the branch). Raises ValueError for degenerate geometry (collinear r1, r2) or when no
    ``revs``-revolution solution exists for this time of flight.
    """
    if not (mu > 0 and tof > 0):
        raise ValueError("mu and tof must be positive")
    if revs < 0:
        raise ValueError("revs must be >= 0")
    r1n, r2n = vnorm(r1), vnorm(r2)
    if r1n == 0 or r2n == 0:
        raise ValueError("positions must be non-zero")
    c = vsub(r2, r1)
    cn = vnorm(c)
    s = 0.5 * (r1n + r2n + cn)
    i_r1, i_r2 = vscale(r1, 1.0 / r1n), vscale(r2, 1.0 / r2n)
    h = vcross(i_r1, i_r2)
    if vnorm(h) < 1e-8:
        raise ValueError("Lambert geometry is degenerate: r1 and r2 are (anti)parallel, so the "
                         "transfer plane is undefined")
    i_h = vunit(h)
    ll = math.sqrt(max(0.0, 1.0 - min(1.0, cn / s)))
    if vdot(i_h, normal) < 0.0:  # the short way round would run against `normal`
        ll = -ll
        i_t1, i_t2 = vunit(vcross(i_r1, i_h)), vunit(vcross(i_r2, i_h))
    else:
        i_t1, i_t2 = vunit(vcross(i_h, i_r1)), vunit(vcross(i_h, i_r2))
    if not prograde:
        ll = -ll
        i_t1, i_t2 = vscale(i_t1, -1.0), vscale(i_t2, -1.0)
    t = math.sqrt(2.0 * mu / s ** 3) * tof

    if revs > 0:
        t00 = math.acos(ll) + ll * math.sqrt(1.0 - ll * ll)
        if t < t00 + revs * math.pi and t < _t_min(ll, revs):
            raise ValueError(f"no {revs}-revolution transfer exists for this time of flight")
    x = _householder(_initial_guess(t, ll, revs, low_path), t, ll, revs)
    y = _y(x, ll)

    gamma = math.sqrt(mu * s / 2.0)
    rho = (r1n - r2n) / cn
    sigma = math.sqrt(max(0.0, 1.0 - rho * rho))
    vr1 = gamma * ((ll * y - x) - rho * (ll * y + x)) / r1n
    vr2 = -gamma * ((ll * y - x) + rho * (ll * y + x)) / r2n
    vt1 = gamma * sigma * (y + ll * x) / r1n
    vt2 = gamma * sigma * (y + ll * x) / r2n
    v1 = vadd(vscale(i_r1, vr1), vscale(i_t1, vt1))
    v2 = vadd(vscale(i_r2, vr2), vscale(i_t2, vt2))
    if not all(math.isfinite(k) for k in v1 + v2):
        raise ValueError("Lambert solution is not finite")
    return v1, v2


# ---------------------------------------------------------------------------------------------
# transfer-window search


@dataclass
class Transfer:
    """One evaluated transfer. Vectors are in the ephemeris frame; v_inf are relative to the bodies."""

    departure_ut: float
    tof: float
    v_inf_departure: Vec3
    v_inf_arrival: Vec3
    cost: float
    departure_normal: Vec3 = field(repr=False, default=(0.0, 0.0, 0.0))
    origin_velocity: Vec3 = field(repr=False, default=(0.0, 0.0, 0.0))
    origin_position: Vec3 = field(repr=False, default=(0.0, 0.0, 0.0))

    @property
    def arrival_ut(self) -> float:
        return self.departure_ut + self.tof

    @property
    def v_inf_departure_mag(self) -> float:
        return vnorm(self.v_inf_departure)

    @property
    def v_inf_arrival_mag(self) -> float:
        return vnorm(self.v_inf_arrival)


def evaluate_transfer(mu: float, origin: StateFn, target: StateFn, departure_ut: float, tof: float,
                      cost: Callable[[float, float], float]) -> Transfer | None:
    """Solve the prograde single-revolution Lambert arc for one (departure UT, TOF) pair.

    Prograde is judged against the origin's own angular momentum at departure. Returns None when the
    geometry is degenerate or the cost is not finite."""
    r_o, v_o = origin(departure_ut)
    r_t, v_t = target(departure_ut + tof)
    normal = vcross(r_o, v_o)
    try:
        v1, v2 = lambert(mu, r_o, r_t, tof, normal=normal, prograde=True)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    vinf_d, vinf_a = vsub(v1, v_o), vsub(v2, v_t)
    c = cost(vnorm(vinf_d), vnorm(vinf_a))
    if not math.isfinite(c):
        return None
    return Transfer(departure_ut, tof, vinf_d, vinf_a, c, normal, v_o, r_o)


@dataclass
class WindowSearch:
    best: Transfer
    evaluations: int
    grid_departure_ut: list[float]
    grid_tof: list[float]
    grid_cost: list[list[float | None]]  # [departure index][tof index]


def search_transfer_window(mu: float, origin: StateFn, target: StateFn, *, t_start: float, t_end: float,
                           tof_min: float, tof_max: float, cost: Callable[[float, float], float],
                           n_departure: int = 72, n_tof: int = 36) -> WindowSearch:
    """Porkchop search for the cheapest prograde transfer.

    ``origin``/``target`` return (position, velocity) of each body at a UT in one inertial frame
    centred on their common primary (GM ``mu``). ``cost(v_inf_departure, v_inf_arrival)`` scores a
    transfer (e.g. ejection Δv + capture Δv). The departure span [t_start, t_end] and TOF span
    [tof_min, tof_max] are scanned on an ``n_departure`` x ``n_tof`` grid, then the best few cells are
    refined with a shrinking pattern search that stays inside both spans.
    """
    if not (t_end >= t_start and tof_max >= tof_min > 0):
        raise ValueError("need t_end >= t_start and tof_max >= tof_min > 0")
    n_departure, n_tof = max(2, n_departure), max(2, n_tof)
    deps = [t_start + (t_end - t_start) * i / (n_departure - 1) for i in range(n_departure)]
    tofs = [tof_min + (tof_max - tof_min) * j / (n_tof - 1) for j in range(n_tof)]
    evals = 0
    grid: list[list[float | None]] = []
    cells: list[tuple[float, int, int]] = []
    for i, ut in enumerate(deps):
        row: list[float | None] = []
        for j, tf in enumerate(tofs):
            tr = evaluate_transfer(mu, origin, target, ut, tf, cost)
            evals += 1
            row.append(tr.cost if tr else None)
            if tr:
                cells.append((tr.cost, i, j))
        grid.append(row)
    if not cells:
        raise ValueError("no valid transfer found in the searched span")

    d_step0 = (t_end - t_start) / (n_departure - 1) if t_end > t_start else 0.0
    f_step0 = (tof_max - tof_min) / (n_tof - 1) if tof_max > tof_min else 0.0

    def refine(ut: float, tf: float) -> tuple[Transfer | None, int]:
        cur = evaluate_transfer(mu, origin, target, ut, tf, cost)
        n = 1
        if cur is None:
            return None, n
        ds, fs = d_step0, f_step0
        while (ds > 1.0 or fs > 1.0) and n < 400:
            moved = False
            for du, dt in ((ds, 0.0), (-ds, 0.0), (0.0, fs), (0.0, -fs)):
                u = min(max(cur.departure_ut + du, t_start), t_end)
                f = min(max(cur.tof + dt, tof_min), tof_max)
                if (u, f) == (cur.departure_ut, cur.tof):
                    continue
                cand = evaluate_transfer(mu, origin, target, u, f, cost)
                n += 1
                if cand and cand.cost < cur.cost:
                    cur, moved = cand, True
                    break
            if not moved:
                ds, fs = ds * 0.5, fs * 0.5
        return cur, n

    best: Transfer | None = None
    seeds = _distinct_minima(sorted(cells), min_sep=(max(2, n_departure // 12), max(2, n_tof // 6)), k=4)
    for _, i, j in seeds:
        tr, n = refine(deps[i], tofs[j])
        evals += n
        if tr and (best is None or tr.cost < best.cost):
            best = tr
    if best is None:  # cannot happen: every seed cell evaluated successfully on the grid
        raise ValueError("transfer refinement failed")
    return WindowSearch(best, evals, deps, tofs, grid)


def _distinct_minima(cells: list[tuple[float, int, int]], min_sep: tuple[int, int], k: int):
    picked: list[tuple[float, int, int]] = []
    for c in cells:
        if all(abs(c[1] - p[1]) >= min_sep[0] or abs(c[2] - p[2]) >= min_sep[1] for p in picked):
            picked.append(c)
            if len(picked) == k:
                break
    return picked
