# ASTRA — an LLM mission architect for Kerbal Space Program 1

> **One line of natural language in. A mission decomposed, designed, calculated, and flown live in the
> game out.** ASTRA does not pick from a menu of canned missions — an LLM reads the goal, breaks it into
> atomic flight primitives, and reasons out every parameter (launch window, altitude, capture mode,
> staging, return) from real body constants and a menu of calculation helpers.

[![KSP1](https://img.shields.io/badge/Kerbal%20Space%20Program-1.12.5-blue)](https://www.kerbalspaceprogram.com/)
[![Python 3.13](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![kRPC](https://img.shields.io/badge/telemetry-kRPC-1f8fff)](https://krpc.github.io/krpc/)
[![MechJeb2](https://img.shields.io/badge/autopilots-MechJeb2-b45309)](https://github.com/MuMech/MechJeb2)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

ASTRA is a **general** agent that flies Kerbal Space Program 1 **live**. You give it a goal in plain
English; an **LLM is the mission architect**. It decomposes the goal into an ordered list of atomic,
body-agnostic primitives, designs and PNG-reviews the rocket, reasons out the parameters for each step
from physics, flies them over a single live connection, retries when a step fails, and records what it
learns. Public repo: **[github.com/shoal-rat/astra-ksp](https://github.com/shoal-rat/astra-ksp)**.

```text
NL goal ─▶ LLM architect (catalog + bodies + live state + calc helpers) ─▶ primitive steps
        ─▶ physics-sized rocket ─▶ Codex three-view review ─▶ executor over kRPC + bridge ─▶ live KSP
```

The decomposition is done by an LLM that ASTRA calls **through a local CLI — Claude Code or Codex — over
the machine's own authenticated session, with no separate API key.** There is no heuristic/keyword
decomposer: the agent reads the destination and the whole flight plan from the text, reasoning like a
flight director, not matching phrases.

```text
$ PYTHONPATH=src python tools/astra.py "land a crew on Mars, plant a flag, and bring them home" --dry-run

[ASTRA] decompose: [llm] Duna: launch(crew=1, heatshield, chutes) -> transfer(target_body=Duna,
        capture_mode=circular) -> jettison_transfer_stage -> land() -> plant_flag() -> ascend()
        -> transfer(target_body=Kerbin, capture_mode=aerocapture) -> recover()
        rationale: "Mars" = Duna; split the upper into a droppable transfer stage + a short lander so the
        lander touches down upright and keeps its own get-home budget; aerocapture home behind the shield.
  RESULT: SUCCESS (dry run)
```

---

## How it works

The pipeline is five stages: **interpret → decompose (LLM) → design + review → execute → learn.** A
natural-language command becomes a planning context, the context goes to the LLM over the local CLI, the
LLM returns an ordered list of primitive steps with calculated args, the rocket is sized from those steps
and gated on a Codex-reviewed three-view drawing, and the executor flies them over one live kRPC + bridge
connection — retrying, diagnosing, and recording on the way.

![ASTRA architecture: NL goal to LLM architect over a local CLI to primitive steps to a physics-sized, Codex-reviewed rocket to the executor over kRPC and the bridge to live KSP](docs/astra-architecture.svg)

| Stage | Code | What it does |
| --- | --- | --- |
| **NL goal** | `tools/astra.py` | one line of plain English describes the whole mission |
| **LLM architect** | `astra/interpreter.py`, `astra/llm_cli.py`, `astra/planning_context.py` | builds a rich planning context and calls the LLM **over a local CLI (Claude Code / Codex)** to decompose AND calculate; strict-JSON out |
| **Primitive steps** | `astra/primitives.py` | 13+ atomic, body-agnostic primitives, each wrapping a proven flight driver |
| **Rocket design + review** | `design.py`, `tools/design_chart.py`, `astra/codex_review.py` | requirements → physics-sized stages (incl. the split transfer/lander and side-booster configs); three-view PNG; **mandatory Codex review** |
| **Executor** | `astra/agent.py` | connects once, threads a live context through every step, bounded retry, fail fast, diagnose |
| **C# bridge** | `csharp/KspAutomationBridge`, HTTP `127.0.0.1:48500` | MechJeb autopilots · EVA / personnel · game-data read-back |
| **kRPC** | RPC `127.0.0.1:50000` (stream `50001`) | live μ · radius · SOI · density · orbit state · nodes · warp |
| **KSP 1.12.5** | the live game | where everything actually flies |

Nothing in the planning layer hardcodes a body. The bodies catalog carries the real stock-KSP constants
(μ, radius, SOI, sidereal period, atmosphere, rotation, synchronous + low-orbit altitude) for every body
except the Sun, and the live universe state is read from kRPC at plan time, so **the same agent plans
correctly for Kerbin, the Mun, Duna ("Mars"), Eve, or any body in the catalog.**

---

## 1 · The LLM mission architect — over a local CLI, no API key

The interpreter (`astra/interpreter.py`) treats the LLM as a **flight director with real
orbital-mechanics comprehension**, not a phrase-matcher. It builds a planning context
(`astra/planning_context.py`) and hands it to the model — but it does **not** call a hosted API with a
secret key. Instead it shells out to a **locally-authenticated coding agent over its own port**:

- **`astra/llm_cli.py`** runs **Claude Code** (`claude -p …`) or **Codex** (`codex exec …`) as a
  subprocess on this machine. Both are already signed in for the human running the lab, so the
  decomposition rides that local session — **no `ANTHROPIC_API_KEY`, no separate billing.** The system
  prompt + the planning context go in on the prompt/STDIN, the model's strict-JSON plan comes back on
  stdout, and the same Windows-aware binary resolver the Codex three-view review uses finds the real
  executable. `ASTRA_LLM_CLI=claude|codex` chooses the backend (default: whichever is installed).

There is **no offline / heuristic decomposer.** The previous build shipped a keyword-driven
"general planner" that ran without any model; it has been **removed**. Decomposition is always an LLM
call, and if the local CLI is missing or its reply does not parse, `interpret()` raises
`LLMUnavailableError` rather than silently degrading. The non-LLM physics — sizing the rocket, the Δv
budget, the bodies math — stays in Python; only the *reasoning* (which steps, in what order, with what
intent) is the model's job.

The architect does three things, in flight-director order:

1. **Decompose** the goal into an ordered list of atomic primitive steps from the catalog. Flight order
   matters — launch → interplanetary transfer → on-body actions → transfer home → recover — and there is
   deliberate **leeway for novel multi-leg missions**: a Moho loop via an Eve gravity assist, a grand
   tour, a relay constellation, a refuel-depot pattern. For a crewed land-and-return on a *planet* it
   inserts a `jettison_transfer_stage` step after capture (see §3).
2. **Calculate** each step's parameters from the body constants and the calculation helpers — the target
   body + altitude (`synchronous`→ that body's `synchronous_alt_km`, `low orbit`→ `low_orbit_alt_km`),
   the capture mode (`circular` for a precise ring, `aerocapture` for a cheap shielded arrival, `loose`
   for a bound ellipse), the launch window (any Sun-to-Sun transfer calls
   `transfer_planner.find_transfer_window` then time-warps to the departure UT), and the launch profile
   (crew, heat shield, chutes, **side boosters** for a heavy upper).
3. **Annotate** each step with the short calculation it used, so the run report shows the reasoning.

Its strict-JSON reply — `{"target_body", "steps":[{"primitive","args","reasoning"}], "mission_rationale",
"open_questions"}` — is parsed and **validated against the catalog**: unknown primitives are dropped,
free-text notes are lifted out of the executable args, and hallucinated args are repaired away. The
launch step is then **physics-sized** from the decomposed plan's mission graph (mission Δv, or the split
transfer/lander budget) — that part is calculation, not decomposition, so it stays deterministic.

---

## 2 · The primitive catalog

`astra/primitives.py` is a registry of small, atomic, **body-agnostic** steps. Each does exactly one
thing for *any* body, reading the launch/target body from its args + `bodies.py` + live kRPC, never a
hardcoded `body="Mun"`. The design rule is **wrap, don't rewrite**: every primitive calls a *proven*
flight function and only parameterizes and sequences it. Each logs a `mission_phase` + `RESULT` marker
and returns a structured `PrimitiveResult`.

| Primitive | What it does | Wraps |
| --- | --- | --- |
| `launch` | design (+ Codex-review) and ascend a craft to a parking orbit | `deploy_relay.launch_to_lko` |
| `transfer` | transfer + capture at another body (`circular` / `loose` / `aerocapture`) | `deploy_relay_transfer.transfer_to_body` / `_transfer_to_mun` |
| `jettison_transfer_stage` | drop the spent transfer stage in orbit, keep the short lander (split round-trip) | `activate_next_stage` + orbit/crew/heat-shield re-select |
| `set_orbit` | Hohmann / circularize to a target orbit of the current body | `deploy_relay_transfer._hohmann_to_radius` |
| `land` | land on the current body — deorbit, then MechJeb descent (air) or hoverslam (airless) | `bridge.mj_land` / `_land_on_mun` |
| `ascend` | climb from the surface back to orbit of the current body | `bridge.mj_ascent` / `_launch_from_mun` |
| `plant_flag` | EVA a kerbal, plant the stock flag, board back (verifies landed + re-boarded) | `bridge.eva_flag` + `eva_board` + `eva_status` |
| `walk_to` | walk an EVA kerbal a calculated great-circle move to a lat/lon | `eva_control.walk_kerbal_to` |
| `rendezvous` · `dock` · `transfer_crew` | assemble / refuel in orbit | `bridge.mj_rendezvous` / `mj_dock` / `transfer_crew` |
| `recover` | jettison the service bus, then descend on the heat-shield capsule + chutes | `crewed_eve_roundtrip.descend_and_recover` |
| `commission_relay` | deploy antenna + solar, set the vessel type to Relay | `deploy_relay.commission` |

The executor runs the decomposed steps over the one live connection, in order, and **fails fast**: a
failed primitive surfaces its marker, gets a knowledge-base diagnosis, and aborts rather than hanging.

![ASTRA mission flow: launch, transfer, capture, jettison transfer stage, land, plant flag, ascend, return, recover — with bounded retry and fail-fast](docs/mission-flow.svg)

---

## 3 · Rocket design — split stages, side boosters, and a forced Codex review

A launch is **never** flown without a PNG-verified rocket shape. The `launch` primitive sizes the rocket
from the same `ShipRequirements` the ascent will fly (`design.py`), renders an orthographic **three-view**
chart (side / front / top), **rasterizes it to a real PNG**, runs the `looks_like_a_rocket` geometry
gate, and then hands the picture to **Codex (ChatGPT) for a mandatory independent review** that critiques
the *shape* with its own eyes (`astra/codex_review.py`) — protruding mass, staging order, booster height,
exposed bells, a wasp-waist that should be framed in a service bay. Codex is called over the same local
CLI, no API key. Only a design both gates accept is flown.

Two configurations exist beyond the simple single stack, chosen by the architect/sizer for the mission:

- **Split transfer + lander (crewed planetary round-trip).** The upper "mission" stage is split into a
  **droppable transfer stage** (does the ejection + capture, jettisoned in orbit by
  `jettison_transfer_stage`) and a **short, wide, low-CoG lander stage** (descent + ascent + return on
  its *own* budget). This fixes two things at once: the lander is short enough to land **upright** (so the
  EVA hatch clears for the flag and the ascent engine points up), and its get-home budget is
  **independent** of however much the variable capture overspent — the transfer stage absorbs that and
  is dropped.
- **Side boosters carrying tanks + engines (heavy lift).** Instead of stretching a heavy interplanetary
  craft into an un-launchable needle, the sizer can strap on **radial boosters that carry their own fuel
  tanks and engines** (asparagus-style: they feed the core, then drop first). This shortens the core,
  raises the lift-off thrust, and keeps the stack a clean, gate-passing rocket. The geometry gate accepts
  **symmetric** strap-ons within the ascent envelope; the writer renders them firing at T0 and separating
  cleanly before the core continues.

---

## 4 · Full game-data integration

The lab knows the real stock parts. `src/ksp_lab/parts.py` keeps a small hand-validated curated core
(masses, heights, Isp checked one-by-one against the live game) and **materializes the whole stock parts
tree** on top of it: `materialize_catalog()` walks the real KSP GameData `.cfg` folders once and writes
every rocket-relevant `PART{}` node into a committed `src/ksp_lab/data/stock_parts.json` — **400+ stock
parts** (every engine, SRB, tank, decoupler, adapter, nose, fairing, pod, RCS, reaction wheel, heat
shield, chute, leg). Where a materialized part shares a curated part's identity, the **curated value
wins**. The design sizer queries the full roster ("every 2.5 m engine, sorted by thrust") and picks
engines to meet each stage's Δv **and a thrust floor**, so a heavy upper is never stuck on a 60 kN
vacuum engine that would crawl the burn and time out.

---

## 5 · The C# bridge

`csharp/KspAutomationBridge` is a KSP plugin serving HTTP on `127.0.0.1:48500`, built with the in-box
.NET Framework C# compiler (`bash csharp/build.sh` runs `csc`, no msbuild). It exposes three groups:

- **MechJeb autopilots** — `/mj-ascent`, `/mj-execute-node`, `/mj-rendezvous`, `/mj-dock`, `/mj-land`,
  `/mj-plan`, `/mj-disable`, plus `/mj-status` and `/mj-stage-stats`.
- **EVA / personnel** — `/eva-flag` (plants the stock flag headlessly), `/eva-go`, `/eva-board`,
  `/eva-walk-to`, `/eva-status`, `/spawn-crew`, `/transfer-crew`, `/crew-list`.
- **Game-data read-back** — `/vessel-info`, `/parts-list`, `/resources`, so the architect reasons over
  the game's own numbers, not a guess.

The EVA layer is **calculated, not guessed**: `eva_control.py` expresses every surface move as a
great-circle bearing + distance on the body sphere (the exact haversine the C# bridge mirrors).

---

## Status (flown live in KSP 1.12.5)

Stated honestly — what is flown, and what is built but not yet validated end to end.

**Flown and verified:**

- **Crewed Mun land-and-return.** A kerbal launched, transferred to the Mun, landed, planted a flag,
  ascended, returned to Kerbin, and was **recovered alive** — the whole decomposed chain, autonomously.
- **Crew on the Duna ("Mars") surface.** The autonomous agent designed, Codex-reviewed, launched,
  transferred to Duna, captured, deorbited, and **landed a crew intact on the Martian surface** — proving
  the launch → transfer → capture → descent chain for a real interplanetary target.
- **3 synchronous Eve relays + a Duna comsat constellation** deployed on the body-agnostic transfer
  pipeline (precise Lambert window → encounter → capture → ring).

**Built, in progress on the live shakedown:**

- The **split transfer/lander + side-booster** round-trip is validated through launch → eject → Duna
  encounter; the remaining work is the transfer's **propellant efficiency** (the relay-tuned capture is
  expensive; the fix is a cheaper aerocapture / loose-capture arrival) so the lander lands upright and
  flies the full flag-and-return. The architecture is sound; the live capture economy is the open item.

---

## Setup

**Requirements**

- **KSP 1.12.5** open, with the **kRPC** mod server on `127.0.0.1:50000` (stream `50001`).
- **MechJeb2** installed, plus the `MechJebForAll.cfg` ModuleManager patch so every pod carries a
  `MechJebCore`.
- The project's C# **`KspAutomationBridge`** plugin on `http://127.0.0.1:48500` — build with
  `bash csharp/build.sh`, install the DLL into GameData, reload KSP.
- **Python 3.13** + the [`krpc`](https://pypi.org/project/krpc/) package. Paths come from
  `configs/local-ksp.yaml`.
- A **headless Chrome / Edge** for the three-view PNG gate.
- A local **Claude Code** or **Codex** CLI, signed in — ASTRA calls it for the decomposition (and the
  three-view review) over the machine's own session. **No `ANTHROPIC_API_KEY` is needed.**

**Run the agent:**

```bash
# Decomposition is done by the local LLM CLI (Claude Code / Codex) — no API key:
PYTHONPATH=src python tools/astra.py "put a relay in synchronous orbit around Duna"

PYTHONPATH=src python tools/astra.py "land a crew on Mars, plant a flag, and bring them home"
```

**Useful flags:** `--dry-run` (LLM decomposes and prints the plan; don't fly) ·
`--max-attempts N` (retries per step, default `2`) · `--config PATH` · `--from-step N` (resume a leg
against the live vessel). `ASTRA_LLM_CLI=claude|codex` picks the backend.

---

## Project layout

```text
ksp1-automation-lab/
├── tools/
│   ├── astra.py                   # the agent CLI — "one sentence in"
│   ├── design_chart.py            # three-view chart + looks_like_a_rocket gate (design_and_verify)
│   ├── render_chart_png.py        # rasterize a chart SVG -> PNG (headless Chrome) — the design proof
│   ├── deploy_relay.py            # proven Kerbin ascent (asparagus / side boosters) -> LKO  [wrapped by launch]
│   ├── deploy_relay_transfer.py   # precise window -> encounter -> capture -> ring      [wrapped by transfer]
│   └── crewed_eve_roundtrip.py    # crew capture / descend-and-recover helpers          [wrapped by transfer/recover]
├── src/ksp_lab/
│   ├── astra/
│   │   ├── interpreter.py         # NL -> LLM-decomposed mission plan (NO heuristic fallback)
│   │   ├── llm_cli.py             # call Claude Code / Codex over the local CLI (no API key)
│   │   ├── planning_context.py    # catalog + bodies + live state + calc-helper briefing for the LLM
│   │   ├── codex_review.py        # forced Codex three-view review of every flown design
│   │   ├── primitives.py          # 13+ atomic, body-agnostic primitives (each wraps a proven driver)
│   │   ├── agent.py               # the executor loop: connect once, run steps, retry, fail fast, record
│   │   └── knowledge.py · ledger.py   # per-step diagnosis + the append-only experience ledger
│   ├── bodies.py                  # body constants + synchronous-altitude (any body)
│   ├── transfer_planner.py        # precise interplanetary: Lambert porkchop window + asymptote ejection
│   ├── astro.py                   # closed-form physics core (vis-viva, Oberth, rocket eqn, hoverslam)
│   ├── design.py                  # requirements-driven ship designer (split stages, side boosters, thrust floors)
│   ├── parts.py                   # curated core + materialized stock catalog (data/stock_parts.json)
│   ├── craft_writer.py            # writes the .craft (stages, decouplers, side boosters, legs, fairings)
│   └── bridge_client.py · flight_controller.py · eva_control.py …
├── csharp/KspAutomationBridge/    # the C# plugin: /mj-* autopilots + EVA/personnel + game-data read-back
├── configs/                       # local-ksp.yaml and friends
├── docs/                          # astra-architecture.svg · mission-flow.svg · the engineering notebook
└── tests/
```

---

## The experience notebook

[`docs/USING_KRPC_AND_MECHJEB.md`](docs/USING_KRPC_AND_MECHJEB.md) records the hard-won lessons from
flying full missions live: the reference-frame rule that cost ~13 docking attempts, why a killed run that
leaves the craft in rails warp silently kills the next burn, why MechJeb's landing AP must be deorbited
into first, why the autostager must be disabled on in-space legs so it can't shed the crew pod, and the
kRPC-proxy `==`-not-`id()` rule that finally got a crew to orbit. The discipline throughout: **let the
LLM reason about the mission, calculate every number, delegate the closed-loop flying to MechJeb, and
write down what you learn.**

---

## Acknowledgements

- [**Kerbal Space Program**](https://www.kerbalspaceprogram.com/) — the simulator everything flies in.
- [**MechJeb2**](https://github.com/MuMech/MechJeb2) — the autopilots ASTRA delegates the closed-loop flying to.
- [**kRPC**](https://krpc.github.io/krpc/) — the RPC mod for live telemetry and the body / orbit constants every calculation reads.
- **Claude Code** and **Codex** — the local coding agents ASTRA calls to architect the mission and review the design.

---

*Licensed under the [MIT License](LICENSE).*
