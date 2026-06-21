# Artemis Precision Design Notes

This document is the calculation-first mission notebook for the live KSP1 Artemis replication loop. It is separate from the chronological engineering notebook so constants, formulas, trigger rules, and asset decisions remain easy to find.

## Objective

Replicate the Artemis lunar landing architecture in KSP1:

1. Launch a lunar relay satellite.
2. Predeploy a Starship/HLS-like lander to Mun orbit.
3. Launch an SLS/Orion-like crew vehicle.
4. Transfer crew to HLS, land, perform science, return to Orion.
5. Return Orion safely to Kerbin.

The live stock-KSP implementation uses Kerbin/Mun physics, but the architecture follows NASA Artemis roles: SLS launches Orion, Orion carries and returns crew, and HLS transports crew from lunar orbit to the surface and back.

## Current Sources

- NASA Human Landing Systems: HLS carries crews from lunar orbit to the surface, supports science, and returns them to orbit. https://www.nasa.gov/reference/human-landing-systems/
- NASA SLS Block 1B: SLS launches Orion and cargo, then performs translunar injection; Block 1B increases lunar payload capability. https://www.nasa.gov/reference/sls-space-launch-system-block-1b/
- NASA SLS overview: official SLS reference material for initial Block 1 configuration and Artemis launch roles. https://www.nasa.gov/humans-in-space/space-launch-system/
- NASA Orion spacecraft components: Orion crew module, service module propulsion, RCS, life support, solar arrays, and return role. https://www.nasa.gov/reference/spacecraft-components/
- NASA Artemis III current page: current HLS test framing and rendezvous/docking emphasis. https://www.nasa.gov/mission/artemis-iii/

## Current Craft Assets

- Primary HLS/Starship analogue: `work/downloads/mega/MUNSHIP.craft`.
  - Source key in code: `matt-lowne-munship-public-video-link`.
  - This is the preferred live HLS asset because it is a complete KSP-authored Moonship/Starship-style craft and has already loaded through the bridge.
- SLS/Orion analogue: `C:/Program Files (x86)/Steam/steamapps/common/Kerbal Space Program/Ships/VAB/Space Launch System Block 1.craft`.
  - Source: Artemis Construction Kit craft installed in the KSP VAB folder.
- SLS cargo relay analogue: `C:/Program Files (x86)/Steam/steamapps/common/Kerbal Space Program/Ships/VAB/Space Launch System Block 1B Cargo.craft`.
  - Source: Artemis Construction Kit craft installed in the KSP VAB folder.
- HLS fallback only: `Kerbal Landing System.craft`.
  - This is lander-like, but not a full Starship/Super Heavy predeployment stack.

Do not use the stock default PT Munsplorer template for the Artemis HLS or Orion phases. It is only useful for basic controller debugging.

## Stock KSP Constants From Live kRPC

| Body | mu, m^3/s^2 | radius, m | SOI, m | rotation/orbit period, s |
|---|---:|---:|---:|---:|
| Kerbin | 3,531,599,999,999.9995 | 600,000 | 84,159,286.48 | 21,549.425 |
| Mun | 65,138,397,520.78069 | 200,000 | 2,429,559.12 | 138,984.377 |

Mun stationary orbit calculation:

```text
r_sync = (mu_mun * T_mun^2 / (4*pi^2))^(1/3)
h_sync = r_sync - R_mun
```

Live value:

```text
r_sync = 3,170,563.34 m
h_sync = 2,970,563.34 m
Mun SOI = 2,429,559.12 m
```

Conclusion: a true Mun-stationary relay orbit is outside the Mun SOI in stock KSP. The stock mission must use a stable high/elliptical relay orbit or a relay constellation. RSS/RO is the separate branch if real Earth/Moon geostationary-style analysis is required.

## Baseline Stock-KSP Energy Numbers

Using an 80 km Kerbin parking orbit:

| Quantity | Value |
|---|---:|
| Kerbin 80 km circular speed | 2,278.93 m/s |
| Ideal Hohmann TMI from 80 km to Mun radius | 856.36 m/s |
| Mun orbital speed around Kerbin | 542.49 m/s |
| Transfer speed at Mun distance | 177.67 m/s |
| Rough Mun-relative v-infinity | 364.83 m/s |
| Capture at 60 km periapsis to 60 x 300 km Mun orbit | 222.19 m/s |
| Mun 60 km circular speed | 500.53 m/s |
| Kerbin specific potential gain from surface to 80 km | 692,470.59 J/kg |
| Mun specific potential gain from surface to 60 km | 75,159.69 J/kg |

