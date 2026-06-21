# ASTRA Diagrams

Self-contained SVG diagrams for the **ASTRA — Autonomous Spaceflight Trial & Research Agent** README. All four are hand-authored, dark-space themed, and render standalone in any browser.

| Diagram | Caption |
| --- | --- |
| [`architecture.svg`](architecture.svg) | System architecture — natural-language command flows through the interpreter, mission spec, architect, and live kRPC flight controller, with the experience ledger, methodology KB, and guidance math feeding a diagnose-and-retry loop. |
| [`agent_loop.svg`](agent_loop.svg) | The autonomous loop — interpret → research → design → fly → diagnose → record, recording experience every attempt and fixing one root cause per retry until the goal is reached. |
| [`flight_state_machine.svg`](flight_state_machine.svg) | Live flight phases from ascent to parachute recovery, each with its success gate; the Falcon-9 hoverslam suicide burn is the highlighted critical phase. |
| [`hoverslam.svg`](hoverslam.svg) | Hoverslam descent profile — speed-vs-altitude chart showing freefall (engine off), the ignition point where actual speed meets the reference curve, the full-throttle brake riding the curve down, and touchdown at 0 m/s. |
