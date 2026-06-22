from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import krpc

from ksp_lab.config import load_config
from ksp_lab.telemetry import TelemetryRecorder


def fuel_fraction(vessel) -> float:
    resources = vessel.resources
    amount = 0.0
    total = 0.0
    for resource in ("LiquidFuel", "Oxidizer", "SolidFuel"):
        if resources.has_resource(resource):
            amount += resources.amount(resource)
            total += resources.max(resource)
    return amount / total if total else 0.0


def record(vessel, recorder: TelemetryRecorder, phase: str, started: float, extra: dict[str, Any] | None = None) -> None:
    flight = vessel.flight(vessel.orbit.body.reference_frame)
    sample = {
        "elapsed_s": time.monotonic() - started,
        "phase": phase,
        "altitude_m": float(flight.mean_altitude),
        "surface_altitude_m": float(flight.surface_altitude),
        "apoapsis_m": float(vessel.orbit.apoapsis_altitude),
        "periapsis_m": float(vessel.orbit.periapsis_altitude),
        "fuel_fraction_left": fuel_fraction(vessel),
        "current_stage": int(vessel.control.current_stage),
        "available_thrust_n": float(vessel.available_thrust),
        "orbit_body": str(vessel.orbit.body.name),
        "situation": str(vessel.situation),
        "surface_speed_mps": float(flight.speed),
        "horizontal_speed_mps": float(flight.horizontal_speed),
        "vertical_speed_mps": float(flight.vertical_speed),
    }
    if extra:
        sample.update(extra)
    recorder.append(sample)


