---
name: mission-planning
description: Map one line of natural language to a target body, an ordered capability list, a Δv budget, and crew/return requirements — the front door of every ASTRA mission.
---

# Mission Planning (NL → plan)

Turn the user's one line into a concrete plan before any flight. Primitive:
`Interpreter.interpret(command) -> MissionPlan(target_body, capabilities, mission, source, rationale)`
in `src/ksp_lab/astra/interpreter.py`. `ANTHROPIC_API_KEY` is REQUIRED: Claude maps the goal to the
EXACT capability set. There is no offline/heuristic fallback — without a key, or on a failed/garbage
Claude response, `interpret()` raises `LLMUnavailableError`.

## METHOD

1. **Target body.** Read it live; never from memory (methodology §6). Default `Mun`. The whole
   pipeline is currently Mun-tuned (relay band, capture gates). Re-read μ, radius, SOI, surface g,
   sidereal period, atmosphere from kRPC if the body changes.
2. **Capabilities (ordered).** Pick from EXACTLY `{relay, hls_land_return, crew_return}` and order
   them in flight order: `relay` → `hls_land_return` → `crew_return`. Only include what the goal
   needs. Keyword map:
   - relay ← "relay, comsat, satellite, signal, comm"
   - hls_land_return ← "land, lander, hls, surface, touchdown, descent"
   - crew_return ← "crew, astronaut, orion, return, bring, home, recover, round trip"
   - all three ← "artemis, everything, full mission, whole"
   - none matched → default `["relay"]` (safest proven capability).
3. **Crew / return requirement.** "crew/astronaut/home/recover" ⇒ `crew_return` AND the return craft
   needs reentry hardware. Gate hardware on the RETURN requirement, NOT on crew (methodology P14): the
   Orion flies `crewed=False` to launch headless but still gets `heatshield=True`.
4. **Δv budget.** Seed from the body's depth in the gravity well + transfer energy. KSP Mun round
   trip ≈ 5.5 km/s; size craft Δv well above the two-body ideal to cover gravity/steering/finite-burn/
   drag losses. The proven relay design carries ≈ 9.3–10 km/s, est launch TWR ≈ 1.6.
5. **Emit a MissionPlan** and hand each capability to the orchestrator in order.

## BODY CONSTANTS (read live; these are the live KSP values)

| body   | μ (m³/s²)   | radius   | SOI         | surface g  | atmosphere | sidereal     |
|--------|-------------|----------|-------------|------------|------------|--------------|
| Kerbin | 3.5316e12   | 600 km   | 84,159 km   | 9.81 m/s²  | top 70 km  | 21,549 s     |
| Mun    | 6.5138e10   | 200 km   | 2,429,559 m | 1.63 m/s²  | none       | period 138,984 s |

Note the unreachable-geometry check: a Mun-stationary orbit needs sync radius 2.97 Mm > SOI 2.43 Mm,
so it is physically impossible — substitute a high elliptical relay orbit (P10/methodology §5). Detect
such infeasibilities and substitute, don't chase them.

## WORKED EXAMPLE

Command: `"Artemis Mun SLS Orion Starship HLS relay science return"` (the canonical full mission).
- target_body = `Mun`.
- "artemis" ⇒ full ⇒ capabilities = `["relay", "hls_land_return", "crew_return"]` in that order.
- crew/return ⇒ Orion gets `heatshield=True`, flies `crewed=False`.
- Δv budget: relay ≈ 9.3–10 km/s (covers Kerbin→Mun TMI ~860 m/s + capture + relay shaping with
  margin). HLS carries enough for capture + hoverslam descent (~Mun g·time) + Mun ascent ~580 m/s.
- Result: dispatch `relay` first, then `hls_land_return` (predeploy + sortie), then `crew_return`.

Command: `"just throw a comsat around the Mun"` → `["relay"]`, no crew, no heat shield.

## SUCCESS / FAILURE MARKERS

- Plan is valid when `capabilities` is non-empty and every entry is in the known set
  (`KNOWN_CAPABILITIES`). If the model returns no usable steps, `interpret()` raises
  `LLMUnavailableError` — there is no heuristic fallback to catch it.
- A plan that omits `crew_return` for a "bring them home" goal is a planning error; re-read the
  keywords.
