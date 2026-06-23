# Interplanetary Comms Constellation — continuous Kerbin↔Duna↔Mun↔Ike connectivity

Designed from real relay-network research (Mars Relay Network, LunaNet/TDRSS, Sun–planet L4/L5
trunk relays, Walker constellations) + KSP CommNet mechanics. Replaces the failed eccentric/RA-2
constellation that left Duna and Ike prone to signal loss.

## Why the previous attempt failed
- **Eccentric orbits** (comsats 7/8 at e=0.29–0.35): by Kepler's 2nd law the sats bunch at periapsis
  and crawl at apoapsis → uneven phasing → coverage gaps that open/close every orbit; altitude (hence
  footprint and inter-sat hop) swings ~2×.
- **Weak antennas**: RA-2 vs a level-3 DSN = √(2·250) = **22.4 Gm**, short of the Kerbin↔Duna
  **conjunction distance 34.3 Gm** → the home link died for a large slice of every synodic period.
- **No conjunction handling**: every ~910 d Duna passes behind Kerbol; the Sun physically occludes the
  direct line (occlusion ignores antenna power). No off-axis relay → guaranteed weeks-long blackout.
- **Ike an afterthought**: Duna's body occults Ike, and tidally-locked Ike hides one hemisphere from any
  Duna-side relay → intermittent Ike contact.

## The architecture (all CIRCULAR orbits, every hub carries an RA-100 relay, vessel type = Relay)
Link rule: `R = √(A·B)`; a link needs `separation < R` AND no body (incl. the Sun) on the line.
Key ranges: RA-100↔DSN3 = **158 Gm**; RA-100↔RA-100 = **100 Gm**; conjunction = 34.3 Gm; worst trunk hop = 29.9 Gm.

| Tier | Sats | Orbit | Why |
|---|---|---|---|
| **0. DSN** | — | Tracking Station **level 3** (250 Gm) | biggest range lever (√ law); ~11× vs DSN1 |
| **1. Duna ring** | 4 | circular equatorial, **2 640 km alt** (SMA 2 960 km, T≈16.2 h), 90° phased | constant footprint + frozen phasing; sits **below Ike** (3 200 km) so never Ike-captured; horizon angle ~84° ≫ 60° needed |
| **1b. Ike relay** | 1 | low circular Ike orbit ~150 km | Duna occults Ike + tidal lock → Ike needs its own relay; hops Ike→ring→trunk→DSN |
| **2. Trunk (conjunction-proof)** | 2 | heliocentric @ Duna radius (20.726 Gm), one **+60° (L4)**, one **−60° (L5)** | a planet + its L4 or L5 is always Sun-visible from any other planet → eliminates conjunction blackout (verified Sun-free across full 910-d synodic cycle) |
| **3. Kerbin ring** | 3 | circular keostationary **2 863 km alt** (SMA 3 463 km, T=1 Kerbin day), 120° | TDRSS-style home ring; always one in view of the Mun relay + inbound trunk |
| **4. Mun relay** | 1 | circular Mun orbit ~500–1000 km equatorial | Mun assets → keo ring → DSN (far-side: add 2nd or Kerbin–Mun L2 relay) |

Routing: Duna ring → nearer trunk relay (≤30 Gm) → DSN3 (158 Gm) at conjunction; direct ring→DSN
(158 Gm) the rest of the synodic period with the trunk as hot standby (CommNet auto-routes).

## Deployment order
1. Tracking Station → level 3 (if career; sandbox already max).
2. Add Ike to `bodies.py` (done — mu 1.857e10, R 130 km, parent Duna, orbit 3.2 Mm).
3. Confirm RA-100 build path: `RelayAntenna100` harvested (`craft_writer._design_part_names`) so the
   bus uses it, not the weak `longAntenna` fallback (FIXED, commit 120be0c).
4. Kerbin home ring: 3 keostationary RA-100 relays, 120°.
5. Mun relay: 1 RA-100, circular equatorial.
6/7. Heliocentric trunk A (+60°/L4) and B (−60°/L5) at Duna's radius.
8. Duna ring: 4 RA-100 comsats → circularize at 2 640 km, 90° phased, below Ike.
9. Ike relay: 1 RA-100 in low Ike orbit.
10. End-to-end verify incl. a forced near-conjunction warp (confirm auto-route through the trunk).
