from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ksp_lab.guidance import capture_burn_estimate, suicide_burn_distance_m


def main() -> int:
    parser = argparse.ArgumentParser(description="Print live KSP guidance trigger data from kRPC.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--rpc-port", type=int, default=50000)
    parser.add_argument("--stream-port", type=int, default=50001)
    args = parser.parse_args()

    import krpc

    conn = krpc.connect(
        name="ksp1-guidance-snapshot",
        address=args.host,
        rpc_port=args.rpc_port,
        stream_port=args.stream_port,
    )
    try:
        vessel = conn.space_center.active_vessel
        body = vessel.orbit.body
        flight = vessel.flight(body.reference_frame)
        mass = float(vessel.mass)
        thrust = float(vessel.available_thrust)
        gravity = float(body.surface_gravity)
        vertical_speed = float(flight.vertical_speed)
        surface_speed = float(flight.speed)
        horizontal_speed = float(flight.horizontal_speed)
        descent_speed = max(0.0, -vertical_speed)
        landing_vertical_stop = suicide_burn_distance_m(
            speed_mps=descent_speed,
            mass_kg=mass,
            thrust_n=max(1.0, thrust),
            gravity_mps2=gravity,
        )
        landing_surface_stop = suicide_burn_distance_m(
            speed_mps=surface_speed,
            mass_kg=mass,
            thrust_n=max(1.0, thrust),
            gravity_mps2=gravity,
            safety_margin_m=80.0,
        )
        capture = capture_burn_estimate(
            mu=float(body.gravitational_parameter),
            body_radius_m=float(body.equatorial_radius),
            periapsis_altitude_m=float(vessel.orbit.periapsis_altitude),
            semi_major_axis_m=float(vessel.orbit.semi_major_axis),
            mass_kg=mass,
            thrust_n=max(1.0, thrust),
        )
        payload = {
            "ut": float(conn.space_center.ut),
            "vessel": vessel.name,
            "body": body.name,
            "situation": str(vessel.situation),
            "stage": int(vessel.control.current_stage),
            "mass_kg": mass,
            "dry_mass_kg": float(vessel.dry_mass),
            "available_thrust_n": thrust,
            "twr_local": thrust / max(0.1, mass * gravity),
            "specific_impulse_s": float(vessel.specific_impulse),
            "surface_altitude_m": float(flight.surface_altitude),
            "mean_altitude_m": float(flight.mean_altitude),
            "vertical_speed_mps": vertical_speed,
            "horizontal_speed_mps": horizontal_speed,
            "surface_speed_mps": surface_speed,
            "landing_vertical_stop_m": landing_vertical_stop,
            "landing_surface_stop_m": landing_surface_stop,
            "orbit": {
                "apoapsis_altitude_m": float(vessel.orbit.apoapsis_altitude),
                "periapsis_altitude_m": float(vessel.orbit.periapsis_altitude),
                "semi_major_axis_m": float(vessel.orbit.semi_major_axis),
                "eccentricity": float(vessel.orbit.eccentricity),
                "speed_mps": float(vessel.orbit.speed),
                "time_to_periapsis_s": float(vessel.orbit.time_to_periapsis),
            },
            "capture_estimate": {
                "delta_v_mps": capture.delta_v_mps,
                "burn_time_s": capture.burn_time_s,
                "lead_time_s": capture.lead_time_s,
            },
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