These are ideal two-body values. Live control must budget extra delta-v for gravity losses, steering losses, finite-burn timing, atmospheric drag, off-axis thrust, and low-throttle trimming.

## Core Equations

Vis-viva:

```text
v = sqrt(mu * (2/r - 1/a))
```

Specific orbital energy:

```text
epsilon = v^2/2 - mu/r
```

Specific potential energy change between radii:

```text
DeltaU/kg = mu * (1/r1 - 1/r2)
```

Finite-burn acceleration estimate:

```text
a = T / m
burn_time ~= delta_v / a
lead_time = burn_time/2 + settle_time + command_delay
```

Suicide-burn distance lower bound:

```text
d = v_vertical^2 / (2 * (T/m - g_local))
trigger_altitude = d + speed * command_delay + margin
```

The controller should use these equations to set trigger points. It should not wait passively for arbitrary clock time when orbital geometry, remaining delta-v, altitude, velocity, or time-to-apsis are available.

## Trigger Rules

- Launch ascent: no physics warp during powered atmosphere. Pitch schedule is allowed, but stage only on measured low thrust or empty active engines.
- Circularization: burn near apoapsis using finite-burn lead time from current mass and available thrust.
- TMI: search future nodes from a calculation seed, then execute from remaining node vector. Stop only when live patched conics show a capture-grade Mun periapsis or when a safety cap is reached.
- Mun capture: warp to a computed periapsis lead, force rails and physics warp to zero, align, then verify actual thrust within the first seconds.
- Relay: in stock KSP, accept a stable captured relay orbit while the crewed mission is being debugged. High-relay shaping requires validated attitude authority.
- Landing: compute suicide-burn trigger from local gravity, vertical speed, mass, and thrust. The theoretical perfect landing still requires deceleration before touchdown; "perfect moment" means starting the required braking burn as late as possible while retaining a margin.
- Surface science: after stable touchdown, run available science modules; if craft lacks modules, record modeled crewed surface-sample science and note the limitation.
- Return: launch HLS back to Mun orbit, rendezvous-equivalent transfer to Orion, then use Orion service propulsion for Kerbin return and crew capsule recovery.

## Current Implementation Decisions

- Use the downloaded Moonship craft as the primary Starship/HLS analogue.
- Use Artemis Construction Kit SLS Block 1 and SLS Block 1B Cargo craft for Orion and relay launches.
- Keep RSS/RO/RP-1 as a future high-fidelity branch. Installing it into this active stock run would invalidate stock craft, saves, and controller constants.
- Use stock-KSP high relay orbit or relay constellation instead of impossible Mun-stationary orbit.
- Record every failed trial in `docs/artemis_mun_engineering_notebook.md` and keep reusable rules here.

## Do Not Repeat

- Do not launch a craft with no controllable command module. Headless bridge launches via `EditorLogic.launchVessel()` do not run the crew-assignment dialog, so a craft whose only command part is an empty crewed pod has no control source and KSP blocks the launch with a "no available crew or probe core" prompt. This manifests as either a flight-scene timeout or an `Object reference not set` from `launchVessel()`. Every generated/seeded craft must carry a probe-core control source with electric charge; uncrewed satellites (relay) are probe-controlled by design. (player-confirmed 2026-06-21)
- Do not accept a relay "geostationary" requirement literally in stock KSP; the required altitude is outside Mun SOI.
- Do not treat default KSP templates as final Artemis assets.
- Do not trust a maneuver marker if the active craft has poor attitude response; verify actual orbital energy movement after ignition.
- Do not start a burn until physics warp is zero, attitude is aligned or bounded, and actual thrust can be observed.
- Do not treat a throttle-zero command as proof of coasting. SLS-style craft may still have unthrottleable thrust; measure actual thrust and hold a retrograde guard or abort before escape.
- Do not let the live-control craft diverge from the design model. Copied real-looking craft are valuable references, but precision relay and capture tests need throttleable stages whose thrust, mass, and staging match the generated design.
- Do not use raw orbital-frame vector guesses for Mun capture. Capture is an energy-removal burn and must use the validated orbital-retrograde steering path plus finite-burn lead from the capture estimate.
- Do not declare the project complete without relay, HLS predeploy, Orion/SLS crew launch, surface landing, science, ascent/return-to-Orion, and safe Kerbin recovery.
