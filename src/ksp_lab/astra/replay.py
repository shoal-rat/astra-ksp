"""OFFLINE REPLAY ENGINE for ASTRA's key flight state machines.

KSP is hard to put in CI: every real run needs the live game + kRPC + MechJeb, and a flight takes
minutes-to-hours of wall clock. So the actual flight loops in ``tools/deploy_relay.py``,
``tools/deploy_relay_transfer.py`` and ``src/ksp_lab/flight_controller.py`` interleave their DECISION
logic with live kRPC reads, warps and burns — you cannot exercise the decision logic without flying.

This module extracts the four key state machines as PURE decision functions. Each consumes a
TELEMETRY SEQUENCE — a list of per-tick dicts, the same shape the live loops read off kRPC each poll —
and emits a DECISION per tick (a transition label, or a terminal). No kRPC, no network, no game: the
logic can be driven by hand-authored / recorded traces and asserted in pytest. This is the offline
regression net that catches state-machine logic regressions WITHOUT flying.

The four machines mirror the live logic IN SPIRIT (the thresholds and failure modes are re-implemented
here for replay; we deliberately do NOT import ``deploy_relay`` or the flight controller so a replay
test never drags in kRPC and never couples to the live loop's incidental structure):

  * ``ascent_sm``   — climb / stage / circularize / ABORT(reason)   (mirrors the fail-fast abort modes:
                      break-up, falling back, crash, diverging under power; apoapsis>=target -> circularize)
  * ``transfer_sm`` — coast / warp_to_periapsis / corrected / CAPTURE / ARRIVED   (capture fires at the
                      periapsis INSIDE the target SOI, never before)
  * ``docking_sm``  — rendezvous / approach / DOCKED / ABORT(timeout)
  * ``recovery_sm`` — entry / deploy_chutes (only when safe: <250 m/s AND <5 km) / LANDED / RECOVERED

Each is a callable ``sm(tick, carry) -> (decision: str, carry)`` where ``carry`` is the small per-machine
state threaded across ticks (rolling history, latched flags). ``replay_trace`` runs a whole trace through
a machine and returns the decision timeline; ``assert_reaches`` checks a terminal was hit.

A DECISION is a short string. Terminals are upper-case (``CIRCULARIZE``, ``CAPTURE``, ``ARRIVED``,
``DOCKED``, ``LANDED``, ``RECOVERED``) or an ``ABORT(...reason...)``; intermediate transitions are
lower-case (``climbing``, ``coast``, ``approach``, ``deploy_chutes``, ...). Once a machine reaches a
HARD terminal (a capture/dock/abort/recovery) it latches and emits that terminal for every later tick,
so the timeline's tail is the outcome.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

Tick = dict[str, Any]
Carry = dict[str, Any]
# An SM is a pure step: (current tick, carried state) -> (decision string, new carried state).
StateMachine = Callable[[Tick, Carry], tuple[str, Carry]]


def _abort(reason: str) -> str:
    return f"ABORT({reason})"


def is_abort(decision: str) -> bool:
    return decision.startswith("ABORT(")


# Terminals that, once emitted, END the machine: every later tick just re-emits the same decision.
# NOTE: LANDED is deliberately NOT here — it is a real terminal of the recovery machine, but a recovery
# trace legitimately PROGRESSES from LANDED (touchdown) to RECOVERED (back at the space centre). If the
# engine latched at LANDED it would never see the recovery tick. The recovery SM latches RECOVERED itself
# (via its carry), so a trace that ends at touchdown still shows LANDED as the final decision.
_HARD_TERMINALS = {"CIRCULARIZE", "CAPTURE", "ARRIVED", "DOCKED", "RECOVERED"}


def _is_terminal(decision: str) -> bool:
    return decision in _HARD_TERMINALS or is_abort(decision)


# ==================================================================================================
# 1. ASCENT — climb / stage / circularize / ABORT(reason)
# ==================================================================================================
# Mirrors deploy_relay._ascent_has_failed IN SPIRIT: a short rolling history, and an abort fires only
# when a real failure mode holds for ``bad_streak`` consecutive ticks (so a one-frame glitch is ridden
# out). On a healthy ascent it emits ``climbing``; a controlled part-count drop above the half-count
# threshold (a normal booster separation) emits ``stage``; once apoapsis reaches the target it emits the
# CIRCULARIZE terminal. The failure modes (in priority order, matching the live predicate):
#   crash (landed/splashed after liftoff) > break-up (part-count collapse) > falling-back > diverging.
ASCENT_BAD_STREAK = 3
ASCENT_PARTS_FRAC = 0.5      # part count below this fraction of the post-staging count = broke up


def make_ascent_sm(
    *,
    post_staging_part_count: int,
    payload_part_count: int = 3,
    atmosphere_top_m: float = 70_000.0,
    target_apoapsis_m: float = 100_000.0,
    bad_streak: int = ASCENT_BAD_STREAK,
) -> StateMachine:
    """Build an ascent state machine bound to a vehicle's baselines. Tick keys:
        alt              (float) surface altitude (m)            [carried for context only]
        apoapsis         (float) apoapsis altitude (m)
        vertical_speed   (float) surface-frame vertical speed (m/s; negative = falling)
        part_count       (int)   live active-vessel part count
        engine_lit       (bool)  is any engine lit
        situation        (str)   kRPC situation, e.g. "Vessel.Situation.flying" / "...landed"
    """
    n = max(1, int(bad_streak))
    collapse_threshold = max(int(post_staging_part_count * ASCENT_PARTS_FRAC), int(payload_part_count) + 1)

    def _sit(s: Tick) -> str:
        return str(s.get("situation", "")).split(".")[-1].lower()

    def step(tick: Tick, carry: Carry) -> tuple[str, Carry]:
        hist = carry.setdefault("hist", [])
        hist.append(tick)
        if len(hist) > 8:
            del hist[:-8]

        apoapsis = float(tick.get("apoapsis", 0.0))
        near_target = apoapsis >= 0.95 * target_apoapsis_m

        # --- terminal: apoapsis reached the target -> hand off to the circularise burn.
        if apoapsis >= target_apoapsis_m:
            return "CIRCULARIZE", carry

        # --- abort checks need a full streak of evidence first.
        if len(hist) >= n:
            tail = hist[-n:]
            cur = tail[-1]

            # 3. CRASHED — landed/splashed after liftoff (decisive once confirmed across the streak).
            if all(_sit(s) in ("landed", "splashed") for s in tail):
                return _abort(f"situation '{_sit(cur)}' after liftoff — vehicle crashed"), carry

            # 1. PART-COUNT COLLAPSE — broke up (down past half the post-staging count).
            if post_staging_part_count > 0 and all(
                int(s.get("part_count", post_staging_part_count)) < collapse_threshold for s in tail
            ):
                cur_pc = int(cur.get("part_count", 0))
                return _abort(
                    f"part count {cur_pc} << {post_staging_part_count} "
                    f"(threshold {collapse_threshold}) — vehicle broke up"
                ), carry

            apo_strictly_decaying = all(
                float(tail[i].get("apoapsis", 0.0)) < float(tail[i - 1].get("apoapsis", 0.0))
                for i in range(1, len(tail))
            )

            # 2. FALLING BACK — below the atmosphere, descending, apoapsis decaying across the streak.
            if (not near_target
                    and apo_strictly_decaying
                    and all(float(s.get("apoapsis", 0.0)) < atmosphere_top_m for s in tail)
                    and all(float(s.get("vertical_speed", 0.0)) < 0.0 for s in tail)):
                return _abort(
                    f"apoapsis decaying {float(tail[0].get('apoapsis', 0.0)) / 1000:.0f}->"
                    f"{apoapsis / 1000:.0f} km, below atmosphere — falling back"
                ), carry

            # 4. DIVERGING — apoapsis strictly losing ground under power, still well short of target.
            if (not near_target
                    and apo_strictly_decaying
                    and all(bool(s.get("engine_lit", False)) for s in tail)
                    and apoapsis < 0.7 * target_apoapsis_m):
                return _abort(
                    f"apoapsis diverging {float(tail[0].get('apoapsis', 0.0)) / 1000:.0f}->"
                    f"{apoapsis / 1000:.0f} km under power — ascent losing ground"
                ), carry

        # --- nominal: detect a controlled staging drop, else we are climbing.
        if len(hist) >= 2:
            prev_pc = int(hist[-2].get("part_count", post_staging_part_count))
            cur_pc = int(tick.get("part_count", post_staging_part_count))
            # A drop that stays ABOVE the break-up threshold, while apoapsis is still rising, is a
            # normal booster separation -> emit 'stage' (not an abort, not plain 'climbing').
            if cur_pc < prev_pc and cur_pc >= collapse_threshold and apoapsis >= float(
                hist[-2].get("apoapsis", 0.0)
            ):
                return "stage", carry
        return "climbing", carry

    return step


# ==================================================================================================
# 2. TRANSFER — coast / warp_to_periapsis / corrected / CAPTURE / ARRIVED
# ==================================================================================================
# Mirrors the Mun/Duna transfer flow in deploy_relay_transfer IN SPIRIT: the craft coasts on its
# transfer arc; if the predicted/encounter periapsis is unsafe (sub-surface / too low) it emits
# ``corrected`` (the grid-search course-correction); once inside the TARGET SOI it warps to periapsis
# (``warp_to_periapsis``) and CAPTUREs there with the retrograde burn — capture fires AT periapsis,
# never at the SOI edge. After the capture burn, a bound orbit around the target is ``ARRIVED``.
TRANSFER_CAPTURE_TTP_S = 60.0       # "at periapsis" = time_to_periapsis at/under this many seconds
TRANSFER_MIN_SAFE_PE_M = 5_000.0    # an encounter periapsis below this is an impact risk -> correct


def make_transfer_sm(
    *,
    target_body: str,
    capture_ttp_s: float = TRANSFER_CAPTURE_TTP_S,
    min_safe_periapsis_m: float = TRANSFER_MIN_SAFE_PE_M,
) -> StateMachine:
    """Build a transfer state machine bound to a target body. Tick keys:
        body              (str)   current SOI body name
        periapsis         (float) periapsis altitude (m) of the current orbit
        apoapsis          (float) apoapsis altitude (m)                       [context]
        ut                (float) universal time (s)                          [context]
        time_to_periapsis (float) seconds to the next periapsis
    """
    def step(tick: Tick, carry: Carry) -> tuple[str, Carry]:
        body = str(tick.get("body", ""))
        periapsis = float(tick.get("periapsis", 0.0))
        ttp = float(tick.get("time_to_periapsis", 1e9))

        if carry.get("captured"):
            # Already captured; a subsequent bound orbit around the target = ARRIVED (final).
            return "ARRIVED", carry

        if body != target_body:
            # Still on the transfer arc (heliocentric / departure-body SOI). If the PREDICTED encounter
            # periapsis is unsafe, the live code course-corrects before warping into the SOI.
            if periapsis < min_safe_periapsis_m and periapsis != 0.0:
                return "corrected", carry
            return "coast", carry

        # Inside the TARGET SOI now.
        if ttp <= capture_ttp_s:
            # AT periapsis inside the SOI -> the retrograde capture burn fires here (and only here).
            carry["captured"] = True
            return "CAPTURE", carry
        # In the SOI but periapsis is still far away: warp down to it (never burn at the SOI edge).
        return "warp_to_periapsis", carry

    return step


# ==================================================================================================
# 3. DOCKING — rendezvous / approach / DOCKED / ABORT(timeout)
# ==================================================================================================
# Mirrors run_dock_and_transfer IN SPIRIT: a far gap is closed with the main engine (``rendezvous``);
# inside the fine-RCS range the chaser creeps in nulling relative speed (``approach``); the ports
# magnetically mate -> DOCKED. If the gap never closes within the tick budget, ABORT(timeout).
DOCK_APPROACH_RANGE_M = 2_500.0     # below this, switch from bulk rendezvous to fine proximity ops
DOCK_TIMEOUT_TICKS = 0              # 0 => derive from the trace length at build time (see below)


def make_docking_sm(
    *,
    approach_range_m: float = DOCK_APPROACH_RANGE_M,
    timeout_ticks: int | None = None,
) -> StateMachine:
    """Build a docking state machine. Tick keys:
        distance_m    (float) chaser->target separation (m)
        rel_speed_mps (float) relative speed (m/s)                  [context for the approach phase]
        ports_state   (str)   docking-port state, e.g. "ready"/"approaching"/"docked"
    ``timeout_ticks`` aborts if not docked within that many ticks (None => no tick-count timeout; a
    trace can still signal a timeout via a final tick whose ports_state never reaches 'docked')."""
    def step(tick: Tick, carry: Carry) -> tuple[str, Carry]:
        i = carry.get("i", 0)
        carry["i"] = i + 1
        if carry.get("docked"):
            return "DOCKED", carry

        ports = str(tick.get("ports_state", "")).lower()
        distance = float(tick.get("distance_m", 1e9))

        if ports.endswith("docked"):
            carry["docked"] = True
            return "DOCKED", carry

        if timeout_ticks is not None and carry["i"] >= int(timeout_ticks):
            # Ran out the budget without mating the ports.
            return _abort(f"docking timeout after {carry['i']} ticks at {distance:.0f} m"), carry

        if distance > approach_range_m:
            return "rendezvous", carry
        return "approach", carry

    return step


# ==================================================================================================
# 4. RECOVERY — entry / deploy_chutes (when safe) / LANDED / RECOVERED
# ==================================================================================================
# Mirrors _recover_on_kerbin IN SPIRIT: through reentry the craft is in ``entry``; chutes are armed
# ONLY when it is safe to do so — below the safe airspeed AND low enough (the spec's <250 m/s AND
# <5 km), the gate that the live code defers to MechJeb so a guessed-too-high chute trigger can't rip
# them off. Touchdown (situation landed/splashed) is LANDED; once recovered to the space centre it is
# RECOVERED (final).
RECOVERY_CHUTE_SAFE_SPEED_MPS = 250.0
RECOVERY_CHUTE_SAFE_ALT_M = 5_000.0


def make_recovery_sm(
    *,
    chute_safe_speed_mps: float = RECOVERY_CHUTE_SAFE_SPEED_MPS,
    chute_safe_alt_m: float = RECOVERY_CHUTE_SAFE_ALT_M,
) -> StateMachine:
    """Build a recovery state machine. Tick keys:
        body            (str)   landing body name (context)
        altitude        (float) surface altitude (m)
        surface_speed   (float) surface-frame speed (m/s)
        chutes_deployed (bool)  are the chutes already out
        situation       (str)   kRPC situation, e.g. "...flying"/"...landed"/"...recovered"
    """
    def _sit(tick: Tick) -> str:
        return str(tick.get("situation", "")).split(".")[-1].lower()

    def step(tick: Tick, carry: Carry) -> tuple[str, Carry]:
        if carry.get("recovered"):
            return "RECOVERED", carry

        sit = _sit(tick)
        if sit == "recovered":
            carry["recovered"] = True
            return "RECOVERED", carry
        if sit in ("landed", "splashed") or carry.get("landed"):
            # Touchdown latches LANDED inside the machine: it stays LANDED until a 'recovered' tick (the
            # craft is on the ground and only the recovery action lifts it off). The engine does NOT latch
            # LANDED, so this machine is the sole place the LANDED->RECOVERED progression is allowed.
            carry["landed"] = True
            return "LANDED", carry

        altitude = float(tick.get("altitude", 1e9))
        speed = float(tick.get("surface_speed", 1e9))
        chutes_out = bool(tick.get("chutes_deployed", False))

        # SAFE chute deploy: slow enough AND low enough. Once armed it stays armed (carry latch) — the
        # craft keeps falling under canopy until touchdown. Never arm above the gate.
        if not chutes_out and not carry.get("chutes_armed"):
            if speed < chute_safe_speed_mps and altitude < chute_safe_alt_m:
                carry["chutes_armed"] = True
                return "deploy_chutes", carry
            return "entry", carry
        # Chutes are out / armed: descending under canopy until touchdown.
        return "deploy_chutes" if (chutes_out or carry.get("chutes_armed")) else "entry", carry

    return step


# ==================================================================================================
# REPLAY ENGINE
# ==================================================================================================
def replay_trace(state_machine: StateMachine, trace: Iterable[Tick]) -> list[str]:
    """Run ``trace`` (a sequence of per-tick dicts) through ``state_machine`` and return the DECISION
    timeline (one string per tick). The machine threads its own ``carry`` state across ticks. Once a
    HARD terminal (circularize/capture/arrived/dock/land/recover/abort) is emitted, every later tick
    re-emits that same terminal — the outcome latches, so the tail of the timeline is the result."""
    decisions: list[str] = []
    carry: Carry = {}
    latched: str | None = None
    for tick in trace:
        if latched is not None:
            decisions.append(latched)
            continue
        decision, carry = state_machine(tick, carry)
        decisions.append(decision)
        if _is_terminal(decision):
            latched = decision
    return decisions


def assert_reaches(decisions: list[str], terminal: str) -> None:
    """Assert the decision timeline reached ``terminal`` (exact match, or — for ``ABORT`` — a prefix
    match so callers can assert ``ABORT`` generically or a specific reason substring via ``in``)."""
    if terminal == "ABORT":
        hits = [d for d in decisions if is_abort(d)]
        assert hits, f"expected an ABORT, got timeline {decisions}"
        return
    assert terminal in decisions, f"expected to reach {terminal!r}, got timeline {decisions}"


def load_trace(path: str | Path) -> tuple[dict[str, Any], list[Tick]]:
    """Load a trace JSON file. Format: a dict with ``machine`` (which SM), optional ``params`` (kwargs
    for the SM builder), ``expect`` (a terminal/reason the test asserts), and ``ticks`` (the sequence).
    Returns ``(meta, ticks)`` where meta carries machine/params/expect."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    ticks = list(data.get("ticks", []))
    meta = {k: v for k, v in data.items() if k != "ticks"}
    return meta, ticks


# Registry mapping a trace's ``machine`` field to its SM builder, so a generic test harness can load
# any trace file and build the right machine from its ``params`` without a per-trace branch.
SM_BUILDERS: dict[str, Callable[..., StateMachine]] = {
    "ascent": make_ascent_sm,
    "transfer": make_transfer_sm,
    "docking": make_docking_sm,
    "recovery": make_recovery_sm,
}


def build_from_meta(meta: dict[str, Any]) -> StateMachine:
    """Build the state machine named by ``meta['machine']`` with ``meta.get('params', {})`` kwargs."""
    machine = meta["machine"]
    builder = SM_BUILDERS[machine]
    return builder(**meta.get("params", {}))
