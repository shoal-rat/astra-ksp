"""Reflex tools: advance game time under control laws the AI commands, stopping on its triggers."""

from typing import Annotated, Any, Literal

from pydantic import Field

from astra.reflex import burn as _burn
from astra.reflex import warp as _warp
from astra.reflex.descent import DescentAttitude, DescentGuidance, DescentThrottle
from astra.reflex.engine import fly
from astra.registry import tool

_UNTIL = Field(description=(
    "Stop conditions (any-of). Each item is {\"metric\": <name>, \"op\": \">=\"|\"<=\"|\">\"|\"<\"|\"==\"|\"!=\", "
    "\"value\": <number or string>} or {\"event\": <name>}. Metrics: ut, met, altitude, surface_altitude, "
    "apoapsis_altitude, periapsis_altitude, time_to_apoapsis, time_to_periapsis, eccentricity, inclination (deg), "
    "semi_major_axis, period, time_to_soi_change, next_periapsis_altitude, orbital_speed, surface_speed, "
    "vertical_speed, horizontal_speed, latitude, longitude, dynamic_pressure (Pa), mach, atmosphere_density, "
    "static_pressure, g_force, pitch, heading, roll, angle_of_attack, sideslip, mass (kg), thrust (N), "
    "available_thrust (N), isp, twr, max_twr, local_g, throttle, stage, stage_propellant (kg), stage_dv, "
    "electric_charge (fraction), part_count, situation, body, node_time_to, node_remaining_dv, "
    "target_distance, target_speed, max_temp_fraction. Events: flameout, all_engines_out, staged, part_lost, "
    "soi_change, situation_change, landed, apoapsis_passed, periapsis_passed, node_passed."))

_THROTTLE = Field(description=(
    "Throttle law. A number 0..1 (constant); \"keep\" (leave as set); {\"mode\": \"twr\", \"twr\": x, "
    "\"min\"?, \"max\"?} (hold thrust-to-weight x in local gravity); {\"mode\": \"approach\", \"metric\": m, "
    "\"target\": x, \"feather_s\": s, \"min\": lo, \"max\": hi} (drive metric m to x, throttling down when the "
    "estimated time to reach it at full throttle falls below s; ends the reflex when reached). Any object "
    "may add \"max_q_pa\" to throttle down above that dynamic pressure."))

_ATTITUDE = Field(description=(
    "Attitude law (kRPC autopilot unless noted). \"keep\"; \"free\" (release autopilot); "
    "{\"mode\": \"hold\", \"pitch\": deg, \"heading\": deg, \"roll\"?: deg}; "
    "{\"mode\": \"pitch_program\", \"by\": \"altitude\"|\"speed\"|\"apoapsis\"|\"met\", \"points\": [[x, pitch_deg], ...], "
    "\"heading\": deg} (your table, linearly interpolated); {\"mode\": \"prograde\"|\"retrograde\", \"frame\": "
    "\"surface\"|\"orbital\", \"min_pitch\"?: deg, \"max_pitch\"?: deg, \"fallback_pitch\"?: deg, \"heading\"?: deg} "
    "(surface frame follows velocity relative to the ground — a gravity turn — with optional elevation clamps; "
    "fallback_pitch/heading are used while ground speed < 1 m/s); {\"mode\": \"normal\"|\"antinormal\"|"
    "\"radial_out\"|\"radial_in\"}; {\"mode\": \"up\"}; {\"mode\": \"target\"|\"anti_target\"}; "
    "{\"mode\": \"node\", \"index\"?: 0}; {\"mode\": \"vector\", \"vector\": [x,y,z], \"frame\": "
    "\"surface\"|\"orbital\"|\"body\"|\"inertial\"}; {\"mode\": \"sas\", \"sas_mode\": \"prograde\"|...} (stock SAS; "
    "basic probe cores only have stability_assist). Any autopilot mode may add \"roll\" and \"tuning\" "
    "({\"stopping_time\": [p,y,r], \"deceleration_time\": [...], \"attenuation_angle\": [...]})."))

