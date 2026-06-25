"""Two-ship orbital-docking architecture to bring a kerbal home from Eve WITHOUT in-flight refueling.

WHY TWO SHIPS: the single-ship round trip (tools/crewed_eve_roundtrip.py) stranded crew #9 dry at Eve on
the RETURN — one liftable stack could not carry enough vacuum Δv to ALSO escape Eve from the capture
periapsis. The fix that keeps the HARD RULE (no fuel is ever pumped between vessels in flight) is to fly
the return propellant on a SECOND, separate ship that waits fully fuelled in Eve orbit. The crew
rendezvous + DOCKS (MechJeb `mj_rendezvous` + `mj_dock`), the kerbal walks across the docking tunnel, and
rides the tug home. No propellant changes vessels — only the kerbal does.

Both designs are PNG-verified (read the three-view: 4 symmetric strap-ons on the ferry, 8 on the tug,
payload + docking port housed, engines at the base, nothing floating):
    FERRY: crew=1, dock, vac 3800, 4 radial boosters         -> 238 t, TWR 1.57, feasible
    TUG:   crew=1 (empty until dock), dock+heatshield+chutes, vac 4000, 8 boosters, 2-engine core
           -> 436 t, TWR 1.71, feasible (carries the full Eve->Kerbin return budget)
"""
from __future__ import annotations

from ksp_lab.design import (LandingSite, Phase, ShipRequirements, default_reserve_frac, design_ship)

KERBIN_G = 9.81
KERBIN_SL_RHO = 1.225


def _phases(vac_dv: float) -> list[Phase]:
    return [
        Phase("booster", 4200.0, twr_body_g=KERBIN_G, min_twr=1.3,
              reserve_frac=default_reserve_frac(KERBIN_G)),
        Phase("vacuum", vac_dv, twr_body_g=KERBIN_G, min_twr=0.5,
              reserve_frac=default_reserve_frac(0.0)),
    ]


def crew_ferry() -> ShipRequirements:
    """Light one-way crew delivery to Eve orbit (no return fuel, no heat shield — the crew rides the tug
    home). Docking port + RCS for the final approach."""
    return ShipRequirements(
        name="AI-Eve-Ferry2", mission_type="crewed", crew=1, payload_t=0.3,
        phases=_phases(3800.0), landing=None, needs_heatshield=False, needs_docking=True,
        max_engine_count=1, radial_booster_count=4,
    )


def return_tug() -> ShipRequirements:
    """Heavy return vehicle: sent to Eve orbit carrying the FULL return budget, plus a heat shield + chutes
    for the Kerbin aerocapture/landing and a docking port + RCS. crew=1 gives a crewable seat the ferry's
    kerbal transfers into (it launches headless; the seat fills at the dock). Needs 8 strap-ons + a
    2-engine core to lift the heavy return-fuel upper."""
    return ShipRequirements(
        name="AI-Eve-Tug2", mission_type="crewed", crew=1, payload_t=0.3,
        phases=_phases(4000.0), landing=LandingSite(KERBIN_G, KERBIN_SL_RHO),
        needs_heatshield=True, needs_docking=True, max_engine_count=2, radial_booster_count=8,
    )


if __name__ == "__main__":
    for label, req in (("FERRY", crew_ferry()), ("TUG", return_tug())):
        d = design_ship(req)
        e = d.estimates
        print(f"{label} {req.name}: {e['wet_mass_t']:.0f} t, launch TWR {e['launch_twr']:.2f}, "
              f"total Δv {e['total_delta_v_mps']:.0f} m/s, docking_port={d.docking_port}, "
              f"heatshield={d.heatshield}, feasible={d.feasible}")
