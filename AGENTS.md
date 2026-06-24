# Agent operating rules — KSP automation lab

This file is the authoritative protocol for any agent (Claude Code, ASTRA, a subagent) working in this
repo. It overrides convenience. The lab's whole premise is **People → Claude → calculated APIs →
MechJeb2/kRPC → ships**: every number a ship flies on is *derived*, never guessed and never pre-set.

## RULE 1 — Designing a new rocket is a three-step gate (MANDATORY, in order)

Whenever you design a **new rocket** (a new `ShipRequirements` → `design_ship`, a new craft, a new
mission profile), you MUST do all three of these BEFORE it is allowed to launch. No exceptions.

1. **Get REAL data and size first.** Size every stage from the real stock-part data in
   `src/ksp_lab/parts.py` (masses, Isp, thrust, diameters) and the measured body catalogue in
   `src/ksp_lab/bodies.py` — never from a hand-typed mass or a flat Δv ladder. When the game is running,
   **close the loop against the live API**: after the craft is loaded, call
   `tools/design_chart.py:verify_against_live(conn, design)` to read the REAL assembled length /
   diameter / mass / part-count back from kRPC and confirm they match the calculated values. If a part's
   real size/mass is unknown, query it — do not assume.

2. **Generate a design chart and confirm it LOOKS LIKE A ROCKET.** Run
   `python tools/design_chart.py` (or call `design_chart.looks_like_a_rocket(design)` +
   `render_svg(design)`). This writes `docs/design_chart_<name>.svg` (a side-view you can eyeball) and
   **hard-gates the proportions**: slender (6 ≤ fineness ≤ 28), monotonic taper (widest at the base),
   a pointed nose (fairing ogive or cone on top), engine at the base, statically stable (CoP ≤ CoG).
   If `looks_like_a_rocket(design)["looks_like_a_rocket"]` is `False`, the design is **REJECTED** — fix
   the shape (it is a pancake, a noodle, top-heavy, or blunt), do not launch it.

3. **Calculate everything from that data.** All of Δv (Tsiolkovsky), TWR, staging / post-separation
   masses, structural coefficient ε and the single-stage Δv ceiling, aerodynamics (Cd, β, ascent drag
   loss, max-Q), the separation sequence and separator placement, and the feasibility verdict flow from
   `astro.py` / `design.py` over the real part + body data. The feasibility gate
   (`design.design_ship` → `RocketDesign.feasible`) must pass: liftoff TWR ≥ 1.2, total Δv ≥ required +
   5 % reserve, every stage meets its Δv + TWR. A failing gate means **do not fly**.

## RULE 2 — No cheating

- **No in-flight refuelling.** `execute.refuel()` is a deliberate no-op and stays that way. Each stage
  reaches its target on its **own** rocket-equation propellant. Electric charge comes from real solar
  panels (commission deploys them), never an EC top-off. Do not re-add a refuel/recharge bridge call.
- **No hardcoded craft designs.** Do not re-introduce hand-built `StageSpec` presets or named-template
  fuel patches. The calculated designer (`design_ship`) is the single source of a craft.

## RULE 3 — Honest reporting & autonomy

- Report outcomes at their true severity — if a launch fails, say so with the telemetry. Never overstate
  ("deployed") what was not verified, never understate a real concern.
- This is a game: be bold, iterate, crew loss is acceptable. Do routine things (KSP restarts, craft
  cleanup, long monitors) yourself — run long drivers with a background task, not a foreground `nohup`.
- Infra gotchas that have cost hours: check Python with `-Name python,python3,python3.13` (WindowsApps
  python is missed otherwise); a launch that "dies" is usually **premature staging** (a tank-crossfeed
  transient reads the engine dry) — the launcher's consecutive-dry guard, not a single poll, decides a
  drop. Write files `encoding="utf-8"` (the console is GBK and chokes on `Ø`/`✓`).

## Where the truth lives

`astro.py` (orbital mechanics + aero + mission), `design.py` (sizing, feasibility, staging),
`parts.py` / `bodies.py` (real catalogues), `craft_writer.py` (assembly + procedural fairing),
`tools/design_chart.py` (the RULE-1 chart + live verify), `tools/deploy_relay.py` (the calculated
comsat launcher), `docs/CONSTELLATION_DESIGN.md` (the network the comsats build).