_AUTOSTAGE = Field(description=(
    "Stage automatically when an engine runs dry? false (default): the reflex stops on flameout and you "
    "decide. true or {\"enabled\": true, \"max_stages\": n, \"min_interval_s\": s}: stage on flameout, but only "
    "when the next stage lights an engine without dropping propellant, or drops only empty parts while "
    "another engine keeps burning; anything else stops the reflex for your decision."))

_INTERLOCKS = Field(description=(
    "Override safety interlocks (they only stop the reflex and return control; they never act). Defaults: "
    "{\"flameout\": true, \"part_lost\": true, \"soi_change\": true, \"overheat\": 0.95, "
    "\"impact\": {\"seconds\": 10, \"speed_mps\": 12}, \"loss_of_control\": {\"error_deg\": 25, \"seconds\": 4, "
    "\"min_q_pa\": 3000}, \"low_power\": 0.02, \"dry_debounce_s\": 0.4}. Set an entry to false/null to disable."))


@tool("fly")
def fly_until(
    until: Annotated[list[dict[str, Any]], _UNTIL],
    throttle: Annotated[Any, _THROTTLE] = "keep",
    attitude: Annotated[Any, _ATTITUDE] = "keep",
    auto_stage: Annotated[Any, _AUTOSTAGE] = False,
    max_game_s: Annotated[float | None, Field(description="Stop after this much GAME time (seconds). Always "
                                              "set a sensible bound for the segment you are flying.")] = None,
    max_real_s: Annotated[float, Field(description="Wall-clock safety bound in seconds.")] = 600.0,
    interlocks: Annotated[dict[str, Any] | None, _INTERLOCKS] = None,
    physics_warp: Annotated[int, Field(description="Physics warp 0..3 during the reflex (e.g. long parachute "
                                        "descents). Keep 0 for powered ascent and landing.", ge=0, le=3)] = 0,
    exit_throttle: Annotated[Any, Field(description="Throttle when the reflex ends: \"keep\", \"cut\", or a "
                                        "number. The game is paused either way.")] = "keep",
    trace_points: Annotated[int, Field(description="How many evenly spaced trace samples to return.",
                                       ge=2, le=100)] = 16,
) -> dict:
    """Run the simulation under the throttle and attitude laws you command until a trigger fires.

    This is how you fly anything that changes faster than you can think: an ascent segment, a coast to
    apoapsis, a capture burn to a target apoapsis (throttle mode "approach"), a parachute descent. You
    choose every number. The reflex also stops on safety interlocks (flameout, part loss, SOI change,
    overheating, impact risk, loss of control, low power) and on max_game_s / max_real_s. When it
    returns, the game is paused. The result has stopped_by (why), events, stages_fired, a compact trace
    (rows ordered as trace_fields; t is game seconds since start), the final state, and control state.
    Fly in segments you can reason about, and re-plan from the result each time.
    """
    return fly(until=until, throttle=throttle, attitude=attitude, auto_stage=auto_stage,
               interlocks=interlocks, max_game_s=max_game_s, max_real_s=max_real_s,
               physics_warp=physics_warp, exit_throttle=exit_throttle, label="fly_until",
               trace_points=trace_points)


