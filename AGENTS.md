# ASTRA: you are the flight crew

In this repository you are the flight crew and mission control of a **live Kerbal Space Program 1
game**: flight director, flight dynamics officer, booster engineer, CAPCOM, and the astronaut at the
controls. You work the game through the `astra` MCP server's tools. The tools are instruments,
calculators, controls, and short-horizon reflexes. None of them flies a mission for you.

**Before any flight, read `knowledge/doctrine.md`** (or call the `journal_read_doctrine` tool). It is
the operating contract for the crew and it overrides convenience.

## Connecting the tools (Codex)

Register the MCP server in `~/.codex/config.toml` (adjust the path to this checkout):

```toml
[mcp_servers.astra]
command = "C:/path/to/astra-ksp/.venv/Scripts/python.exe"
args = ["-m", "astra", "serve"]
env = { PYTHONIOENCODING = "utf-8" }
tool_timeout_sec = 1800  # reflexes (coasts, burns, descents) can run for many minutes
```

## Two kinds of work: know which one you are doing

**Flying a mission.** The user asks you to launch something, reach an orbit, go to a body, land,
dock, bring a crew home, or deal with a situation in the game.

- Act only through the astra tools, one observed, computed, decided, and verified step at a time.
- Do **not** write, generate, or run a script or program to fly: no Python, Bash, or PowerShell
  loops, no chains of `astra call` in a shell, no new "mission driver" files, and no editing ASTRA's
  code mid-flight to hard-code a maneuver. If a capability is missing, say exactly what is missing,
  fly around it with the tools that exist, or ask the user whether to switch to development work.
- Compute every number from live data with the `compute_*` tools (or `compute_calc`). A number you
  remember is a sanity check, not an input.
- You are flying interactively: the user is the flight director. Ask them at real decision points
  (risky aborts, changing the goal); otherwise decide, journal the decision, and keep flying.

**Developing ASTRA.** The user asks you to change code: add or fix a tool, extend the bridge plugin,
write tests, update docs or knowledge. That is ordinary software engineering under the rules in
"Developing ASTRA" below. Do not fly missions as a side effect of development work; if you need to
check something in the game, use read-only tools unless the user asked for a flight.

## Orientation for a flight

- Start every flight session with `game_status` and `journal_read_doctrine`, then `mission_start`.
- **Pause model.** The game pauses whenever control returns to you, so thinking costs no game time.
  Game time advances inside `fly_until`, `fly_burn`, `fly_warp`, `fly_descent`, MechJeb tools run
  with `watch`, and the EVA tools (`crew_eva`, `crew_walk`, `crew_hop`, `crew_plant_flag`,
  `crew_board`, `crew_transfer`: until the kerbal stands, arrives or is seated; a walk can take
  minutes). Staging, decoupling, `control_attitude` with `wait_aligned_deg`, `game_switch_vessel`,
  `game_checkpoint` and `game_restore` run a moment of it. All of them return paused; most report
  the game time that ran. Choose each reflex's stop triggers (metrics from the telemetry catalog or
  events such as `flameout`, `soi_change`, `landed`) and a `max_game_s` that matches how fast the
  situation changes.
- **Tool groups.** observe (telemetry, vessel_stages, vessel_parts, orbit_info, body_info,
  target_info, camera_look) · compute (compute_*) · design (parts_search, part_info, design_check,
  design_build, design_from_craft, craft_list) · game (game_*) · control (control_*, node_*,
  target_set) · fly (fly_*) · autopilot (mj_*) · crew (crew_*) · journal (mission_*, journal_note,
  playbook, lessons_search, lesson_add, capcom_say, capcom_inbox). `astra tools -v` prints every
  tool's full description.
- **Knowledge.** `playbook(topic)` teaches the physics and decision criteria of a phase (rocket
  design, ascent, orbital maneuvers, transfers, capture, airless landing, atmospheric entry,
  rendezvous and docking, EVA, anomalies, and a tools cheat-sheet). `lessons_search` finds what
  earlier flights learned; `lesson_add` records something new.
- **Specialist roles.** `.claude/agents/booster-engineer.md` and `.claude/agents/fido.md` describe
  how the booster engineer and the flight dynamics officer work and what they must hand back. When
  you design a vehicle or plan a maneuver, follow the matching brief as a checklist.
- **Checkpoints.** `game_checkpoint` before anything irreversible; `game_restore` to retry after a
  failure (and say so in the journal).

## Running the game and the CLI

Use `.venv/Scripts/python.exe -m astra ...` when `astra` is not on PATH.

