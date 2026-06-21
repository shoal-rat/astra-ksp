from __future__ import annotations

from .models import MissionSpec, RocketDesign, ScoreResult, TelemetrySummary


class MissionScorer:
    def score(self, mission: MissionSpec, design: RocketDesign, telemetry: TelemetrySummary) -> ScoreResult:
        components: dict[str, float] = {}
        success = False
        failure = ""

        if telemetry.vessel_destroyed:
            failure = "low_twr" if telemetry.mission_phase == "pad_or_low_atmosphere_failure" else "vessel_destroyed"
            components["mission"] = 0.0
        elif mission.mission_type == "kerbin_orbit":
            apo_ok = telemetry.apoapsis_m >= mission.target_orbit_m
            peri_ok = telemetry.periapsis_m >= 70_000
            success = apo_ok and peri_ok
            if not apo_ok:
                failure = "apoapsis_below_target"
            elif not peri_ok:
                failure = "periapsis_not_orbital"
            components["mission"] = 70.0 if success else max(0.0, telemetry.apoapsis_m / mission.target_orbit_m * 50.0)
        elif mission.mission_type == "mun_landing_return":
            success = telemetry.recovered and telemetry.max_altitude_m > 10_000_000
            failure = "" if success else telemetry.mission_phase or "mun_profile_incomplete"
            components["mission"] = 80.0 if success else min(60.0, telemetry.max_altitude_m / 10_000_000 * 40.0)
        else:
            success = telemetry.max_altitude_m > 10_000
            failure = "" if success else "generic_goal_not_met"
            components["mission"] = 50.0 if success else min(40.0, telemetry.max_altitude_m / 10_000)

        estimates = design.estimates
        components["fuel_margin"] = min(10.0, max(0.0, telemetry.fuel_fraction_left * 30.0))
        components["cost"] = max(0.0, 10.0 - estimates.get("cost", 0.0) / 5000.0)
        components["part_count"] = max(0.0, 10.0 - estimates.get("part_count", 0.0) / 12.0)
        score = sum(components.values())
        return ScoreResult(round(score, 3), success, failure, components)