@tool("fly")
def fly_burn(
    node_index: Annotated[int, Field(description="Which maneuver node to execute (0 = next).", ge=0)] = 0,
    max_throttle: Annotated[float, Field(description="Throttle ceiling for the burn. Lower it for small, "
                                         "precise corrections on high-thrust stages.", gt=0, le=1)] = 1.0,
    lead_fraction: Annotated[float, Field(description="Fraction of the estimated burn time to start before "
                                          "the node time (0.5 centers the burn on the node).", ge=0, le=1)] = 0.5,
    align_deg: Annotated[float, Field(description="Pointing accuracy required to ignite at the planned start "
                                      "(degrees). The burn keeps tracking the vector, so a few degrees at "
                                      "ignition cost almost nothing.", gt=0)] = 3.0,
    max_late_s: Annotated[float, Field(description="If still not aligned this many seconds after the planned "
                                       "start, do not burn (a late burn along a node's fixed vector bends the "
                                       "orbit); re-plan instead.", ge=0)] = 5.0,
    align_margin_s: Annotated[float, Field(description="Time reserved before the start to turn and settle; "
                                           "warp stops this long before the burn.", gt=0)] = 60.0,
    feather_s: Annotated[float, Field(description="Start throttling down when the remaining Δv is less than "
                                      "this many seconds of full acceleration.", gt=0)] = 2.0,
    min_throttle: Annotated[float, Field(description="Throttle floor while feathering.", ge=0, le=1)] = 0.05,
    tolerance_mps: Annotated[float, Field(description="Cut off when the remaining Δv is below this.",
                                          gt=0)] = 0.1,
    auto_stage: Annotated[Any, _AUTOSTAGE] = False,
    stop_when: Annotated[list[dict[str, Any]] | None, Field(description=(
        "Optional extra stop triggers (same syntax as fly_until.until), e.g. stop an injection burn when "
        "next_periapsis_altitude reaches your target even if the node says otherwise."))] = None,
    remove_node: Annotated[bool, Field(description="Delete the node after the burn.")] = True,
    max_axis_offset_deg: Annotated[float, Field(description="Refuse to burn if the control part's forward "
                                                "axis is further than this from the engines' thrust axis.")] = 10.0,
) -> dict:
    """Execute a maneuver node: warp near it, align, burn with a feathered cutoff, and report.

    Before burning it checks that an engine can thrust and that the control point faces along the
    thrust axis (the autopilot aims the control part, not the engines). It warps to align_margin_s
    before the start (start = node time - lead_fraction * estimated burn time) and turns to the burn
    vector; it ignites at the start once within align_deg (or aborts if not aligned within max_late_s,
    because a late burn misses the plan), then burns: full throttle until the remaining Δv is within feather_s
    seconds of acceleration, then proportionally down to min_throttle, cutting off at tolerance_mps,
    on overshoot (the burn vector reverses), on a wrong-way burn (remaining Δv grows), on your
    stop_when triggers, or on interlocks. Applied Δv is integrated over game time. The game is paused
    afterwards. Compute the node first (compute_maneuver / compute_node_search) and create it with
    node_create; check the predicted result with orbit_info before burning.
    """
    return _burn.execute_node(node_index=node_index, max_throttle=max_throttle, lead_fraction=lead_fraction,
                              align_deg=align_deg, max_late_s=max_late_s, align_margin_s=align_margin_s,
                              feather_s=feather_s, min_throttle=min_throttle, tolerance_mps=tolerance_mps,
                              auto_stage=auto_stage, stop_when=stop_when, remove_node=remove_node,
                              max_axis_offset_deg=max_axis_offset_deg)