def target_retrograde(vessel) -> None:
    flight = vessel.flight(vessel.orbit.body.reference_frame)
    vessel.auto_pilot.reference_frame = vessel.orbit.body.reference_frame
    vessel.auto_pilot.target_direction = flight.retrograde
    try:
        vessel.auto_pilot.engage()
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/local-ksp.yaml")
    parser.add_argument("--telemetry", required=True)
    parser.add_argument("--timeout-s", type=int, default=1800)
    args = parser.parse_args()

    config = load_config(Path(args.config))
    conn = krpc.connect(
        name="ksp1-automation-lab-descent-resume",
        address=config["krpc"].get("host", "127.0.0.1"),
        rpc_port=int(config["krpc"].get("rpc_port", 50000)),
        stream_port=int(config["krpc"].get("stream_port", 50001)),
    )
    vessel = conn.space_center.active_vessel
    recorder = TelemetryRecorder(args.telemetry)
    started = time.monotonic()

    if vessel.orbit.body.name != "Mun":
        record(vessel, recorder, "mun_descent_resume_wrong_body", started)
        return 2

    vessel.control.throttle = 0.0
    vessel.control.legs = True
    conn.space_center.physics_warp_factor = 0
    record(vessel, recorder, "mun_descent_resume_start", started)

    while time.monotonic() - started < args.timeout_s:
        flight = vessel.flight(vessel.orbit.body.reference_frame)
        surface_altitude = float(flight.surface_altitude)
        if surface_altitude < 120_000.0 or vessel.orbit.time_to_periapsis < 900.0:
            break
        coast_s = min(600.0, max(0.0, float(vessel.orbit.time_to_periapsis) - 900.0))
        if coast_s <= 1.0:
            break
        record(vessel, recorder, "mun_descent_rails_warp", started, {"coast_s": coast_s})
        conn.space_center.warp_to(
            float(conn.space_center.ut) + coast_s,
            max_rails_rate=100_000.0,
            max_physics_rate=4.0,
        )

    last_record = 0.0
    while time.monotonic() - started < args.timeout_s:
        situation = str(vessel.situation).lower()
        if situation.endswith("landed"):
            vessel.control.throttle = 0.0
            conn.space_center.physics_warp_factor = 0
            flight = vessel.flight(vessel.orbit.body.reference_frame)
            touchdown_speed = float(flight.speed)
            if touchdown_speed <= 8.0:
                record(vessel, recorder, "mun_landed", started, {"landed": True, "touchdown_speed_mps": touchdown_speed})
                return 0
            record(vessel, recorder, "mun_landed_hard", started, {"landed": True, "touchdown_speed_mps": touchdown_speed})
            return 5

        flight = vessel.flight(vessel.orbit.body.reference_frame)
        surface_altitude = max(0.0, float(flight.surface_altitude))
        vertical_speed = float(flight.vertical_speed)
        horizontal_speed = float(flight.horizontal_speed)
        speed = float(flight.speed)
        available_thrust = max(1.0, float(vessel.available_thrust))
        max_accel = max(0.1, available_thrust / max(0.1, float(vessel.mass)))
        gravity = float(vessel.orbit.body.surface_gravity)
        stopping_accel = max(0.1, max_accel - gravity)
        stopping_distance = speed * speed / (2.0 * stopping_accel)
        burn_gate = stopping_distance * 2.2 + 8_000.0

        target_retrograde(vessel)
        if surface_altitude > burn_gate and surface_altitude > 35_000.0:
            vessel.control.throttle = 0.0
            if surface_altitude > 80_000.0:
                conn.space_center.physics_warp_factor = 3
            else:
                conn.space_center.physics_warp_factor = 0
        else:
            conn.space_center.physics_warp_factor = 0
            if surface_altitude > 20_000.0:
                target_vertical = -140.0
            elif surface_altitude > 10_000.0:
                target_vertical = -95.0
            elif surface_altitude > 5_000.0:
                target_vertical = -55.0
            elif surface_altitude > 2_000.0:
                target_vertical = -30.0
            elif surface_altitude > 800.0:
                target_vertical = -16.0
            elif surface_altitude > 250.0:
                target_vertical = -8.0
            elif surface_altitude > 60.0:
                target_vertical = -4.0
            else:
                target_vertical = -1.5

            too_slow_high = vertical_speed > target_vertical + 8.0 and surface_altitude > 50.0 and horizontal_speed < 80.0
            if too_slow_high:
                throttle = 0.0
            else:
                correction = max(0.0, target_vertical - vertical_speed) / 2.0
                throttle = (gravity + correction) / max_accel
                if surface_altitude < stopping_distance * 1.35 + 120.0:
                    throttle = max(throttle, 0.75)
                if surface_altitude < stopping_distance * 0.95 + 45.0:
                    throttle = max(throttle, 0.95)
                if surface_altitude < 900.0 and horizontal_speed > 25.0:
                    throttle = max(throttle, 0.45)
                if surface_altitude < 250.0 and horizontal_speed > 12.0:
                    throttle = max(throttle, 0.75)
                if horizontal_speed > 120.0 and surface_altitude < 30_000.0:
                    throttle = max(throttle, 0.80)
                if surface_altitude < 15.0 and vertical_speed > -6.0 and horizontal_speed < 6.0 and speed < 8.0:
                    throttle = 0.0
                if surface_altitude < 30.0 and vertical_speed > -2.0:
                    throttle = min(throttle, gravity / max_accel)
                if surface_altitude < 8.0 and abs(vertical_speed) < 2.5 and horizontal_speed < 5.0:
                    throttle = 0.0
            vessel.control.throttle = min(1.0, max(0.0, throttle))

        now = time.monotonic()
        if now - last_record > 0.5:
            record(
                vessel,
                recorder,
                "mun_landing_descent",
                started,
                {
                    "landing_throttle": float(vessel.control.throttle),
                    "stopping_distance_m": stopping_distance,
                    "burn_gate_m": burn_gate,
                },
            )
            last_record = now

        if fuel_fraction(vessel) <= 0.001 and vessel.available_thrust < 1.0:
            vessel.control.throttle = 0.0
            conn.space_center.physics_warp_factor = 0
            record(vessel, recorder, "mun_landing_out_of_fuel", started)
            return 3
        time.sleep(0.1)

    vessel.control.throttle = 0.0
    conn.space_center.physics_warp_factor = 0
    record(vessel, recorder, "mun_landing_timeout", started)
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
