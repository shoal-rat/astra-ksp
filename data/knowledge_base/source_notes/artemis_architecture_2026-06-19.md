# Artemis Architecture Source Notes

Date checked: 2026-06-19

Sources:

- NASA Artemis overview: https://www.nasa.gov/humans-in-space/artemis/
- NASA Artemis III overview: https://www.nasa.gov/mission/artemis-iii/
- NASA Space Launch System overview: https://www.nasa.gov/humans-in-space/space-launch-system/
- NASA Orion reference guide: https://www.nasa.gov/reference/orion-spacecraft/
- NASA Human Landing Systems reference: https://www.nasa.gov/reference/human-landing-systems-2/
- NASA/SpaceX HLS mission sequence: https://www.nasa.gov/directorates/esdmd/artemis-campaign-development-division/human-landing-system-program/nasa-spacex-illustrate-key-moments-of-artemis-lunar-lander-mission/
- NASA Artemis II nominal free-return trajectory: https://svs.gsfc.nasa.gov/5610/
- NASA Artemis II 2026 flight-derived trajectory: https://svs.gsfc.nasa.gov/5632/
- NASA Artemis II flight path animations: https://svs.gsfc.nasa.gov/20412/
- NASA Artemis II mission profile with multiple Earth-orbit maneuvers: https://www.nasa.gov/missions/artemis/nasas-first-flight-with-crew-important-step-on-long-term-return-to-the-moon-missions-to-mars/
- NASA SLS Block 1B reference: https://www.nasa.gov/reference/sls-space-launch-system-block-1b/
- NASA HLS update paper: https://ntrs.nasa.gov/api/citations/20240012719/downloads/HLS%20Update%20Kent%20Chojnacki%20IEEE%20Aero%202025%20v2.pdf
- Matt Lowne public Moonship craft video source: https://www.youtube.com/watch?v=OJCCDIBmrBI
- ESA Artemis III architecture page, historical/contextual because ESA says it will update the page: https://www.esa.int/Science_Exploration/Human_and_Robotic_Exploration/Orion/Artemis_III

Reusable principles for KSP:

- Treat Orion as the crew transport and Earth/Kerbin return vehicle.
- Treat SLS as the high-energy launch/TMI stack for Orion, not as the lunar surface lander.
- Treat HLS as a separately launched/predeployed lunar/Mun descent and ascent system.
- Keep HLS free of Kerbin re-entry hardware; put heat shield and parachute responsibility on Orion.
- Score the split architecture by four phase gates: HLS parked in Mun orbit, Orion captured in Mun orbit, HLS landed/ascent back to Mun orbit, Orion recovered on Kerbin.
- Current NASA planning no longer makes Artemis III the first lunar landing; model the architecture rather than hard-coding the mission number.
- For KSP automation, crew transfer is currently a rendezvous-equivalent phase until docking automation is implemented.
- For crewed Orion/SLS phases, prefer a free-return trajectory before lunar capture and permit multiple parking/phasing orbits before TMI.
- For the stock KSP profile, approximate Artemis II/SLS checkout behavior by allowing multiple Kerbin parking revolutions and timing TMI from a stable parking orbit instead of forcing an immediate burn after circularization.
- For HLS/Starship phases, use a complete KSP-authored craft file where possible. The verified local priority is downloaded Moonship craft first, ACK Kerbal Landing System second, and generated HLS Project craft only as an experimental fallback.