@tool("fly")
def fly_warp(
    to: Annotated[Literal["ut", "in", "apoapsis", "periapsis", "soi", "node"],
                  Field(description="Warp until: an absolute UT, 'in' seconds from now, the next apoapsis / "
                                    "periapsis, the next SOI change, or the next maneuver node.")],
    offset_s: Annotated[float, Field(description="Arrive this many seconds BEFORE the event (lead time you "
                                     "need to prepare, e.g. to turn for a burn).", ge=0)],
    ut: Annotated[float | None, Field(description="Target UT when to='ut'.")] = None,
    seconds: Annotated[float | None, Field(description="Seconds from now when to='in'.", gt=0)] = None,
    floor_alt_m: Annotated[float | None, Field(description=(
        "Airless bodies: never warp past the moment the orbit descends through this altitude (use a height "
        "above the terrain you computed, e.g. with compute_terrain). Atmospheric bodies always stop at the "
        "atmosphere edge. Ignored for a vessel that is landed, splashed or on the pad."))] = None,
    max_rails_rate: Annotated[float, Field(description="Highest rails warp rate allowed.", gt=1)] = 100000.0,
) -> dict:
    """Time-warp safely: throttle off; in flight never inside the atmosphere and never past atmospheric
    entry. A vessel that is landed, splashed or on the pad rails-warps on the ground, atmosphere or not
    (e.g. to wait for a launch window).

    In flight the warp stops early (and says so in `clamped`) if the trajectory would enter the
    atmosphere, or on an airless body descend below floor_alt_m, before the requested time. Rails warp
    is limited by altitude, so long waits in low orbits run slowly in real time; plan windows
    accordingly. The game is paused afterwards; the result shows the orbit and SOI after the warp.
    """
    return _warp.warp(to=to, ut=ut, seconds=seconds, offset_s=offset_s, floor_alt_m=floor_alt_m,
                      max_rails_rate=max_rails_rate)


