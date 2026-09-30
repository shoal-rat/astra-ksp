# EVA and surface operations

Keywords: eva, extravehicular, kerbal, crew, astronaut, flag, plant flag, walk, surface, board, hatch, crew transfer, roster, ladder, jetpack, hop, harmony, surface operations

EVA is the part of the mission where a kerbal leaves the vessel: to plant a flag, walk somewhere,
move between vessels, or come home through a different hatch. The vessel keeps its own state while
the kerbal is out, and a kerbal who cannot get back in is a crew loss in slow motion.

## 1. Before anyone goes outside

- **Situation.** A flag needs the kerbal standing on the ground: the vessel `landed` (or `splashed`).
  Check that the vessel has settled: speeds near zero, tilt small, legs grounded (`telemetry`).
- **The hatch must be free.** Parts over or right below the hatch (a heat shield directly under a Mk1
  pod, radial parachutes, panels, an RTG) make EVA fail with "no free hatch". A vessel that tipped
  over can bury its hatch. If EVA matters to the mission, test `crew_eva` on the pad before launch.
- **Who is aboard.** `crew_roster` and `crew_status` show who is where. After a launch, confirm the
  crew you asked for is actually seated.
- **Safe vessel.** Throttle 0, no reflex running, nothing that will stage. Take a `game_checkpoint`.
- **Will the lander stay up without its pilot?** A crewed pod gives no control once the pilot is
  out, so SAS stops. In low gravity a light, springy lander on a slope can be thrown over by the
  jolt KSP gives landed vessels when the active vessel changes (EVA, boarding, a reload), even if
  the kerbal never touches it. Designs that keep control with the crew out (a probe core with SAS),
  a wide low stance and flat ground reduce the risk; lift off soon after boarding.
- **Where will the kerbal land?** The kerbal leaves the hatch and drops. Anything wider than the pod
  below the hatch (a wider tank, panels, a heat-shield lip) becomes a shelf the kerbal lands on, and
  a kerbal standing or walking on a light lander can push it over. `crew_eva(kerbal, hop_clear_m=6)`
  flies the EVA jetpack straight out from the hatch before the kerbal touches anything, then drops
  it on the ground clear of the vessel (only where the pack can lift the kerbal: see section 2).

## 2. Moving around

- `crew_walk(kerbal, lat_deg=..., lon_deg=...)` or `crew_walk(kerbal, bearing_deg=..., distance_m=...)`
  walks the kerbal to a point and lets game time run until the walk ends: `walk.state` arrived,
  stalled (no progress: an obstacle or a slope too steep), or stopped with `timed_out`; `lost` means
  the kerbal is no longer on EVA in physics range. The game-time bound is `max_game_s`, by default
  30 s plus distance at 0.4 m/s: a walk of a few hundred metres runs minutes of game time, so check
  what else is running (power, a vessel in orbit you need to meet). `wait=false` only gives the order;
  the kerbal walks whenever time runs next. Confirm with `crew_status` rather than assuming.
- `crew_hop(kerbal, distance_m, rise_m, away_from=vessel)` (or `bearing_deg` instead of `away_from`)
  is a short EVA-jetpack flight at up to 1.5 m/s: the kerbal flies toward the point holding `rise_m`
  above its start altitude, then the pack is stowed and the kerbal drops and stands. Use it to get off
  a vessel or over an obstacle without walking on it. `rise_m` is absolute (not above the ground
  under the track): take the tallest obstacle on the track plus any rise of the ground along it, plus
  a margin. Game time runs until the kerbal stands (bounded by about 25 s + distance / 1.2 m/s + 2 s
  per metre of rise).
- A hop works only where the pack can lift the kerbal: the bridge refuses it when the pack's
  acceleration is below 1.3 × local gravity, which with the stock pack rules out Kerbin, Eve, Laythe,
  Tylo, Duna and Moho (read the surface gravity with `body_info`; the refusal gives both numbers).
  `crew_hop` also refuses in orbit, when the kerbal moves fast over the ground, and without a jetpack
  or EVA propellant. `crew_eva(..., hop_clear_m=...)` reports a hop it could not start as
  `hop_skipped`, and one the bridge refused or that failed as `hop_error`: the kerbal is outside
  anyway, standing where the hatch dropped it.
- Walking and hopping need the Harmony mod in KSP (the bridge drives the stock EVA controls through
  it); without it they fail with "unavailable" (bridge 503) and only EVA, boarding and flags work.
- To board, stand beside the vessel under the hatch: boarding reaches about 5 m, so a hatch a few
  metres up is reachable from the ground.
- Distances and bearings on a sphere of radius R (read R with `body_info`):
  - distance d = 2R·asin(√(sin²(Δφ/2) + cos φ1·cos φ2·sin²(Δλ/2)))
  - initial bearing θ = atan2(sin Δλ·cos φ2, cos φ1·sin φ2 − sin φ1·cos φ2·cos Δλ)
  - destination from (φ1, λ1) along θ for d: δ = d/R, φ2 = asin(sin φ1·cos δ + cos φ1·sin δ·cos θ),
    λ2 = λ1 + atan2(sin θ·sin δ·cos φ1, cos δ − sin φ1·sin φ2). `compute_calc` evaluates these.
- Stay within loading range of the vessel (a couple of kilometres); beyond it the vessel is unloaded
  and the walk back is long.
- Low gravity changes walking into bouncing. On very small bodies escape speed is tiny (Gilly ≈ 36 m/s,
  Minmus ≈ 243 m/s): careless jetpack use can leave the surface for good.

## 3. Flags

`crew_plant_flag(kerbal, name, plaque)` while the kerbal stands on the ground. Confirm with the result
and `crew_status`. Treat the flag as the goal only if the mission says so; never let it strand the
crew (walk back, board, then decide the next step).

## 4. Coming back in

- `crew_board(kerbal, part)` into a crewable part with a free seat, then verify with `crew_status`
  (the kerbal is aboard, not on EVA) and the vessel's crew list (`telemetry` full).
- Inside one vessel (including a docked stack), move crew with `crew_transfer(kerbal, to_part)`.
- Board everyone before any engine ignition, warp, or staging.

## What goes wrong, and how it shows

| Result / telemetry | Meaning | Response |
|---|---|---|
| `crew_eva` fails: no free hatch | hatch blocked by a part or by terrain | another crewed part's hatch, or none; note it for the next design |
| Flag refused | not standing on the ground, or vessel not landed | wait until landed and settled |
| Kerbal state shows flying/ragdoll on a slope | fell or slid | wait until it settles, then walk |
| `walk.state` stalled | an obstacle or a slope too steep on the straight line | walk around it in legs with other bearings, or hop over it where the pack can lift |
| `crew_walk` returns `timed_out` | `max_game_s` too short for the distance and pace | re-read the position, walk on with a bound from distance / measured pace |
| Hop refused: pack can neither lift nor glide | gravity too strong for the jetpack and the kerbal already on the ground | walk; to leave a lander in strong gravity, hop straight out of the hatch (`crew_eva` `hop_clear_m`): a pack that gives most of g glides clear from hatch height |
| Walk or hop "unavailable" (503) | Harmony not installed in KSP | tell the flight director; walk-free plans only (board from where the kerbal stands) |
| Board refused | no free seat or too far from the hatch | walk closer; check seats |
| Board refused with `boarded: false` in reach | KSP stopped at a carried-science dialog or an inventory that does not fit | look with `camera_look`; walking closer will not help |

## Sanity checks

- Crew counts before and after every EVA should match what you expect; journal them.
- If a kerbal has been walking far longer than distance / walking pace suggests, re-read their
  position; they may be stuck on terrain.
