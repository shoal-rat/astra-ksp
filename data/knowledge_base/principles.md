# KSP1 Automation Principles

## Mission decomposition

- Convert user goals into concrete phases: design, craft file generation, launch, flight, recovery, scoring, and iteration.
- Keep design generation outside the KSP plugin. The bridge only performs scene operations and reports state.
- Store every trial before launch so crashes and failed trials leave recoverable evidence.

## Craft files

- VAB craft files belong in `<KSP root>/saves/<save name>/Ships/VAB`.
- Treat `.craft` as serialized hierarchical part data. The root part links to children; stack attachments use `attN`; part IDs must be unique.
- Restrict craft names and paths. Never allow absolute paths outside the selected save's `Ships/VAB` folder for bridge loading.
- Prefer generated stock-part stacks first. Public craft files are useful as references, but copy only when the license allows it.

## Design heuristics

- Low Kerbin orbit typically needs a high-margin launch budget; the lab starts with 4500 m/s as a conservative stock sandbox target.
- Crewed Mun landing and return is treated as a 7500 m/s target in early optimization so the loop has room for inefficient guidance.
- Launch TWR below about 1.1 is penalized because the rocket may not clear the pad efficiently.
- Cost and part count are scored after mission viability, not before it.

## Flight control

- kRPC is the live control channel for vessel steering, staging, telemetry, and maneuver execution.
- The initial ascent controller uses a gradual eastward gravity turn, targets the requested apoapsis, then performs a circularization burn.
- Mun transfer, landing, and return are scaffolded as mission phases; extend `KrpcFlightController` with dedicated phase controllers before expecting reliable live Mun completion.

## Iteration

- A failed trial should produce a specific failure reason such as low TWR, under delta-v, no orbit, destroyed vessel, or incomplete return.
- Optimizer changes should be small and explainable: add propellant for under delta-v, upgrade the first-stage engine for low TWR, trim tanks after high-margin successes.
- External AI providers should return structured `RocketDesign` JSON only. The lab owns validation, file writes, bridge commands, telemetry, scoring, and storage.