@tool("fly")
def fly_descent(
    touchdown_mps: Annotated[float, Field(description=(
        "Target vertical speed at touchdown (m/s), also the slowest sink the terminal phase commands. Choose "
        "from the landing gear's crash tolerance with margin, lower on slopes."), gt=0)],
    terminal_alt_m: Annotated[float, Field(description=(
        "Height of the vessel's lowest point above the terrain floor (the highest ground sampled along the "
        "track ahead, or the ground below if that is higher) at which braking hands over to the terminal "
        "phase. Braking continues below it while the lander is still too fast for the terminal law. It also "
        "sets the terminal speed (terminal_rate x terminal_alt_m, capped by what the engine can stop). Size "
        "it to cancel the residual drift with the tilt you allow; do not pad it for terrain relief ahead, "
        "which the guidance samples itself. Every metre of terminal descent is near-hover and costs fuel."),
        gt=0)],
    throttle_reserve: Annotated[float, Field(description=(
        "Fraction of maximum thrust held back when deciding when to start braking: the predictor plans the "
        "stop at (1 - reserve) x max, and the burn then uses the lowest throttle (up to full) that still "
        "holds the terminal gate. It covers throttle and attitude lag, pointing error, and terrain between "
        "or beside the sampled track, not the relief ahead. More reserve brakes earlier and costs fuel."),
        ge=0, lt=0.9)],
    touchdown_drift_mps: Annotated[float, Field(description=(
        "Largest sideways speed allowed at contact (m/s). A few metres up the lander holds height and tilts "
        "against the drift until it is below this, then sets down upright. Derive it from the leg stance, "
        "the centre-of-mass height and the slope: sliding sideways on a slope loads one leg and tips narrow "
        "landers over. A smaller value costs hover time (about g per second of hover)."), gt=0)],
    terminal_rate: Annotated[float, Field(description=(
        "Terminal sink rate per metre of height (1/s): target vertical speed = -min(v_term, "
        "max(touchdown_mps, rate x height above the ground below)), where v_term = min(rate x "
        "terminal_alt_m, ((1 - reserve) x max acceleration - g) / rate), at least touchdown_mps. 1/rate is "
        "the time constant of the final descent; keep it several times the lander's turn time."),
        gt=0)] = 0.15,
    max_tilt_deg: Annotated[float, Field(description=(
        "Maximum tilt from vertical used to cancel horizontal drift in the terminal phase. Horizontal "
        "authority is about g x tan(max_tilt); the sink command keeps the vertical thrust it needs first. "
        "Must be > 0: without tilt the terminal phase can never cancel drift."), gt=0, le=45)] = 15.0,
    tilt_gain_deg_per_mps: Annotated[float, Field(description=(
        "Terminal tilt per m/s of horizontal drift (> 0). Small drift decays at about g x gain (gain in rad "
        "per m/s) per second; a high gain on a slow-turning lander oscillates."), gt=0)] = 4.0,
    sink_gain: Annotated[float, Field(description=(
        "Terminal vertical-speed controller gain (1/s): commanded acceleration per m/s of sink-rate error. "
        "Keep 1/sink_gain longer than the throttle and attitude response."), gt=0)] = 0.8,
    legs_alt_m: Annotated[float | None, Field(description="Deploy landing legs when the vessel's lowest "
                                              "point is below this height above the ground (null: leave "
                                              "them as they are).")] = None,
    max_game_s: Annotated[float, Field(description=(
        "Game-time safety bound for the whole descent, including the coast, hover and the settle after "
        "contact. On timeout the throttle is cut in the air: size it from compute_descent with margin."),
        gt=0)] = 900.0,
) -> dict:
    """Fly a powered landing from a descending trajectory to touchdown.

    Call this once the vessel is falling toward the surface (after your deorbit burn); it refuses to
    start from an orbit whose periapsis is above the surface or when (1 - reserve) x max thrust cannot
    beat surface gravity. The autopilot takes over at once (SAS off). On a late hand-off braking can
    start at once, but braking thrust is held back until the vessel has turned within about 10 deg of
    the braking direction (none beyond 45 deg), so a lander still in its deorbit attitude does not burn
    the wrong way; point it surface-retrograde before a low hand-off, since turn time is braking lost.

    Braking: a suicide-burn predictor integrates a surface-retrograde burn at (1 - reserve) x max under
    gravity (less the centrifugal relief of the horizontal speed). The gate is terminal_alt_m above the
    terrain floor: the highest ground sampled along the track ahead, out past the predicted stop, with
    peaks remembered until passed. Engine off while the predicted stop ends above the gate; then
    retrograde with the lowest throttle (up to full) whose predicted stop still holds the gate. When
    full thrust retrograde cannot hold it, or the gate is reached with more horizontal speed than the
    terminal law can cancel, it brakes height-first: the vertical thrust holds a sink it can still stop
    and the rest kills horizontal speed, so the attitude is then off retrograde by design.

    Terminal (below the gate and slow): sink proportional to the height above the ground below,
    tilting against drift; a few metres up it hovers until the drift is below touchdown_drift_mps, then
    sets down upright. If the ground falls away it goes back to coasting and braking.

    After contact the engine is cut and the reflex runs 4 more game seconds, then leaves the autopilot
    off and SAS on (holding the settled attitude). It also stops on interlocks (part loss, flameout)
    and max_game_s. The result adds phase_at_end, touchdown (speeds and height at contact), and, when
    they happened, hover_to_cancel_drift_s and brake_held_for_pointing_s. Atmospheric drag is not
    modeled (it only helps). For parachute landings use fly_until instead.
    """
    guide = DescentGuidance(touchdown_mps, throttle_reserve, terminal_alt_m, terminal_rate, max_tilt_deg,
                            tilt_gain_deg_per_mps, sink_gain, legs_alt_m, drift_max_mps=touchdown_drift_mps)
    tlaw = DescentThrottle(guide)
    report = fly(throttle_law=tlaw, attitude_law=DescentAttitude(tlaw), exit_throttle="cut",
                 interlocks={"impact": None, "soi_change": False, "loss_of_control": None},
                 max_game_s=max_game_s, max_real_s=max(600.0, max_game_s * 1.5), label="fly_descent",
                 trace_points=20)
    report["phase_at_end"] = guide.phase
    report["touchdown"] = guide.touchdown
    if guide.hovered_s:
        report["hover_to_cancel_drift_s"] = round(guide.hovered_s, 1)
    if guide.brake_held_s >= 0.05:
        report["brake_held_for_pointing_s"] = round(guide.brake_held_s, 1)
    if guide.touchdown is None and report["stopped_by"]["kind"] != "law":
        report["note"] = "no touchdown: read stopped_by and telemetry before deciding the next step"
    return report