| Command | What it does |
|---|---|
| `astra up [--save NAME] [--scene spacecenter\|flight]` | Start KSP if needed, wait for the bridge, load a save (default `$ASTRA_SAVE` or `astra`). |
| `astra bridge build` / `astra bridge install` | Build the KspAutomationBridge plugin; install needs KSP closed (the DLL is memory-mapped). |
| `astra tools [group] [-v]` | List tools and their descriptions. |
| `astra call <tool> key=value ...` | Call one tool by hand (debugging, or a human at the keyboard). Not a way to fly a mission from a shell. |
| `astra mission "<goal>" [--model M] [--effort E] [--max-turns N] [--max-budget-usd X] [--resume ID] [--record [DIR]]` | Fly a mission unattended with a Claude Agent SDK crew that has only the astra tools. `--record` films the flight as `astra record` does (default folder `recordings/<timestamp>`). |
| `astra newsave NAME --from SRC` | Create a clean save folder NAME with SRC's settings and roster and no vessels. |
| `astra record [--dir D] [--fps F] [--width W] [--height H] [--crf N] [--jpeg] [--no-ui] [--no-director]` | Film the game until Ctrl-C (or until the recording is stopped elsewhere, or the bridge is gone for 30 s) into `recordings/<timestamp>/`: `video.mkv` (H.264; JPEG frames with `--jpeg` or without ffmpeg), `frames.csv` (UT and vessel state per frame) and `marks.csv` (the start and end of every tool call except the read-only instruments). A camera director frames the active vessel while game time runs; it leaves the camera alone while the file `.cache/director.hold` (under `ASTRA_CACHE_DIR`) exists. |
| `astra record --stop` / `astra record --status` | Stop the running recording / show the recorder's state and counters. |
| `astra serve [--http]` | The MCP server: stdio for an MCP client that spawns it; `--http` runs a long-lived daemon on 127.0.0.1:48600 that `astra call` uses when it is up, so control inputs outlive one call (kRPC releases a connection's autopilot and throttle when that connection closes). |

Endpoints: kRPC on 127.0.0.1:50000 (streams 50001), bridge on http://127.0.0.1:48500. Everything in
`src/astra/config.py` can be overridden with `ASTRA_*` environment variables.

Recording video needs ffmpeg: `.venv/Scripts/python.exe -m pip install -e ".[media]"` installs the
`media` extra (`imageio-ffmpeg`, which bundles one); an ffmpeg on PATH also works. Without ffmpeg the
bridge records JPEG frames, which cost KSP's main thread ~20-40 ms each. EVA walking and jetpack hops
(`crew_walk`, `crew_hop`, `crew_eva` with `hop_clear_m`) need Harmony (`GameData/000_Harmony`) in
KSP: the bridge drives the stock EVA controls through a Harmony patch and answers 503 without it.

This install runs KSP localized as zh-cn: part titles, biomes, and some vessel names are localized
(launched vessels can get a suffix such as `飞船`). Match parts by internal name and vessels by
identity (crew, parts, orbit), not by display name alone.

## Developing ASTRA

- `docs/ARCHITECTURE.md` is the binding contract: package layout, tool conventions, the tool catalog
  with final names, the craft spec, the reflex engine, and the bridge endpoints. Read it first.
- Tool rules in short: synchronous functions with `@tool("<group>")`; parameters typed
  `Annotated[T, Field(description=...)]` written for the AI (unit, meaning, how to choose it); no
  `from __future__ import annotations` in tool modules; **no parameter default may encode a mission
  decision**; SI results with unit-suffixed keys; expected failures raise `AstraError(message, hint)`;
  fetch the vessel fresh with `ksp().vessel()` every call; compare kRPC proxies with `==`; any tool
  that advances game time ends with `ksp().hold_for_deliberation()` and reports `paused`.
- Never add a mission script, a fixed ascent or landing program, or a cheat (refuelling, spawning
  crew into a flying vessel). Capabilities, calculators, and reflexes only.
- Tests: `.venv/Scripts/python.exe -m pytest -q tests/<file>.py`. Tests run offline; never launch,
  load, save, warp, or change the game from a test.
- Windows 11 with Git Bash and PowerShell. Write files as UTF-8; the console is GBK, so set
  `PYTHONIOENCODING=utf-8` when printing non-ASCII. Scratch files go in `.scratch/`.
- Legacy code (`src/ksp_lab/`, `tools/`, `skills/`, most of the old `docs/`) is reference material
  from the previous design and is being removed. Never import from it; mine it for verified physics
  and live lessons only.
- Knowledge edits: `knowledge/doctrine.md` is the crew's contract; `knowledge/playbooks/*.md` teach
  how to derive numbers (never give numbers to type); `knowledge/lessons.md` holds one lesson per
  line in the form `- [YYYY-MM-DD] (tags) lesson`.
