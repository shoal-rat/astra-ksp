# Atmospheric entry: reentry, aerobraking, aerocapture, and parachutes

Keywords: reentry, re-entry, entry, entry corridor, atmospheric entry, heat shield, heating, parachute, parachutes, chutes, splashdown, landing on kerbin, duna landing, eve, laythe, aerobraking, aerobrake, aerocapture, mj_land, return home, recovery

The atmosphere can remove thousands of m/s for free, or burn the vessel up, or let it skip back out.
Everything here comes from two numbers you control: where the periapsis sits in the atmosphere, and
what shape arrives there.

## 1. The corridor

Read the density profile with `body_info` (density and pressure versus altitude). Near a height h
the atmosphere is close to exponential, ρ ≈ ρ_p·e^(−(h − h_p)/H); get the scale height from two
samples, H = Δh / ln(ρ1/ρ2).

**How much one pass removes** (a model for planning, calibrated in flight). With ballistic
coefficient β = m/(Cd·A), drag gives dv/ds = −ρv/(2β). Along a pass through periapsis radius r_p on
an orbit of eccentricity e, the density integral is

    I = ∫ρ ds ≈ ρ_p · √(2π · r_p · H · (1 + e)/e)

so the speed after the pass is v_out ≈ v_p · e^(−I/(2β)). `compute_calc` evaluates it. KSP's drag
is not a constant Cd·A, so treat β as an effective value: estimate it from the capsule's mass and
cross-section, then **recalibrate** from the first real pass (β = I / (2·ln(v_in/v_out)), with v_in,
v_out from vis-viva before and after). The model is for passes that exit the atmosphere again; an
orbit whose periapsis is deep enough to stop the vessel is simply an entry.

- **Too shallow:** the vessel loses a little speed and leaves again (a 48 km Kerbin periapsis skipped
  out pass after pass in an earlier flight; MechJeb's landing autopilot would not deorbit it either).
- **Too deep:** deceleration and heating peak higher. Heating rises steeply with speed (roughly with
  v³) and with density; watch `max_temp_fraction` and `g_force`.
- **Captured in one pass:** after the pass the new apoapsis must fall inside the atmosphere:
  a' = 1/(2/r_p − v_out²/μ), r_a' = 2a' − r_p ≤ R + atmosphere depth.
- Evidence for Kerbin returns (from low orbit and from the Mun): periapses around 25–35 km captured and
  landed reliably. That is a data point for Kerbin with those capsules, not a rule for other bodies:
  derive the corridor from the density profile of the body you are entering.

**Getting into the corridor.** From orbit: a retrograde burn at apoapsis lowers the periapsis
(`compute_maneuver(kind="deorbit_to_periapsis")`). Arriving from another body: correct the periapsis
far out, where it is cheap, and verify it in the patch chain (`orbit_info`).

## 2. Configure the vessel for entry

- Jettison everything behind the heat shield in vacuum (after the last engine burn): a long stack
  tumbles, parts stick into the flow, and the pod can shear off without its chute. Only the short
  capsule (pod, shield, parachutes) should enter.
- Heat shield facing the flow: surface retrograde attitude (`{"mode": "retrograde", "frame":
  "surface"}`) or stock SAS retrograde. A capsule with its shield forward is usually stable on its
  own; the autopilot's authority is small against aerodynamic torque at high dynamic pressure.
- Legs and other deployables retracted (extended legs were ripped off on an entry).
- No warp inside the atmosphere during the hot part: `fly_warp` stops at the edge. Physics warp in
  `fly_until` is acceptable only for a slow parachute descent.
- Foreign atmospheres: a lander that aerocaptured engine-first with its shield sized and placed for
  the home return broke up. Enter behind the shield or capture propulsively above the air.

## 3. Parachutes

- Terminal speed under chutes: v_t = √(2·m·g / (ρ·CdA_total)), with CdA_total ≈ N·CdA_one.
- Calibrate CdA_one from an observed steady descent (CdA = 2·m·g/(ρ·v²)), or from a previous flight
  of the same chute; the part file does not give it directly.
- Chutes needed for a touchdown speed v at the landing site: N = ⌈2·m·g / (ρ_site·v²·CdA_one)⌉, with
  ρ_site from `body_info` at the site's altitude. Thin atmospheres need many chutes or a propulsive
  final burn (one chute gave ~30 m/s at Duna in an earlier flight; two crews were lost that way).
- Stock chutes arm at any time and open only when the air is thick enough (their minimum-pressure
  setting) and fully deploy at their deploy altitude; arming early is harmless. The danger is opening
  while too fast or too hot, which destroys them; earlier flights saw rips around 250 m/s. Arm with
  `control_part` (or the stage that holds them), and read their state with `vessel_parts`.
- Touchdown speed below the crash tolerance of whatever touches first (`part_info`).

## 4. Aerobraking and aerocapture

Aerobraking lowers an apoapsis over several passes; aerocapture turns a hyperbola into an orbit in
one pass. Use the pass model: pick the periapsis that removes the Δv you want per pass, fly one pass,
recalibrate β from what actually happened, and adjust the periapsis at the next apoapsis. Protect
the vessel: heat shield forward, service parts that cannot take the heat jettisoned or shielded.
A propulsive Duna landing that worked: aerobrake from ~1,300 to ~400 m/s, then a powered landing
without chutes.

## 5. Flying the entry

1. `game_checkpoint` before the deorbit or the final correction.
2. Put the periapsis in the corridor; journal the corridor reasoning.
3. `fly_warp(to="periapsis", ...)` stops at the atmosphere edge by itself.
4. `fly_until` with surface-retrograde attitude and triggers such as `max_temp_fraction`,
   `surface_speed <= <safe chute speed>`, `altitude <= <arming height>`, `{"event": "landed"}`,
   `{"event": "part_lost"}`; then arm or deploy the chutes and continue to touchdown.
5. MechJeb alternative: `mj_land` owns deorbit timing, attitude, and chute deployment once the
   periapsis is already in a real corridor. It will not deorbit from a stable orbit above the
   atmosphere, and it will not deorbit a vessel on a grazing pass; release SAS before engaging.

## What goes wrong, and how it shows

| Telemetry | Meaning | Response |
|---|---|---|
| Apoapsis barely lower after a pass | periapsis too shallow | lower it at apoapsis (engine) and try again |
| `max_temp_fraction` climbing toward 1 | entry too hot or shield not forward | hold retrograde; next time shallower or better shielded |
| Angle of attack swinging, heading wandering | tumbling (long stack, CoM behind CoP) | little to do mid-entry; jettison what is behind the shield if possible |
| `part_lost` at chute opening | chutes opened too fast or too hot | ones left may still suffice; compute the new terminal speed |
| Descent speed under chutes far above plan | fewer or smaller chutes than needed, thin air | powered final burn if an engine remains; compute with `compute_descent` |

## Sanity checks (not targets)

- Kerbin atmosphere 70 km; kRPC density ≈ 1.14 kg/m³ at sea level. Duna 50 km, ≈ 0.133 kg/m³ at
  datum (about an eighth of Kerbin's; parachutes are far weaker). Eve 90 km, ≈ 6.2 kg/m³ (parachutes
  very effective, an ascent from the surface extremely expensive). Laythe 50 km, ≈ 0.74 kg/m³.
- Low Kerbin orbit ≈ 2.3 km/s; a Mun return arrives near 3.2–3.3 km/s at a 30 km Kerbin periapsis. A pod
  without a heat shield broke up at ~3 km/s.
