# Changelog

## Unreleased

- **README rewrite:** the README is now in English, with a Chinese version in `README.zh-CN.md`.
  It adds a hero banner, a loop diagram, a video thumbnail, and a call-by-call excerpt of the Duna
  landing log. Every figure was re-checked against the flight logs. Two corrections: Duna took 377
  Kerbin days from launch to touchdown (329 was the planned transfer), and only the 0.86 and
  0.03 m/s Mun landings used the new predictor guidance. `scripts/showcase/readme_art.py` renders
  the artwork.
- **English showcase video (YouTube):** `make_video.py --lang en` builds the English cut from the
  same footage. It uses the narration in `storyboard.en.json` and adds English cards, HUD,
  thumbnail and chapters. Subtitles are timed word by word. The Chinese cut is unchanged, and
  `docs/video/YOUTUBE.md` has the upload notes.

## v2.0.0: the AI is the crew (2026-09-30)

A ground-up rewrite. The previous design had the AI write fixed mission programs. ASTRA 2 turns the
running game into tools, and the AI flies every mission itself as astronaut and mission control:
observe, compute, decide, act, verify. The repository contains no mission scripts.

### Flown in the game

- **Kerbin orbit and return:** the crew designed *ASTRA Orbiter 2* from the live part catalog,
  diagnosed a loss of control (nothing gimbaled), redesigned it, reached 80.3 × 79.6 km, and
  splashed down at 3.9 m/s.
- **Autonomous crew:** `astra mission` flew the same goal unattended with only the ASTRA tools and
  recovered Valentina.
- **Mun landing and return:** MechJeb ascent and trans-Mun injection, `fly_descent` landing, EVA,
  flag "ASTRA Mun Base", return and splashdown.
- **Duna (Mars) landing:** a Lambert window, two mid-course corrections, a 713 m/s capture with
  auto-staging, aerobraking, three parachutes and a powered touchdown at 1.22 m/s. EVA by jetpack hop,
  flag "ASTRA Duna Base".

Logs, journals and transcripts are in `docs/missions/`.

### New

- **74 MCP tools in 9 groups:** observe, compute, design, game, control, fly, autopilot, crew,
  journal. Tool reference: `docs/TOOLS.md`.
- **Stop-the-world deliberation:** the game pauses whenever a tool returns. Game time advances only
  inside reflexes (`fly_until`, `fly_burn`, `fly_warp`, `fly_descent`, MechJeb watches, crew
  actions), which run until the AI's triggers fire or a safety interlock trips.
- **Rocket design from the live part catalog:** part trees with real attach nodes, a staging and
  stage simulator, `.craft` output, and design checks for steering authority and power.
- **`fly_descent` powered landing:** a numerically integrated suicide-burn predictor with
  centrifugal relief, terrain look-ahead with peak memory, and height-first braking when retrograde
  thrust cannot hold the gate. The terminal phase hands over on speed and drift, hovers to cancel
  drift, and SAS settles the lander after touchdown. An offline closed-loop simulator and adversarial
  scenarios cover it.
- **MechJeb autopilot tools:** ascent, node executor, landing, rendezvous, docking, planner, status
  and abort. Every setting is explicit and watched with interlocks.
- **Crew tools:** EVA, walk-to (fixed steering), EVA jetpack hop (`crew_hop`,
  `crew_eva(hop_clear_m)`), flags verified through the bridge, and boarding. Where the pack cannot
  lift a kerbal off the ground (Duna gives about 0.94 g), a hop glides out from the hatch instead.
- **Filming:** `astra record` and `astra mission --record`. The bridge renders the cameras
  off-screen with asynchronous readback into ffmpeg (H.264) and records only while game time runs. It
  marks every tool call except the read-only instruments and logs telemetry per frame, and a camera director frames each phase. The
  showcase video builder is `scripts/showcase/`.
- **KspAutomationBridge 2.1.0:**
  - scene and save control from the main menu, and a full part database with geometry
  - EVA walking, hops, flags and boarding; MechJeb; the CAPCOM panel
  - pause without the ESC menu, screenshots, and the video recorder
- **Autonomous runner:** `astra mission "<goal>"` uses the Claude Agent SDK with only the ASTRA
  tools, the `booster-engineer` and `fido` subagents, `--resume` and `--record`.
- **Knowledge:** the crew doctrine, 11 physics playbooks, and a lessons file that the crew searches
  and extends in flight.

### Fixed during live flights

- Attitude read from the surface frame, `Resources.density` called statically, the thrust-axis
  sign, late burns refused after `max_late_s`, and the throttle settled before pausing.
- The impact interlock is chute-aware, and `game_restore` returns to the checkpoint's scene.
- A broken part bounding box (the Mk1 pod reports ±6×10¹⁷ m) made the descent hover at 13 km.
  Vessel boxes are now built from sane per-part boxes.
- The overheat interlock compared skin temperature against the internal limit.
- kRPC's vessel list omits EVA kerbals and flags, so EVA and flag checks now use the bridge.
- Rails warp is allowed on the ground. Checkpoints and restores let physics settle around vessel
  switches and loads.

### Requirements

KSP 1.12.5, kRPC 0.5.4, MechJeb2, ModuleManager, Harmony, Python 3.11+. Optional extras: `agent`
(Claude Agent SDK) and `media` (imageio-ffmpeg for recording).
