from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

from .guidance import (
    burn_duration_s,
    capture_burn_estimate,
    finite_burn_lead_s,
    hohmann_transfer_delta_v_mps,
    hohmann_transfer_time_s,
    hoverslam_reference_speed_mps,
    hoverslam_throttle,
    outward_transfer_phase_angle_rad,
    suicide_burn_distance_m,
    terminal_landing_throttle,
    vis_viva_speed_mps,
    vertical_landing_throttle,
)
from .models import MissionSpec, RocketDesign, TelemetrySummary
from .parts import estimate_design
from .telemetry import TelemetryRecorder


class FlightControllerError(RuntimeError):
    pass


class OfflineSurrogateController:
    """Fast local evaluator used for smoke tests and optimizer dry runs."""

    def fly(
        self,
        mission: MissionSpec,
        design: RocketDesign,
        telemetry_path: str | Path,
        timeout_s: int = 900,
    ) -> TelemetrySummary:
        estimates = estimate_design(design)
        recorder = TelemetryRecorder(telemetry_path)
        dv = estimates["delta_v_mps"]
        twr = estimates["launch_twr"]
        orbit_margin = dv - mission.delta_v_budget_mps

        if twr < 1.1:
            phase = "pad_or_low_atmosphere_failure"
            max_alt = max(100.0, 2000.0 * twr)
            apo = max_alt
            peri = -600000.0
            fuel_left = 0.0
        elif orbit_margin < 0:
            phase = "under_delta_v"
            ratio = max(0.05, dv / max(1, mission.delta_v_budget_mps))
            max_alt = mission.target_orbit_m * min(0.95, ratio)
            apo = max_alt
            peri = -600000.0
            fuel_left = 0.0
        else:
            fuel_left = min(0.35, orbit_margin / max(1, mission.delta_v_budget_mps))
            if mission.mission_type == "mun_landing_return":
                phase = "recovered" if orbit_margin > 250 else "thin_return_margin"
                max_alt = 12_000_000.0
                apo = 12_000_000.0
                peri = 35_000.0 if orbit_margin > 0 else -1.0
            else:
                phase = "orbit_delivered"
                max_alt = mission.target_orbit_m + min(20_000.0, orbit_margin)
                apo = max_alt
                peri = mission.target_orbit_m - min(10_000.0, orbit_margin / 2)

        for step in range(6):
            frac = step / 5
            recorder.append(
                {
                    "elapsed_s": frac * min(timeout_s, 600),
                    "phase": phase,
                    "altitude_m": max_alt * math.sin(frac * math.pi / 2),
                    "apoapsis_m": apo * frac,
                    "periapsis_m": peri,
                    "fuel_fraction_left": fuel_left * frac,
                    "vessel_destroyed": phase == "pad_or_low_atmosphere_failure",
                    "landed": phase == "recovered",
                    "recovered": phase == "recovered",
                    "offline": True,
                    "estimated_delta_v_mps": dv,
                    "estimated_launch_twr": twr,
                }
            )
        return recorder.summarize()


class KrpcFlightController:
    def __init__(self, krpc_config: dict[str, Any]):
        self.config = krpc_config

    def _connect(self, name_suffix: str = ""):
        try:
            import krpc
        except ImportError as exc:
            raise FlightControllerError("Install live KSP support with: pip install '.[ksp]'") from exc

        base_name = self.config.get("name", "ksp1-automation-lab")
        name = f"{base_name}-{name_suffix}" if name_suffix else base_name
        return krpc.connect(
            name=name,
            address=self.config.get("host", "127.0.0.1"),
            rpc_port=int(self.config.get("rpc_port", 50000)),
            stream_port=int(self.config.get("stream_port", 50001)),
        )

    @staticmethod
    def _select_vessel(conn, vessel_name: str):
        for vessel in conn.space_center.vessels:
            if str(vessel.name) == vessel_name:
                conn.space_center.active_vessel = vessel
                time.sleep(1.0)
                return conn.space_center.active_vessel
        raise FlightControllerError(f"Vessel not found in kRPC vessel list: {vessel_name}")

    @staticmethod
    def _vessel_is_usable(vessel, preferred_name: str = "") -> bool:
        try:
            if preferred_name and not str(vessel.name).startswith(preferred_name):
                return False
            vessel_type = getattr(vessel, "vessel_type", getattr(vessel, "type", ""))
            if "debris" in str(vessel_type).lower():
                return False
            return len(vessel.parts.all) > 0 and float(vessel.mass) > 0.0
        except Exception:
            return False

    @classmethod
    def _reacquire_vessel(cls, conn, vessel, preferred_name: str = ""):
        if cls._vessel_is_usable(vessel, preferred_name):
            return vessel

        try:
            active = conn.space_center.active_vessel
            if cls._vessel_is_usable(active, preferred_name):
                return active
        except Exception:
            pass

        candidates = []
        for candidate in conn.space_center.vessels:
            if not cls._vessel_is_usable(candidate, preferred_name):
                continue
            try:
                stage_status = cls._stage_status(candidate)
                candidates.append(
                    (
                        int(stage_status["fueled_active_engines"]) * 1_000_000
                        + int(stage_status["active_engines"]) * 100_000
                        + min(float(candidate.available_thrust), 1_000_000.0) / 100.0
                        + len(candidate.parts.all) * 10
                        + min(float(candidate.mass), 100_000.0) / 1000.0,
                        candidate,
                    )
                )
            except Exception:
                continue

        if candidates:
            selected = max(candidates, key=lambda item: item[0])[1]
            try:
                conn.space_center.active_vessel = selected
                time.sleep(0.25)
                return conn.space_center.active_vessel
            except Exception:
                return selected

        raise FlightControllerError(f"Unable to reacquire usable vessel after staging: {preferred_name or '<active vessel>'}")

    def run_hls_surface_sortie(
        self,
        vessel_name: str,
        telemetry_path: str | Path,
        timeout_s: int = 900,
    ) -> TelemetrySummary:
        conn = self._connect("hls-sortie")
        recorder = TelemetryRecorder(telemetry_path)
        vessel = self._select_vessel(conn, vessel_name)
        start = time.monotonic()
        try:
            if vessel.orbit.body.name != "Mun":
                self._record_live_sample(vessel, recorder, start, "artemis_hls_not_in_mun_orbit")
                return recorder.summarize()
            if self._land_on_mun(conn, vessel, recorder, start, timeout_s):
                if self._perform_mun_surface_science(vessel, recorder, start) and self._launch_from_mun(
                    conn, vessel, recorder, start, timeout_s
                ):
                    self._record_live_sample(vessel, recorder, start, "artemis_hls_returned_to_mun_orbit")
        finally:
            self._set_physics_warp(conn, 0)
            try:
                vessel.auto_pilot.disengage()
            except Exception:
                pass
        return recorder.summarize()

    def run_orion_return(
        self,
        vessel_name: str,
        telemetry_path: str | Path,
        timeout_s: int = 900,
    ) -> TelemetrySummary:
        conn = self._connect("orion-return")
        recorder = TelemetryRecorder(telemetry_path)
        vessel = self._select_vessel(conn, vessel_name)
        start = time.monotonic()
        try:
            if vessel.orbit.body.name != "Mun":
                self._record_live_sample(vessel, recorder, start, "artemis_orion_not_in_mun_orbit")
                return recorder.summarize()
            self._return_to_kerbin_from_mun_orbit(conn, vessel, recorder, start, timeout_s)
        finally:
            self._set_physics_warp(conn, 0)
            try:
                vessel.auto_pilot.disengage()
            except Exception:
                pass
        return recorder.summarize()

    def run_dock_and_transfer(
        self,
        chaser_name: str,
        target_name: str,
        telemetry_path: str | Path,
        timeout_s: int = 1500,
    ) -> TelemetrySummary:
        """Automated rendezvous + docking + (merge-based) crew transfer + undock.

        Both craft must carry a Clamp-O-Tron (``docking_port=True``) and RCS. The chaser closes on
        the target with RCS proximity ops and mates docking ports; once docked the two vessels MERGE
        into one in KSP, which is the crew transfer (crew can move freely between docked modules).
        The chaser then undocks. NOTE: like every capability in this project, robust autonomous
        rendezvous needs live tuning — from far orbits the closing phase is best-effort; the
        proximity dock assumes the craft start within a few km (arrange matching launch orbits).
        """
        conn = self._connect("dock")
        recorder = TelemetryRecorder(telemetry_path)
        sc = conn.space_center
        chaser = self._select_vessel(conn, chaser_name)
        target = self._select_vessel(conn, target_name)
        start = time.monotonic()
        try:
            sc.active_vessel = chaser
            try:
                sc.target_vessel = target
            except Exception:
                pass
            chaser.control.rcs = True
            chaser.control.sas = False
            # Seat real astronauts in the (headless-launched, empty) chaser pod before the dock, so
            # there are actual crew to transfer to the target afterwards.
            seated = self._bridge_spawn_crew(count=2)
            self._record_live_sample(chaser, recorder, start, "dock_rendezvous_start",
                                     {"target": target_name, "crew_seated": seated})
            # Rendezvous-from-far: close an arbitrary co-body orbit down to RCS range before the
            # proximity dock. Skipped automatically if already close.
            if self._relative_distance_m(chaser, target) > 2500.0:
                self._rendezvous_from_far(conn, chaser, target, recorder, start, timeout_s)
            docked = self._approach_and_dock(conn, chaser, target, recorder, start, timeout_s)
            if not docked:
                self._record_live_sample(chaser, recorder, start, "dock_not_completed")
                return recorder.summarize()
            # Docked => the two craft are now ONE vessel. Move a REAL kerbal across via the bridge
            # crew-transfer endpoint (no-op if the rebuilt mod isn't loaded yet).
            moved = self._bridge_transfer_crew(target_name)
            self._record_live_sample(chaser, recorder, start, "dock_crew_transfer_complete",
                                     {"crew_moved": moved})
            self._undock_after_transfer(conn, chaser, recorder, start)
            self._record_live_sample(chaser, recorder, start, "dock_and_transfer_complete")
        finally:
            try:
                chaser.control.rcs = False
                chaser.auto_pilot.disengage()
            except Exception:
                pass
        return recorder.summarize()

    def _bridge_spawn_crew(self, count: int = 2) -> int:
        """Seat up to ``count`` roster kerbals into the active vessel via the bridge (a headless
        launch leaves crewed pods empty). Returns how many were seated. Best-effort."""
        import json as _json
        import urllib.request

        host = self.config.get("bridge_host", "127.0.0.1")
        port = int(self.config.get("bridge_port", 48500))
        seated = 0
        for _ in range(max(0, count)):
            try:
                req = urllib.request.Request(
                    f"http://{host}:{port}/spawn-crew", data=b"{}",
                    headers={"content-type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    if _json.loads(resp.read().decode("utf-8")).get("ok"):
                        seated += 1
                    else:
                        break
            except Exception:
                break
        return seated

    def _bridge_transfer_crew(self, to_vessel: str) -> bool:
        """Move a kerbal across the dock via the bridge. Tries the target vessel first; if it has no
        free crewable seat (e.g. a probe lander), falls back to moving a kerbal to any free seat in
        the now-merged docked stack. Best-effort; False if the endpoint is unavailable."""
        import json as _json
        import urllib.request

        host = self.config.get("bridge_host", "127.0.0.1")
        port = int(self.config.get("bridge_port", 48500))

        def attempt(payload: dict) -> bool:
            try:
                req = urllib.request.Request(
                    f"http://{host}:{port}/transfer-crew",
                    data=_json.dumps(payload).encode("utf-8"),
                    headers={"content-type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return bool(_json.loads(resp.read().decode("utf-8")).get("ok"))
            except Exception:
                return False

        return attempt({"toVessel": to_vessel}) or attempt({})

    @staticmethod
    def _relative_distance_m(chaser, target) -> float:
        try:
            p = target.position(chaser.reference_frame)
            return (p[0] ** 2 + p[1] ** 2 + p[2] ** 2) ** 0.5
        except Exception:
            return 1.0e12

    def _match_orbital_plane(self, conn, chaser, target, recorder, start, timeout_s) -> bool:
        """Reduce the relative inclination to the target's plane with a normal burn at the relative
        node. Coplanar -> no-op. A near-retrograde mismatch needs ~2x orbital velocity and the craft
        may run out of fuel; the burn does what it can and the result is recorded honestly. Best-
        effort and intended to be observed live."""
        import math

        sc = conn.space_center
        ref = chaser.orbit.body.non_rotating_reference_frame

        def unit(v3):
            m = self._norm(v3)
            return (v3[0] / m, v3[1] / m, v3[2] / m) if m > 1e-9 else (0.0, 0.0, 1.0)

        def orbit_normal(v):
            return unit(self._cross(v.position(ref), v.velocity(ref)))

        def rel_incl_deg():
            nc, nt = orbit_normal(chaser), orbit_normal(target)
            d = max(-1.0, min(1.0, nc[0] * nt[0] + nc[1] * nt[1] + nc[2] * nt[2]))
            return math.degrees(math.acos(d)), nc, nt

        rel, nc, nt = rel_incl_deg()
        self._record_live_sample(chaser, recorder, start, "rendezvous_plane_relincl",
                                 {"rel_incl_deg": rel})
        # Only do a plane change for a LARGE mismatch (e.g. a retrograde capture). For a small
        # relative inclination the orbits cross near the nodes, so the phasing-drift + RCS proximity
        # ops can close the residual cross-track — and a precise node burn isn't worth the risk/time.
        if rel < 6.0:
            return True
        # The two planes intersect along node = nc x nt; the chaser is at the node when its position
        # lies along +/- that line. Warp there (with a HARD cap so this can never stall) so a normal
        # burn rotates its plane toward the target.
        node = unit(self._cross(nc, nt))
        deadline = time.monotonic() + 150.0
        while time.monotonic() < deadline:
            pos = unit(chaser.position(ref))
            align = abs(pos[0] * node[0] + pos[1] * node[1] + pos[2] * node[2])
            self._record_live_sample(chaser, recorder, start, "rendezvous_plane_seek_node",
                                     {"align": align, "rel_incl_deg": rel_incl_deg()[0]})
            if align > 0.97:
                break
            try:
                sc.warp_to(sc.ut + max(8.0, float(chaser.orbit.period) / 96.0),
                           max_rails_rate=1000.0, max_physics_rate=4.0)
            except Exception:
                time.sleep(1.0)
        self._set_physics_warp(conn, 0)
        # Point along the direction that rotates the chaser normal toward the target normal and burn.
        desired = unit((nt[0] - nc[0], nt[1] - nc[1], nt[2] - nc[2]))
        try:
            ap = chaser.auto_pilot
            ap.reference_frame = ref
            ap.target_direction = desired
            ap.engage()
            time.sleep(6.0)
        except Exception:
            pass
        t0 = time.monotonic()
        chaser.control.throttle = 1.0
        while time.monotonic() - t0 < min(timeout_s, 240):
            cur, _, _ = rel_incl_deg()
            if cur < 1.5 or self._fuel_fraction(chaser) < 0.05:
                break
            try:
                ap.target_direction = unit((orbit_normal(target)[0] - orbit_normal(chaser)[0],
                                            orbit_normal(target)[1] - orbit_normal(chaser)[1],
                                            orbit_normal(target)[2] - orbit_normal(chaser)[2]))
            except Exception:
                pass
            time.sleep(0.5)
        chaser.control.throttle = 0.0
        final, _, _ = rel_incl_deg()
        self._record_live_sample(chaser, recorder, start, "rendezvous_plane_matched",
                                 {"rel_incl_deg": final})
        return final < 5.0

    def _rendezvous_from_far(self, conn, chaser, target, recorder, start, timeout_s) -> bool:
        """Close an arbitrary same-body orbit down to RCS proximity range.

        (1) Match the target's orbital altitude (two apsis burns -> near-circular at the target's
        mean altitude). (2) Drop into a slightly lower PHASING orbit so the chaser orbits faster and
        the angular gap to the target shrinks. (3) Drift, watching the relative distance, and when it
        falls inside proximity range, circularize back onto the target's orbit to kill the drift.
        Designed to be driven/observed live (telemetry markers every step); robust autonomous
        rendezvous classically needs a few live adjustments.
        """
        sc = conn.space_center
        body_radius = float(chaser.orbit.body.equatorial_radius)
        mu = float(chaser.orbit.body.gravitational_parameter)
        # 0. Match the target's ORBITAL PLANE first. Surface-ascended targets (and an unlucky
        #    capture) can sit in a very different plane; without this the closest approach is floored
        #    by the plane separation no matter how well altitude/phase are matched.
        self._match_orbital_plane(conn, chaser, target, recorder, start, timeout_s)
        try:
            t_alt = max(5_000.0, 0.5 * (float(target.orbit.apoapsis_altitude)
                                        + float(target.orbit.periapsis_altitude)))
        except Exception:
            return False
        self._record_live_sample(chaser, recorder, start, "rendezvous_match_orbit_begin",
                                 {"target_alt_m": t_alt, "distance_m": self._relative_distance_m(chaser, target)})

        def set_apsis(set_attr: str, burn_at: str) -> None:
            try:
                cur_apo = float(chaser.orbit.apoapsis_altitude)
                cur_per = float(chaser.orbit.periapsis_altitude)
            except Exception:
                return
            burn_alt = cur_apo if burn_at == "apoapsis" else cur_per
            opp = cur_per if burn_at == "apoapsis" else cur_apo
            t_to = (chaser.orbit.time_to_apoapsis if burn_at == "apoapsis"
                    else chaser.orbit.time_to_periapsis)
            dv = self._opposite_apsis_delta_v_mps(
                mu=mu, body_radius_m=body_radius, burn_altitude_m=burn_alt,
                current_opposite_altitude_m=opp, target_opposite_altitude_m=t_alt)
            if abs(dv) < 2.0:
                return
            # Scale the burn to its size: a LARGE match (e.g. lowering apoapsis from 600+ km) needs
            # full throttle and lots of time or it times out half-done; a SMALL tweak needs a low
            # throttle for precision or full throttle overshoots and oscillates until timeout.
            if abs(dv) > 60.0:
                mt, mb = 1.0, 400.0
            elif abs(dv) > 20.0:
                mt, mb = 0.45, 200.0
            else:
                mt, mb = 0.18, 150.0
            self._execute_mun_apsis_node(
                conn, chaser, recorder, start, timeout_s, phase=f"rendezvous_set_{set_attr}",
                node_ut=sc.ut + max(1.0, float(t_to)), prograde_delta_v_mps=dv,
                target_attr=set_attr, target_altitude_m=t_alt, max_burn_s=mb, max_throttle=mt)

        # 1. Set the periapsis to the target's altitude (the close approach is at periapsis). Leave
        #    the apoapsis to the phasing step below, which raises it — lowering then re-raising it
        #    would waste fuel and time.
        set_apsis("periapsis_altitude", "apoapsis")
        self._record_live_sample(chaser, recorder, start, "rendezvous_orbit_matched",
                                 {"distance_m": self._relative_distance_m(chaser, target)})

        # 2. Phasing orbit: RAISE the apoapsis a lot (burn prograde at periapsis) so the chaser's
        #    period differs strongly from the target's and the relative phase sweeps fast. A low
        #    phasing orbit is capped by the atmosphere (gentle, too-slow drift); a high one is not.
        #    The periapsis stays at the target's altitude, so a phase-aligned periapsis pass is the
        #    close approach.
        phase_apo = t_alt + max(180_000.0, t_alt * 1.5)
        try:
            cur_per = float(chaser.orbit.periapsis_altitude)
            dv = self._opposite_apsis_delta_v_mps(
                mu=mu, body_radius_m=body_radius, burn_altitude_m=cur_per,
                current_opposite_altitude_m=float(chaser.orbit.apoapsis_altitude),
                target_opposite_altitude_m=phase_apo)
            if dv > 2.0:
                self._execute_mun_apsis_node(
                    conn, chaser, recorder, start, timeout_s, phase="rendezvous_phasing_orbit",
                    node_ut=sc.ut + max(1.0, float(chaser.orbit.time_to_periapsis)),
                    prograde_delta_v_mps=dv, target_attr="apoapsis_altitude",
                    target_altitude_m=phase_apo, max_burn_s=150.0, max_throttle=0.9)
        except Exception:
            pass

        # 3. Drift and pounce: the close approach happens at PERIAPSIS (the chaser's periapsis sits at
        #    the target's altitude). Warp to just before each periapsis and step in real time through
        #    the pass — a fixed-fraction warp would skip the brief periapsis where the orbits actually
        #    meet. The strong phasing sweeps the phase fast, so a periapsis pass lands inside
        #    proximity range within a few orbits; then null the relative velocity and hand to RCS.
        best = 1.0e12
        while time.monotonic() - start < timeout_s:
            d = self._relative_distance_m(chaser, target)
            best = min(best, d)
            try:
                t_peri = float(chaser.orbit.time_to_periapsis)
            except Exception:
                t_peri = 0.0
            self._record_live_sample(chaser, recorder, start, "rendezvous_phasing_drift",
                                     {"distance_m": d, "best_m": best, "t_peri_s": t_peri})
            if d < 3_500.0:
                self._set_physics_warp(conn, 0)
                self._null_relative_velocity(conn, chaser, target, recorder, start)
                self._record_live_sample(chaser, recorder, start, "rendezvous_close_approach",
                                         {"distance_m": self._relative_distance_m(chaser, target)})
                return True
            try:
                if t_peri < 45.0 or d < 10_000.0:
                    # near (or approaching) a periapsis pass: watch in real time so we catch the min
                    self._set_physics_warp(conn, 0)
                    time.sleep(0.5)
                else:
                    # skip ahead to just before the next periapsis (the close-approach point)
                    sc.warp_to(sc.ut + max(10.0, t_peri - 25.0),
                               max_rails_rate=100_000.0, max_physics_rate=4.0)
            except Exception:
                time.sleep(2.0)
        self._set_physics_warp(conn, 0)
        return self._relative_distance_m(chaser, target) < 5_000.0

    def _null_relative_velocity(self, conn, chaser, target, recorder, start, max_s: float = 90.0) -> None:
        """Burn the main engine to null the chaser's velocity relative to the target. Point along the
        target's relative-velocity vector (= match the target's velocity) and burn until the relative
        speed is small."""
        ref = chaser.orbital_reference_frame
        ap = chaser.auto_pilot
        chaser.control.rcs = True
        t0 = time.monotonic()
        while time.monotonic() - t0 < max_s and time.monotonic() - start < self._dock_deadline(start):
            try:
                relv = target.velocity(ref)
                speed = (relv[0] ** 2 + relv[1] ** 2 + relv[2] ** 2) ** 0.5
            except Exception:
                break
            self._record_live_sample(chaser, recorder, start, "rendezvous_null_rel_velocity",
                                     {"rel_speed_mps": speed,
                                      "distance_m": self._relative_distance_m(chaser, target)})
            if speed < 4.0:
                break
            try:
                ap.reference_frame = ref
                ap.target_direction = relv  # accelerate toward the target's velocity -> null relative
                ap.engage()
            except Exception:
                pass
            chaser.control.throttle = min(1.0, max(0.1, speed / 25.0))
            time.sleep(0.3)
        chaser.control.throttle = 0.0

    @staticmethod
    def _dock_deadline(start: float) -> float:
        return 1.0e9  # rendezvous uses its own per-step timeout; this guard is effectively off

    def _approach_and_dock(self, conn, chaser, target, recorder, start, timeout_s) -> bool:
        """Proximity ops: point at the target, translate in with RCS, null lateral drift, mate ports."""
        sc = conn.space_center
        try:
            chaser_port = self._first_docking_port(chaser)
            target_port = self._first_docking_port(target)
        except Exception:
            chaser_port = target_port = None
        ap = chaser.auto_pilot
        last_phase = ""
        while time.monotonic() - start < timeout_s:
            # Docked? (the chaser's port reports docked, or the vessel gained the target's parts)
            try:
                if chaser_port is not None and str(chaser_port.state).lower().endswith("docked"):
                    self._record_live_sample(chaser, recorder, start, "dock_ports_mated")
                    return True
            except Exception:
                pass
            # Relative position/velocity of the target in the chaser's reference frame.
            ref = chaser.reference_frame
            try:
                rel_pos = target.position(ref)
                rel_vel = target.velocity(ref)
            except Exception:
                return False
            distance = (rel_pos[0] ** 2 + rel_pos[1] ** 2 + rel_pos[2] ** 2) ** 0.5
            # Point the chaser docking axis at the target.
            try:
                ap.reference_frame = ref
                ap.target_direction = rel_pos
                ap.engage()
            except Exception:
                pass
            # Desired closing speed: slow when near, faster when far (cap ~3 m/s).
            v_des = max(0.3, min(3.0, distance * 0.08))
            unit = [c / distance for c in rel_pos] if distance > 1e-3 else [0.0, 0.0, 0.0]
            # Command translation = drive relative velocity toward (unit * v_des). control axes:
            # forward=+y(nose), right=+x, up=+z in the vessel frame; gains kept gentle.
            err = [unit[i] * v_des - rel_vel[i] for i in range(3)]
            chaser.control.right = max(-1.0, min(1.0, err[0] * 0.5))
            chaser.control.up = max(-1.0, min(1.0, err[2] * 0.5))
            chaser.control.forward = max(-1.0, min(1.0, err[1] * 0.5))
            phase = "dock_final_approach" if distance < 50 else "dock_closing"
            if phase != last_phase:
                last_phase = phase
            self._record_live_sample(
                chaser, recorder, start, phase,
                {"distance_m": distance, "closing_speed_mps": v_des},
            )
            if distance < 0.4:
                return True
            time.sleep(0.25)
        return False

    @staticmethod
    def _first_docking_port(vessel):
        ports = vessel.parts.docking_ports
        return ports[0] if ports else None

    def _undock_after_transfer(self, conn, chaser, recorder, start) -> None:
        try:
            port = self._first_docking_port(chaser)
            if port is not None:
                port.undock()
                self._record_live_sample(chaser, recorder, start, "dock_undocked")
                # small back-away pulse
                chaser.control.rcs = True
                chaser.control.forward = -0.5
                time.sleep(2.0)
                chaser.control.forward = 0.0
        except Exception:
            self._record_live_sample(chaser, recorder, start, "dock_undock_failed")

    def fly(
        self,
        mission: MissionSpec,
        design: RocketDesign,
        telemetry_path: str | Path,
        timeout_s: int = 900,
    ) -> TelemetrySummary:
        recorder = TelemetryRecorder(telemetry_path)
        conn = self._connect()
        vessel = conn.space_center.active_vessel
        preferred_name = design.name
        target_altitude = mission.target_orbit_m
        turn_start_altitude = 1_000.0
        turn_end_altitude = 40_000.0
        heading = 90.0

        self._set_physics_warp(conn, 0)
        vessel.control.sas = False
        vessel.control.rcs = False
        vessel.control.throttle = 1.0
        vessel.auto_pilot.engage()
        vessel.auto_pilot.target_pitch_and_heading(90, heading)
        time.sleep(1)
        self._start_launch_sequence(vessel)

        start = time.monotonic()
        last_stage_at = start
        phase = "ascent"
        try:
            while time.monotonic() - start < timeout_s:
                vessel = self._reacquire_vessel(conn, vessel, preferred_name)
                self._set_physics_warp(conn, 0)
                if phase == "ascent":
                    vessel.control.throttle = 1.0
                # Read flight in the body's (rotating) reference frame. The default frame is the
                # vessel's own surface frame, which is centred on and co-moving with the vessel, so
                # vertical_speed/speed read ~0 the whole ascent (this zeroed the telemetry and
                # false-triggered the stuck-on-pad guard). The landing/capture phases already use
                # vessel.flight(vessel.orbit.body.reference_frame); the ascent must too.
                flight = vessel.flight(vessel.orbit.body.reference_frame)
                altitude = float(flight.mean_altitude)
                apoapsis = float(vessel.orbit.apoapsis_altitude)
                periapsis = float(vessel.orbit.periapsis_altitude)
                elapsed = time.monotonic() - start
                stage_status = self._stage_status(vessel)
                if altitude > turn_start_altitude and altitude < turn_end_altitude:
                    frac = (altitude - turn_start_altitude) / (turn_end_altitude - turn_start_altitude)
                    pitch = 90.0 - frac * 90.0
                    vessel.auto_pilot.target_pitch_and_heading(pitch, heading)
                elif altitude >= turn_end_altitude:
                    vessel.auto_pilot.target_pitch_and_heading(0, heading)

                if apoapsis > target_altitude * 1.10 and phase == "ascent":
                    phase = "coast_to_apoapsis"
                    vessel.control.throttle = 0.0
                    break

                recorder.append(
                    {
                        "elapsed_s": elapsed,
                        "phase": phase,
                        "altitude_m": altitude,
                        "apoapsis_m": apoapsis,
                        "periapsis_m": periapsis,
                        "fuel_fraction_left": self._fuel_fraction(vessel),
                        "vertical_speed_mps": float(flight.vertical_speed),
                        "surface_speed_mps": float(flight.speed),
                        "pitch_deg": float(flight.pitch),
                        "current_stage": int(vessel.control.current_stage),
                        "available_thrust_n": float(vessel.available_thrust),
                        "actual_thrust_n": float(vessel.thrust),
                        "throttle": float(vessel.control.throttle),
                        "active_engines": stage_status["active_engines"],
                        "fueled_active_engines": stage_status["fueled_active_engines"],
                        "dead_active_engines": stage_status["dead_active_engines"],
                        "situation": str(vessel.situation),
                    }
                )
                # Genuinely-stuck detection: gained no orbital energy and not moving. Keyed on
                # apoapsis (frame-independent, reliable) rather than mean_altitude, so it cannot
                # false-fire on a transient altitude read while the craft is actually climbing.
                if elapsed > 30 and apoapsis < 1000.0 and abs(float(flight.vertical_speed)) < 2.0 and float(flight.speed) < 2.0:
                    phase = "ascent_stuck_on_pad"
                    self._record_live_sample(
                        vessel,
                        recorder,
                        start,
                        phase,
                        {"actual_thrust_n": float(vessel.thrust), "throttle": float(vessel.control.throttle)},
                    )
                    break
                if elapsed > 5 and self._can_stage_now(last_stage_at) and self._should_stage(vessel, stage_status):
                    vessel.control.activate_next_stage()
                    vessel.control.throttle = 1.0
                    last_stage_at = time.monotonic()
                    vessel = self._reacquire_vessel(conn, vessel, preferred_name)
                time.sleep(0.25)

            if phase == "coast_to_apoapsis":
                self._set_physics_warp(conn, 0)
                circularization_phase = self._circularize_simple(
                    conn,
                    vessel,
                    target_altitude,
                    recorder,
                    start,
                    timeout_s,
                    preferred_name,
                )
                vessel = self._reacquire_vessel(conn, vessel, preferred_name)
                if circularization_phase != "circularized":
                    return recorder.summarize()
                if vessel.orbit.periapsis_altitude >= 70_000 and mission.mission_type == "artemis_hls_predeploy":
                    self._model_orbital_refuel(vessel, recorder, start)
                if vessel.orbit.periapsis_altitude >= 70_000 and mission.mission_type == "mun_landing_return":
                    self._fly_mun_profile(conn, vessel, recorder, start, timeout_s)
                elif vessel.orbit.periapsis_altitude >= 70_000 and mission.mission_type == "artemis_hls_predeploy":
                    self._fly_mun_orbit_only(
                        conn,
                        vessel,
                        recorder,
                        start,
                        timeout_s,
                        success_phase="artemis_hls_parked_in_mun_orbit",
                    )
                elif vessel.orbit.periapsis_altitude >= 70_000 and mission.mission_type == "artemis_mun_relay":
                    self._fly_mun_relay_profile(conn, vessel, recorder, start, timeout_s)
                elif vessel.orbit.periapsis_altitude >= 70_000 and mission.mission_type == "artemis_orion_mun_orbit_only":
                    self._fly_mun_orbit_only(
                        conn,
                        vessel,
                        recorder,
                        start,
                        timeout_s,
                        success_phase="artemis_orion_waiting_in_mun_orbit",
                        transfer_profile="orion_free_return",
                    )
                elif vessel.orbit.periapsis_altitude >= 70_000 and mission.mission_type == "artemis_orion_sls_return":
                    if self._transfer_and_capture_mun_orbit(
                        conn,
                        vessel,
                        recorder,
                        start,
                        timeout_s,
                        transfer_profile="orion_free_return",
                    ):
                        self._record_live_sample(vessel, recorder, start, "artemis_orion_rendezvous_equivalent")
                        self._return_to_kerbin_from_mun_orbit(conn, vessel, recorder, start, timeout_s)
        finally:
            self._set_physics_warp(conn, 0)
            try:
                vessel.auto_pilot.disengage()
            except Exception:
                pass
        return recorder.summarize()

    def _should_stage(self, vessel, stage_status: dict[str, int | float | bool] | None = None) -> bool:
        stage_status = stage_status or self._stage_status(vessel)
        stage = int(stage_status["current_stage"])
        next_engine_stage = self._next_fueled_inactive_engine_stage(vessel, stage)
        if (
            next_engine_stage is not None
            and stage_status["available_thrust_n"] < 1.0
            and stage >= next_engine_stage
        ):
            return True
        if stage <= int(self.config.get("min_autostage_stage", 1)):
            return False
        if stage_status["available_thrust_n"] < 1.0:
            return True
        if stage_status["dead_active_engines"] > 0:
            return True
        if stage_status["active_engines"] == 0 and stage_status["available_thrust_n"] < 1.0:
            return True
        if stage_status["active_engines"] > 0 and stage_status["fueled_active_engines"] == 0:
            return True
        return False

    @staticmethod
    def _next_fueled_inactive_engine_stage(vessel, current_stage: int) -> int | None:
        stages: list[int] = []
        try:
            engines = vessel.parts.engines
        except Exception:
            return None
        for engine in engines:
            try:
                if engine.active:
                    continue
            except Exception:
                pass
            try:
                stage = int(engine.part.stage)
            except Exception:
                continue
            if stage < 0 or stage > current_stage:
                continue
            has_fuel = False
            try:
                has_fuel = bool(engine.has_fuel)
            except Exception:
                has_fuel = True
            if has_fuel:
                stages.append(stage)
        return max(stages) if stages else None

    @staticmethod
    def _stage_status(vessel) -> dict[str, int | float | bool]:
        active_engines = 0
        fueled_active_engines = 0
        dead_active_engines = 0
        for engine in vessel.parts.engines:
            if not engine.active:
                continue
            active_engines += 1
            has_fuel = False
            try:
                has_fuel = bool(engine.has_fuel)
            except Exception:
                pass
            try:
                has_fuel = has_fuel or float(engine.available_thrust) > 1.0
            except Exception:
                pass
            try:
                has_fuel = has_fuel or float(engine.thrust) > 1.0
            except Exception:
                pass
            if has_fuel:
                fueled_active_engines += 1
            else:
                dead_active_engines += 1
        return {
            "current_stage": int(vessel.control.current_stage),
            "available_thrust_n": float(vessel.available_thrust),
            "active_engines": active_engines,
            "fueled_active_engines": fueled_active_engines,
            "dead_active_engines": dead_active_engines,
        }

    @staticmethod
    def _can_stage_now(last_stage_at: float) -> bool:
        return time.monotonic() - last_stage_at > 1.5

    def _target_local_up(self, vessel) -> None:
        body_frame = vessel.orbit.body.reference_frame
        position = tuple(float(value) for value in vessel.position(body_frame))
        norm = max(1.0e-6, self._norm(position))
        vessel.auto_pilot.reference_frame = body_frame
        vessel.auto_pilot.target_direction = tuple(value / norm for value in position)
        vessel.auto_pilot.engage()

    @staticmethod
    def _mun_ascent_pitch(surface_altitude: float, apoapsis: float, target_apoapsis: float, vertical_speed: float) -> float:
        if surface_altitude < 120.0 or (surface_altitude < 350.0 and vertical_speed < 25.0):
            return 90.0
        if apoapsis > target_apoapsis * 0.88:
            return 0.0
        if apoapsis > target_apoapsis * 0.70:
            return 5.0
        if apoapsis > target_apoapsis * 0.50:
            return 12.0
        if surface_altitude < 350.0:
            return 70.0
        if surface_altitude < 900.0:
            return 45.0
        if surface_altitude < 2_500.0:
            return 25.0
        return 10.0

    def _target_surface_pitch_heading(self, vessel, pitch: float, heading: float = 90.0) -> None:
        try:
            vessel.auto_pilot.reference_frame = vessel.surface_reference_frame
        except Exception:
            pass
        vessel.auto_pilot.target_pitch_and_heading(float(pitch), float(heading))
        vessel.auto_pilot.engage()

    def _point_at_node(self, vessel, node, align_timeout_s: float = 0.0, invert: bool = False) -> bool:
        # Always steer with the AUTOPILOT toward the node burn vector and WAIT for real alignment.
        # The old non-inverted shortcut (SAS maneuver + a fixed <=4 s sleep) returned "aligned"
        # without checking, so heavy craft (e.g. the 901 t MUNSHIP) ignited the burn still rotating
        # -> wrong-direction deorbit/apsis burns. The autopilot path below checks auto_pilot.error.
        try:
            reference_frame = vessel.orbital_reference_frame
            vector = tuple(float(value) for value in node.remaining_burn_vector(reference_frame))
            norm = self._norm(vector)
            if norm < 1.0:
                vector = tuple(float(value) for value in node.burn_vector(reference_frame))
                norm = self._norm(vector)
            if norm < 1.0:
                return False
            direction = tuple((-value if invert else value) / norm for value in vector)
            vessel.auto_pilot.reference_frame = reference_frame
            vessel.auto_pilot.target_direction = direction
            vessel.auto_pilot.engage()
        except Exception:
            return False

        deadline = time.monotonic() + max(0.0, float(align_timeout_s))
        while time.monotonic() < deadline:
            try:
                if float(vessel.auto_pilot.error) <= float(self.config.get("node_alignment_error_deg", 6.0)):
                    return True
            except Exception:
                pass
            time.sleep(0.25)
        if align_timeout_s <= 0.0:
            return True
        try:
            return float(vessel.auto_pilot.error) <= float(self.config.get("node_alignment_error_deg", 6.0))
        except Exception:
            return False

    def _point_orbital_prograde(self, vessel, align_timeout_s: float = 0.0, invert: bool = False) -> bool:
        try:
            vessel.auto_pilot.reference_frame = vessel.orbital_reference_frame
            vessel.auto_pilot.target_direction = (0, -1 if invert else 1, 0)
            vessel.auto_pilot.engage()
        except Exception:
            return False

        deadline = time.monotonic() + max(0.0, float(align_timeout_s))
        while time.monotonic() < deadline:
            try:
                if float(vessel.auto_pilot.error) <= float(self.config.get("node_alignment_error_deg", 6.0)):
                    return True
            except Exception:
                pass
            time.sleep(0.25)
        if align_timeout_s <= 0.0:
            return True
        try:
            return float(vessel.auto_pilot.error) <= float(self.config.get("node_alignment_error_deg", 6.0))
        except Exception:
            return False

    def _point_orbital_sas_prograde(self, vessel, align_timeout_s: float = 4.0) -> bool:
        try:
            from krpc.services import spacecenter

            try:
                vessel.auto_pilot.disengage()
            except Exception:
                pass
            vessel.control.sas = True
            vessel.control.speed_mode = self._speed_mode_value(spacecenter, "orbit", "orbital")
            vessel.control.sas_mode = spacecenter.SASMode.prograde
            time.sleep(max(0.0, min(4.0, float(align_timeout_s))))
            return True
        except Exception:
            return self._point_orbital_prograde(vessel, align_timeout_s=align_timeout_s)

    def _point_orbital_retrograde(self, vessel, align_timeout_s: float = 4.0) -> bool:
        try:
            from krpc.services import spacecenter

            try:
                vessel.auto_pilot.disengage()
            except Exception:
                pass
            vessel.control.sas = True
            vessel.control.speed_mode = self._speed_mode_value(spacecenter, "orbit", "orbital")
            vessel.control.sas_mode = spacecenter.SASMode.retrograde
            time.sleep(max(0.0, min(4.0, float(align_timeout_s))))
            return True
        except Exception:
            pass

        try:
            vessel.control.sas = False
        except Exception:
            pass
        try:
            vessel.auto_pilot.reference_frame = vessel.orbital_reference_frame
            # In the Moonship landing run, (0, -1, 0) raised periapsis.
            # Keep the fallback opposite and let the periapsis guard below verify it.
            vessel.auto_pilot.target_direction = (0, 1, 0)
            vessel.auto_pilot.engage()
        except Exception:
            return False
        return self._wait_for_autopilot_alignment(vessel, timeout_s=align_timeout_s)

    @staticmethod
    def _speed_mode_value(spacecenter, *names: str):
        for name in names:
            try:
                return getattr(spacecenter.SpeedMode, name)
            except AttributeError:
                continue
        raise AttributeError(f"None of the requested kRPC SpeedMode names exist: {', '.join(names)}")

    @staticmethod
    def _node_lateral_delta_v(node) -> float:
        total = 0.0
        for attr in ("radial", "normal"):
            try:
                value = float(getattr(node, attr))
            except Exception:
                value = 0.0
            total += value * value
        return math.sqrt(total)

    @staticmethod
    def _node_prograde_delta_v(node) -> float:
        try:
            return float(getattr(node, "prograde"))
        except Exception:
            return 0.0

    def _use_direct_prograde_correction(self, phase: str, node) -> bool:
        if phase != "mun_transfer_correction":
            return False
        lateral = self._node_lateral_delta_v(node)
        return lateral <= float(self.config.get("direct_correction_lateral_threshold_mps", 1.0))

    def _maneuver_node_throttle(
        self,
        phase: str,
        remaining_delta_v_mps: float,
        vessel_mass_kg: float,
        thrust_n: float,
    ) -> float:
        if remaining_delta_v_mps <= 0.0 or thrust_n <= 0.0 or vessel_mass_kg <= 0.0:
            return 0.0
        if phase == "mun_transfer_correction":
            acceleration = thrust_n / vessel_mass_kg
            target_acceleration = float(self.config.get("correction_target_accel_mps2", 0.10))
            throttle = target_acceleration / max(0.05, acceleration)
            if remaining_delta_v_mps < 4.0:
                throttle *= 0.5
            return min(
                float(self.config.get("correction_max_throttle", 0.025)),
                max(float(self.config.get("correction_min_throttle", 0.004)), throttle),
            )
        if remaining_delta_v_mps < 8.0:
            return 0.05
        if remaining_delta_v_mps < 25.0:
            return 0.25
        return 1.0

    def _tmi_apoapsis_cap_m(self, planned_safe_mun_transfer: bool) -> float:
        if planned_safe_mun_transfer:
            return float(self.config.get("tmi_planned_safe_apoapsis_cap_m", 22_000_000.0))
        return float(self.config.get("tmi_apoapsis_cap_m", 14_500_000.0))

    @staticmethod
    def _correction_closest_is_worsening(
        *,
        closest_approach_m: float,
        best_closest_approach_m: float,
        mun_soi_m: float,
        node_time_to_s: float,
        seconds_since_best: float,
        min_node_lag_s: float = 60.0,
        min_seconds_since_best_s: float = 18.0,
        worsening_margin_m: float = 120_000.0,
    ) -> bool:
        if not all(math.isfinite(value) for value in (closest_approach_m, best_closest_approach_m, mun_soi_m)):
            return False
        return (
            node_time_to_s < -abs(min_node_lag_s)
            and seconds_since_best >= min_seconds_since_best_s
            and closest_approach_m > mun_soi_m + worsening_margin_m
            and closest_approach_m > best_closest_approach_m + worsening_margin_m
        )

    @staticmethod
    def _opposite_apsis_delta_v_mps(
        *,
        mu: float,
        body_radius_m: float,
        burn_altitude_m: float,
        current_opposite_altitude_m: float,
        target_opposite_altitude_m: float,
    ) -> float:
        burn_radius = body_radius_m + burn_altitude_m
        current_opposite_radius = body_radius_m + current_opposite_altitude_m
        target_opposite_radius = body_radius_m + target_opposite_altitude_m
        if mu <= 0.0 or burn_radius <= 0.0 or current_opposite_radius <= 0.0 or target_opposite_radius <= 0.0:
            return 0.0
        current_a = (burn_radius + current_opposite_radius) / 2.0
        target_a = (burn_radius + target_opposite_radius) / 2.0
        current_speed = vis_viva_speed_mps(mu, burn_radius, current_a)
        target_speed = vis_viva_speed_mps(mu, burn_radius, target_a)
        return target_speed - current_speed

    def _wait_for_autopilot_alignment(self, vessel, timeout_s: float | None = None) -> bool:
        deadline = time.monotonic() + float(
            self.config.get("node_alignment_timeout_s", 20.0) if timeout_s is None else timeout_s
        )
        while time.monotonic() < deadline:
            try:
                if float(vessel.auto_pilot.error) <= float(self.config.get("node_alignment_error_deg", 6.0)):
                    return True
            except Exception:
                pass
            time.sleep(0.25)
        try:
            return float(vessel.auto_pilot.error) <= float(self.config.get("node_alignment_error_deg", 6.0))
        except Exception:
            return False

    @staticmethod
    def _still_prelaunch(vessel) -> bool:
        try:
            return str(vessel.situation).lower().endswith("pre_launch")
        except Exception:
            return False

    @staticmethod
    def _set_physics_warp(conn, factor: int) -> None:
        if int(factor) <= 0:
            try:
                conn.space_center.rails_warp_factor = 0
            except Exception:
                pass
        try:
            conn.space_center.physics_warp_factor = max(0, min(3, int(factor)))
        except Exception:
            pass

    @staticmethod
    def _fuel_fraction(vessel) -> float:
        total = 0.0
        amount = 0.0
        for part in getattr(vessel.parts, "all", []):
            try:
                part_resources = part.resources.all
            except Exception:
                continue
            for resource in part_resources:
                try:
                    if resource.name in ("LiquidFuel", "Oxidizer", "SolidFuel"):
                        amount += float(resource.amount)
                        total += float(resource.max)
                except Exception:
                    pass
        if total <= 0.0:
            resources = vessel.resources
            for resource in ("LiquidFuel", "Oxidizer", "SolidFuel"):
                if resources.has_resource(resource):
                    amount += resources.amount(resource)
                    total += resources.max(resource)
        return float(amount / total) if total else 0.0

    def _start_launch_sequence(self, vessel) -> None:
        """Ignite engines, then release clamps after thrust is available."""

        max_start_stages = int(self.config.get("max_launch_start_stages", 6))
        for _ in range(max_start_stages):
            vessel.control.throttle = 1.0
            status = self._stage_status(vessel)
            if status["fueled_active_engines"] > 0 and status["available_thrust_n"] > 1.0:
                if self._next_launch_clamp_stage(vessel, int(status["current_stage"])) is not None:
                    vessel.control.activate_next_stage()
                    vessel.control.throttle = 1.0
                    time.sleep(1.0)
                return
            if not self._still_prelaunch(vessel):
                return
            vessel.control.activate_next_stage()
            vessel.control.throttle = 1.0
            time.sleep(1.0)

    @staticmethod
    def _next_launch_clamp_stage(vessel, current_stage: int) -> int | None:
        try:
            parts = vessel.parts.all
        except Exception:
            return None
        clamp_stages: list[int] = []
        for part in parts:
            try:
                stage = int(part.stage)
                if stage > int(current_stage):
                    continue
            except Exception:
                continue
            fields = []
            for attr in ("name", "title", "tag"):
                try:
                    fields.append(str(getattr(part, attr)))
                except Exception:
                    pass
            text = " ".join(fields).lower()
            if "clamp" in text or ("launch" in text and "stability" in text):
                clamp_stages.append(stage)
        return max(clamp_stages) if clamp_stages else None

    def _circularize_simple(
        self,
        conn,
        vessel,
        target_altitude,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
        preferred_name: str = "",
    ) -> str:
        self._set_physics_warp(conn, 0)
        vessel.control.throttle = 0.0
        time.sleep(0.25)
        vessel.control.throttle = 0.0
        vessel.auto_pilot.reference_frame = vessel.orbital_reference_frame
        vessel.auto_pilot.target_direction = (0, 1, 0)
        time.sleep(1.0)

        last_unthrottleable_guard_at = 0.0
        while time.monotonic() - start < timeout_s:
            try:
                time_to_apoapsis = float(vessel.orbit.time_to_apoapsis)
            except Exception:
                time_to_apoapsis = float("nan")
            try:
                actual_thrust = float(vessel.thrust)
            except Exception:
                actual_thrust = 0.0
            escaping = str(vessel.situation).lower().endswith("escaping") or not math.isfinite(
                float(vessel.orbit.apoapsis_altitude)
            )
            if not math.isfinite(time_to_apoapsis) or escaping:
                vessel.control.throttle = 0.0
                self._set_physics_warp(conn, 0)
                self._record_live_sample(
                    vessel,
                    recorder,
                    start,
                    "coast_to_apoapsis_invalid_or_escape",
                    {"time_to_apoapsis_s": time_to_apoapsis, "actual_thrust_n": actual_thrust},
                )
                phase = "coast_to_apoapsis_invalid_or_escape"
                break
            if time_to_apoapsis <= 35.0:
                phase = "coast_to_apoapsis_complete"
                break

            vessel.control.throttle = 0.0
            if actual_thrust > float(self.config.get("coast_unthrottleable_thrust_n", 10_000.0)):
                self._set_physics_warp(conn, 0)
                if time.monotonic() - last_unthrottleable_guard_at > 5.0:
                    self._point_orbital_retrograde(vessel, align_timeout_s=3.0)
                    last_unthrottleable_guard_at = time.monotonic()
                self._record_live_sample(
                    vessel,
                    recorder,
                    start,
                    "coast_to_apoapsis_unthrottleable_thrust_guard",
                    {"time_to_apoapsis_s": time_to_apoapsis, "actual_thrust_n": actual_thrust},
                )
                time.sleep(0.25)
                continue

            self._set_physics_warp(conn, int(self.config.get("physics_warp_factor", 0)))
            flight = vessel.flight()
            recorder.append(
                {
                    "elapsed_s": time.monotonic() - start,
                    "phase": "coast_to_apoapsis",
                    "altitude_m": float(flight.mean_altitude),
                    "apoapsis_m": float(vessel.orbit.apoapsis_altitude),
                    "periapsis_m": float(vessel.orbit.periapsis_altitude),
                    "fuel_fraction_left": self._fuel_fraction(vessel),
                    "time_to_apoapsis_s": time_to_apoapsis,
                }
            )
            time.sleep(0.25)
        else:
            phase = "flight_timeout"

        if phase not in ("coast_to_apoapsis_complete",):
            vessel.control.throttle = 0.0
            return phase

        self._set_physics_warp(conn, 0)
        vessel.control.throttle = 1.0
        burn_start = time.monotonic()
        last_stage_at = burn_start
        while time.monotonic() - start < timeout_s:
            vessel = self._reacquire_vessel(conn, vessel, preferred_name)
            flight = vessel.flight()
            if vessel.orbit.periapsis_altitude >= 70_000:
                phase = "circularized"
                break
            if str(vessel.situation).lower().endswith("escaping") or not math.isfinite(
                float(vessel.orbit.time_to_apoapsis)
            ):
                phase = "circularization_escape_abort"
                break
            if self._fuel_fraction(vessel) <= 0.001 and vessel.available_thrust < 1.0:
                phase = "out_of_fuel_during_circularization"
                break
            if time.monotonic() - burn_start > 180:
                phase = "circularization_timeout"
                break
            stage_status = self._stage_status(vessel)
            if self._can_stage_now(last_stage_at) and self._should_stage(vessel, stage_status):
                vessel.control.activate_next_stage()
                last_stage_at = time.monotonic()
                vessel = self._reacquire_vessel(conn, vessel, preferred_name)
            recorder.append(
                {
                    "elapsed_s": time.monotonic() - start,
                    "phase": "circularization_burn",
                    "altitude_m": float(flight.mean_altitude),
                    "apoapsis_m": float(vessel.orbit.apoapsis_altitude),
                    "periapsis_m": float(vessel.orbit.periapsis_altitude),
                    "fuel_fraction_left": self._fuel_fraction(vessel),
                    "time_to_apoapsis_s": float(vessel.orbit.time_to_apoapsis),
                    "available_thrust_n": float(vessel.available_thrust),
                    "current_stage": int(vessel.control.current_stage),
                    "active_engines": stage_status["active_engines"],
                    "fueled_active_engines": stage_status["fueled_active_engines"],
                    "dead_active_engines": stage_status["dead_active_engines"],
                }
            )
            time.sleep(0.25)
        else:
            phase = "flight_timeout"
        vessel.control.throttle = 0.0
        flight = vessel.flight()
        recorder.append(
            {
                "elapsed_s": time.monotonic() - start,
                "phase": phase,
                "altitude_m": float(flight.mean_altitude),
                "apoapsis_m": float(vessel.orbit.apoapsis_altitude),
                "periapsis_m": float(vessel.orbit.periapsis_altitude),
                "fuel_fraction_left": self._fuel_fraction(vessel),
            }
        )
        return phase

    def _model_orbital_refuel(self, vessel, recorder: TelemetryRecorder, start: float) -> None:
        if not bool(self.config.get("model_orbital_refueling", False)):
            return
        if vessel.orbit.body.name != "Kerbin" or vessel.orbit.periapsis_altitude < 70_000:
            return

        before = self._fuel_fraction(vessel)
        try:
            result = self._bridge_refill_vessel(
                fraction=float(self.config.get("orbital_refuel_fraction", 1.0)),
                resources=str(
                    self.config.get(
                        "orbital_refuel_resources",
                        "LiquidFuel,Oxidizer,MonoPropellant,ElectricCharge,LqdHydrogen,LqdMethane,Methane,LqdOxygen",
                    )
                ),
            )
            time.sleep(0.5)
            self._record_live_sample(
                vessel,
                recorder,
                start,
                "modeled_orbital_refuel",
                {
                    "fuel_fraction_before": before,
                    "fuel_fraction_after": self._fuel_fraction(vessel),
                    "bridge_result": result,
                    "model": "Starship HLS orbital refueling represented by tank refill after verified Kerbin parking orbit.",
                },
            )
        except Exception as exc:
            self._record_live_sample(
                vessel,
                recorder,
                start,
                "modeled_orbital_refuel_failed",
                {
                    "fuel_fraction_before": before,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )

    def _bridge_refill_vessel(self, fraction: float = 1.0, resources: str = "", vessel_name: str = "") -> dict:
        from .bridge_client import BridgeClient

        bridge = BridgeClient(
            base_url=self.config.get("bridge_base_url", "http://127.0.0.1:48500"),
            timeout_s=int(self.config.get("bridge_timeout_s", 30)),
        )
        return bridge.refuel_vessel(
            vessel_name=vessel_name,
            fraction=float(fraction),
            resources=resources,
        )

    @staticmethod
    def _resource_fraction(vessel, resource_name: str) -> float:
        try:
            resources = vessel.resources
            if not resources.has_resource(resource_name):
                return 1.0
            maximum = float(resources.max(resource_name))
            if maximum <= 0.0:
                return 1.0
            return max(0.0, min(1.0, float(resources.amount(resource_name)) / maximum))
        except Exception:
            return 1.0

    def _fly_mun_profile(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
    ) -> None:
        if self._transfer_and_capture_mun_orbit(conn, vessel, recorder, start, timeout_s):
            if self._land_on_mun(conn, vessel, recorder, start, timeout_s):
                self._return_from_mun(conn, vessel, recorder, start, timeout_s)

    def _fly_mun_orbit_only(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
        success_phase: str,
        transfer_profile: str = "capture",
    ) -> bool:
        if self._transfer_and_capture_mun_orbit(conn, vessel, recorder, start, timeout_s, transfer_profile):
            self._record_live_sample(vessel, recorder, start, success_phase)

    def _fly_mun_relay_profile(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
    ) -> None:
        if self._transfer_and_capture_mun_orbit(conn, vessel, recorder, start, timeout_s, relay_capture=True):
            self._shape_mun_relay_orbit(conn, vessel, recorder, start, timeout_s)
            return True
        return False

    def _transfer_and_capture_mun_orbit(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
        transfer_profile: str = "capture",
        relay_capture: bool = False,
    ) -> bool:
        try:
            if vessel.orbit.body.name == "Kerbin" and float(vessel.orbit.periapsis_altitude) < 70_000.0:
                self._record_live_sample(
                    vessel,
                    recorder,
                    start,
                    "mun_transfer_requires_parking_orbit",
                    {"periapsis_m": float(vessel.orbit.periapsis_altitude)},
                )
                return False
        except Exception:
            pass
        self._record_live_sample(vessel, recorder, start, "mun_transfer_planning")
        try:
            node = self._find_mun_transfer_node(conn, vessel, recorder, start, transfer_profile=transfer_profile)
        except Exception as exc:
            self._record_live_sample(vessel, recorder, start, "mun_transfer_planning_failed", {"error": str(exc)})
            return False
        if node is None:
            self._record_live_sample(vessel, recorder, start, "mun_transfer_node_not_found")
            return False

        vessel = self._execute_node(
            conn,
            vessel,
            node,
            recorder,
            start,
            timeout_s,
            "trans_mun_injection",
            preferred_name=str(vessel.name),
        )
        self._coast_to_mun_soi(conn, vessel, recorder, start, timeout_s)
        if vessel.orbit.body.name == "Mun":
            # Finite-burn drift can leave a grazing SOI entry (periapsis far above the capture
            # gate). Lower it with an in-SOI mid-course burn before capture, leveraging the
            # abundant remaining fuel. See docs (2026-06-21 TMI targeting).
            self._correct_mun_soi_periapsis(conn, vessel, recorder, start, timeout_s, relay_capture=relay_capture)
            return self._capture_mun_orbit(conn, vessel, recorder, start, timeout_s, relay_capture=relay_capture)
        return False

    def _find_mun_transfer_node(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        transfer_profile: str = "capture",
    ):
        bodies = conn.space_center.bodies
        mun = bodies["Mun"]
        now = float(conn.space_center.ut)
        period = max(1_800.0, min(7_200.0, float(vessel.orbit.period)))
        seed = self._estimate_mun_transfer_seed(conn, vessel, mun, now, period)
        recorder.append(
            {
                "elapsed_s": time.monotonic() - start,
                "phase": "mun_transfer_seed",
                **seed,
            }
        )

        local_times = self._candidate_times(now, seed["launch_delay_s"], period, (-900, -600, -360, -180, 0, 180, 360, 600, 900))
        local_dv = self._candidate_values(seed["prograde_mps"], width=80.0, step=10.0, lower=740.0, upper=980.0)
        radial_values = (0.0, -40.0, 40.0)
        if transfer_profile != "capture":
            radial_values = (0.0, -40.0, 40.0, -80.0, 80.0)
        best = self._search_mun_transfer_grid(
            vessel,
            mun,
            local_times,
            local_dv,
            radial_values,
            transfer_profile=transfer_profile,
        )

        if best is None or not self._is_safe_mun_transfer_candidate(best, mun):
            # Search ~3 orbital periods ahead, not just one: the Mun phase angle is often not
            # favorable within the first period, which was the dominant cause of
            # mun_transfer_node_not_found (~half of predeploys). More candidate times = a transfer
            # window is found instead of failing and forcing a relaunch.
            coarse_times = [now + 120.0 + period * dt_index / 24.0 for dt_index in range(0, 73)]
            coarse_dv = self._candidate_values(seed["prograde_mps"], width=120.0, step=20.0, lower=740.0, upper=980.0)
            fallback = self._search_mun_transfer_grid(
                vessel,
                mun,
                coarse_times,
                coarse_dv,
                radial_values,
                transfer_profile=transfer_profile,
            )
            if fallback is not None and (best is None or float(fallback["score"]) < float(best["score"])):
                best = fallback

        if best is None:
            return None

        recorder.append(
            {
                "elapsed_s": time.monotonic() - start,
                "phase": "mun_transfer_node_selected",
                "node_ut": best["ut"],
                "prograde_mps": best["prograde"],
                "radial_mps": best.get("radial", 0.0),
                "closest_approach_m": best.get("closest_approach_m", -1),
                "mun_periapsis_m": best.get("mun_periapsis_m", -1),
                "encounter": best.get("encounter", False),
                "free_return": best.get("free_return", False),
                "free_return_kerbin_periapsis_m": best.get("free_return_kerbin_periapsis_m", -1),
                "transfer_profile": transfer_profile,
            }
        )
        if not self._is_safe_mun_transfer_candidate(best, mun):
            return None
        return vessel.control.add_node(float(best["ut"]), prograde=float(best["prograde"]), radial=float(best.get("radial", 0.0)))

    def _estimate_mun_transfer_seed(self, conn, vessel, mun, now: float, period: float) -> dict[str, float]:
        body = vessel.orbit.body
        try:
            ref = body.non_rotating_reference_frame
            vessel_position = tuple(float(value) for value in vessel.position(ref))
            mun_position = tuple(float(value) for value in mun.position(ref))
            mun_velocity = tuple(float(value) for value in mun.velocity(ref))
            origin_radius = max(float(body.equatorial_radius) + 70_000.0, self._norm(vessel_position))
            target_radius = max(origin_radius + 1_000.0, self._norm(mun_position))
            mu = float(body.gravitational_parameter)
            transfer_time = hohmann_transfer_time_s(mu, origin_radius, target_radius)
            prograde = hohmann_transfer_delta_v_mps(mu, origin_radius, target_radius)
            target_phase = outward_transfer_phase_angle_rad(mu, target_radius, transfer_time)
            current_phase = self._signed_angle_around_normal(
                vessel_position,
                mun_position,
                self._cross(mun_position, mun_velocity),
            )
            vessel_mean_motion = math.sqrt(mu / origin_radius**3)
            mun_mean_motion = math.sqrt(mu / target_radius**3)
            phase_rate = max(1.0e-6, vessel_mean_motion - mun_mean_motion)
            launch_delay = ((current_phase - target_phase) % (2.0 * math.pi)) / phase_rate
            while launch_delay < 120.0:
                launch_delay += (2.0 * math.pi) / phase_rate
            launch_delay = min(max(120.0, launch_delay), max(period * 3.0, 3_600.0))
            return {
                "launch_delay_s": float(launch_delay),
                "prograde_mps": float(max(740.0, min(980.0, prograde))),
                "transfer_time_s": float(transfer_time),
                "current_phase_deg": float(math.degrees(current_phase)),
                "target_phase_deg": float(math.degrees(target_phase)),
            }
        except Exception:
            return {
                "launch_delay_s": float(max(600.0, min(period * 0.75, 1_800.0))),
                "prograde_mps": 860.0,
                "transfer_time_s": 0.0,
                "current_phase_deg": -1.0,
                "target_phase_deg": -1.0,
            }

    @staticmethod
    def _candidate_times(now: float, seed_delay: float, period: float, offsets: tuple[int, ...]) -> list[float]:
        values = {round(now + max(120.0, seed_delay + float(offset)), 1) for offset in offsets}
        values.add(round(now + max(120.0, seed_delay), 1))
        return sorted(values)

    @staticmethod
    def _candidate_values(seed: float, *, width: float, step: float, lower: float, upper: float) -> list[float]:
        start = max(lower, seed - width)
        stop = min(upper, seed + width)
        count = int((stop - start) / step) + 1
        values = {round(start + index * step, 3) for index in range(count + 1)}
        values.add(round(max(lower, min(upper, seed)), 3))
        return sorted(value for value in values if lower <= value <= upper)

    def _search_mun_transfer_grid(
        self,
        vessel,
        mun,
        candidate_uts,
        prograde_values,
        radial_values,
        transfer_profile: str = "capture",
    ):
        best: dict[str, float | bool | str] | None = None
        best_score = float("inf")

        for ut in candidate_uts:
            for dv in prograde_values:
                for radial in radial_values:
                    node = vessel.control.add_node(float(ut), prograde=float(dv), radial=float(radial))
                    try:
                        candidate = self._score_mun_transfer_node(node, mun, transfer_profile=transfer_profile)
                        score = float(candidate["score"])
                        if score < best_score:
                            best_score = score
                            best = {"ut": float(ut), "prograde": float(dv), "radial": float(radial), **candidate}
                    finally:
                        try:
                            node.remove()
                        except Exception:
                            pass

        return best

    def _search_mun_correction_node(self, vessel, mun, candidate_uts, prograde_values, radial_values):
        best: dict[str, float | bool | str] | None = None
        best_score = float("inf")

        for ut in candidate_uts:
            for prograde in prograde_values:
                for radial in radial_values:
                    if abs(float(prograde)) < 0.01 and abs(float(radial)) < 0.01:
                        continue
                    node = vessel.control.add_node(float(ut), prograde=float(prograde), radial=float(radial))
                    try:
                        candidate = self._score_mun_transfer_node(node, mun)
                        if not self._is_safe_mun_transfer_candidate(
                            candidate,
                            mun,
                            min_kerbin_periapsis_m=float(
                                self.config.get("min_kerbin_transfer_periapsis_m", 70_000.0)
                            ),
                        ):
                            continue
                        delta_v = math.hypot(float(prograde), float(radial))
                        score = float(candidate["score"]) + delta_v * 1_000.0
                        if score < best_score:
                            best_score = score
                            best = {
                                "ut": float(ut),
                                "prograde": float(prograde),
                                "radial": float(radial),
                                "delta_v_mps": delta_v,
                                **candidate,
                            }
                    finally:
                        try:
                            node.remove()
                        except Exception:
                            pass

        return best

    @staticmethod
    def _is_safe_mun_transfer_candidate(
        candidate: dict[str, float | bool | str],
        mun,
        min_kerbin_periapsis_m: float = 0.0,
    ) -> bool:
        kerbin_periapsis = float(candidate.get("kerbin_periapsis_m", float("inf")))
        if math.isfinite(kerbin_periapsis) and kerbin_periapsis < min_kerbin_periapsis_m:
            return False
        if not candidate.get("encounter", False):
            return float(candidate.get("closest_approach_m", float("inf"))) <= float(mun.sphere_of_influence)
        periapsis = float(candidate.get("mun_periapsis_m", -1.0))
        return 35_000.0 <= periapsis <= 120_000.0

    @staticmethod
    def _norm(values: tuple[float, float, float]) -> float:
        return math.sqrt(sum(value * value for value in values))

    @staticmethod
    def _cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
        return (
            a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0],
        )

    @staticmethod
    def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
        return sum(left * right for left, right in zip(a, b))

    @classmethod
    def _project_onto_plane(
        cls,
        vector: tuple[float, float, float],
        normal: tuple[float, float, float],
    ) -> tuple[float, float, float]:
        normal_norm = max(1.0e-9, cls._norm(normal))
        unit = tuple(value / normal_norm for value in normal)
        projection = cls._dot(vector, unit)
        return tuple(value - projection * unit_value for value, unit_value in zip(vector, unit))

    @classmethod
    def _signed_angle_around_normal(
        cls,
        origin: tuple[float, float, float],
        target: tuple[float, float, float],
        normal: tuple[float, float, float],
    ) -> float:
        origin_plane = cls._project_onto_plane(origin, normal)
        target_plane = cls._project_onto_plane(target, normal)
        origin_norm = max(1.0e-9, cls._norm(origin_plane))
        target_norm = max(1.0e-9, cls._norm(target_plane))
        origin_unit = tuple(value / origin_norm for value in origin_plane)
        target_unit = tuple(value / target_norm for value in target_plane)
        normal_norm = max(1.0e-9, cls._norm(normal))
        normal_unit = tuple(value / normal_norm for value in normal)
        sin_angle = cls._dot(cls._cross(origin_unit, target_unit), normal_unit)
        cos_angle = cls._dot(origin_unit, target_unit)
        return math.atan2(sin_angle, cos_angle) % (2.0 * math.pi)

    @staticmethod
    def _score_mun_transfer_node(node, mun, transfer_profile: str = "capture") -> dict[str, float | bool | str]:
        closest = float("inf")
        try:
            closest = float(node.orbit.distance_at_closest_approach(mun.orbit))
        except Exception:
            pass

        encounter = False
        kerbin_periapsis = float("inf")
        mun_periapsis = float("inf")
        next_body = ""
        free_return = False
        free_return_kerbin_periapsis = float("inf")
        try:
            if str(node.orbit.body.name) == "Kerbin":
                kerbin_periapsis = float(node.orbit.periapsis_altitude)
        except Exception:
            pass
        try:
            next_orbit = node.orbit.next_orbit
            next_body = str(next_orbit.body.name)
            encounter = next_body == "Mun"
            if encounter:
                mun_periapsis = float(next_orbit.periapsis_altitude)
                try:
                    post_mun_orbit = next_orbit.next_orbit
                    post_mun_body = str(post_mun_orbit.body.name)
                    if post_mun_body == "Kerbin":
                        free_return_kerbin_periapsis = float(post_mun_orbit.periapsis_altitude)
                        free_return = -80_000.0 <= free_return_kerbin_periapsis <= 80_000.0
                except Exception:
                    pass
        except Exception:
            pass

        if encounter:
            target_periapsis = 60_000.0
            if 45_000.0 <= mun_periapsis <= 90_000.0:
                score = abs(mun_periapsis - target_periapsis)
            elif 20_000.0 <= mun_periapsis <= 140_000.0:
                score = abs(mun_periapsis - target_periapsis) + 250_000.0
            elif 12_000.0 <= mun_periapsis < 20_000.0:
                score = abs(mun_periapsis - target_periapsis) + 4_000_000.0
            elif mun_periapsis < 12_000.0:
                score = abs(mun_periapsis - target_periapsis) + 20_000_000.0
            else:
                score = abs(mun_periapsis - target_periapsis) + 1_000_000.0
            if transfer_profile == "orion_free_return":
                if free_return:
                    score += abs(free_return_kerbin_periapsis - 35_000.0) * 0.25
                else:
                    score += 1_000_000.0
        else:
            score = closest + 5_000_000.0
        return {
            "score": score,
            "encounter": encounter,
            "kerbin_periapsis_m": kerbin_periapsis,
            "closest_approach_m": closest,
            "mun_periapsis_m": mun_periapsis,
            "next_body": next_body,
            "free_return": free_return,
            "free_return_kerbin_periapsis_m": free_return_kerbin_periapsis,
        }

    def _execute_node(
        self,
        conn,
        vessel,
        node,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
        phase: str,
        preferred_name: str = "",
    ):
        vessel.control.throttle = 0.0
        planned_safe_mun_transfer = False
        if phase == "trans_mun_injection":
            try:
                next_orbit = node.orbit.next_orbit
                planned_safe_mun_transfer = (
                    str(next_orbit.body.name) == "Mun"
                    and 35_000.0 <= float(next_orbit.periapsis_altitude) <= 180_000.0
                )
            except Exception:
                planned_safe_mun_transfer = False
        tmi_lateral_delta_v = self._node_lateral_delta_v(node) if phase == "trans_mun_injection" else 0.0
        use_node_vector_for_tmi = (
            phase == "trans_mun_injection"
            and tmi_lateral_delta_v >= float(self.config.get("tmi_node_vector_lateral_threshold_mps", 5.0))
        )
        if phase == "trans_mun_injection":
            self._record_live_sample(
                vessel,
                recorder,
                start,
                "trans_mun_injection_steering",
                {
                    "steering_mode": "node_vector" if use_node_vector_for_tmi else "orbital_prograde",
                    "node_lateral_delta_v_mps": tmi_lateral_delta_v,
                    "planned_safe_mun_transfer": planned_safe_mun_transfer,
                },
            )

        def point_for_phase(align_timeout_s: float = 0.0, invert: bool = False) -> bool:
            if phase == "trans_mun_injection" and not use_node_vector_for_tmi:
                return self._point_orbital_prograde(vessel, align_timeout_s=align_timeout_s, invert=invert)
            if self._use_direct_prograde_correction(phase, node):
                prograde = self._node_prograde_delta_v(node)
                burn_prograde = prograde >= 0.0
                if invert:
                    burn_prograde = not burn_prograde
                if burn_prograde:
                    return self._point_orbital_sas_prograde(vessel, align_timeout_s=align_timeout_s)
                return self._point_orbital_retrograde(vessel, align_timeout_s=align_timeout_s)
            return self._point_at_node(vessel, node, align_timeout_s=align_timeout_s, invert=invert)

        point_for_phase(align_timeout_s=float(self.config.get("node_alignment_timeout_s", 20.0)))

        thrust = max(1.0, float(vessel.available_thrust))
        burn_time = min(900.0, max(5.0, burn_duration_s(float(vessel.mass), thrust, float(node.delta_v))))
        lead_time = finite_burn_lead_s(burn_time, settle_s=5.0, command_delay_s=1.0, min_lead_s=burn_time / 2.0 + 5.0)
        start_ut = float(node.ut) - lead_time
        if start_ut > conn.space_center.ut:
            conn.space_center.warp_to(start_ut, max_rails_rate=100_000.0, max_physics_rate=4.0)
        point_for_phase(align_timeout_s=float(self.config.get("node_alignment_timeout_s", 20.0)))
        last_vector_update_at = time.monotonic()
        while node.time_to > burn_time / 2.0 and time.monotonic() - start < timeout_s:
            if time.monotonic() - last_vector_update_at > 2.0:
                point_for_phase()
                last_vector_update_at = time.monotonic()
            self._record_live_sample(vessel, recorder, start, f"{phase}_waiting", {"node_time_to_s": float(node.time_to)})
            time.sleep(0.25)

        point_for_phase(align_timeout_s=float(self.config.get("node_alignment_timeout_s", 20.0)))
        vessel.control.throttle = self._maneuver_node_throttle(
            phase,
            float(node.delta_v),
            float(vessel.mass),
            thrust,
        )
        last_stage_at = time.monotonic()
        last_progress_remaining = float("inf")
        no_progress_since = time.monotonic()
        burn_started = time.monotonic()
        burn_direction_flipped = False
        direction_flip_at = 0.0
        direction_flip_remaining = float("inf")
        tmi_realign_count = 0
        tmi_realign_at = 0.0
        initial_remaining = float("inf")
        initial_apoapsis = float("nan")
        correction_best_closest = float("inf")
        correction_best_closest_at = time.monotonic()
        min_kerbin_periapsis = float(self.config.get("min_kerbin_transfer_periapsis_m", 70_000.0))
        while time.monotonic() - start < timeout_s:
            vessel = self._reacquire_vessel(conn, vessel, preferred_name)
            if time.monotonic() - last_vector_update_at > 1.0:
                point_for_phase(invert=burn_direction_flipped)
                last_vector_update_at = time.monotonic()
            remaining = float(node.remaining_delta_v)
            if not math.isfinite(initial_remaining):
                initial_remaining = remaining
            try:
                current_apoapsis = float(vessel.orbit.apoapsis_altitude)
            except Exception:
                current_apoapsis = float("nan")
            try:
                current_periapsis = float(vessel.orbit.periapsis_altitude)
            except Exception:
                current_periapsis = float("nan")
            if not math.isfinite(initial_apoapsis):
                initial_apoapsis = current_apoapsis
            if (
                phase == "mun_transfer_correction"
                and str(vessel.orbit.body.name) == "Kerbin"
                and math.isfinite(current_periapsis)
                and current_periapsis < min_kerbin_periapsis
            ):
                vessel.control.throttle = 0.0
                self._record_live_sample(
                    vessel,
                    recorder,
                    start,
                    "mun_transfer_correction_reentry_abort",
                    {
                        "remaining_delta_v_mps": remaining,
                        "node_time_to_s": float(node.time_to),
                        "periapsis_m": current_periapsis,
                        "min_kerbin_periapsis_m": min_kerbin_periapsis,
                    },
                )
                break
            if phase == "mun_transfer_correction":
                try:
                    correction_time_to_soi = float(vessel.orbit.time_to_soi_change)
                except Exception:
                    correction_time_to_soi = float("inf")
                correction_body = ""
                correction_periapsis = float("inf")
                if math.isfinite(correction_time_to_soi) and correction_time_to_soi > 0.0:
                    try:
                        correction_next_orbit = vessel.orbit.next_orbit
                        correction_body = str(correction_next_orbit.body.name)
                        correction_periapsis = float(correction_next_orbit.periapsis_altitude)
                    except Exception:
                        correction_body = ""
                        correction_periapsis = float("inf")
                if correction_body == "Mun" and 35_000.0 <= correction_periapsis <= 180_000.0:
                    vessel.control.throttle = 0.0
                    self._record_live_sample(
                        vessel,
                        recorder,
                        start,
                        "mun_transfer_correction_encounter_restored",
                        {
                            "remaining_delta_v_mps": remaining,
                            "node_time_to_s": float(node.time_to),
                            "mun_periapsis_m": correction_periapsis,
                            "time_to_soi_change_s": correction_time_to_soi,
                        },
                    )
                    break
                try:
                    mun = conn.space_center.bodies["Mun"]
                    mun_soi = float(mun.sphere_of_influence)
                    correction_closest = float(vessel.orbit.distance_at_closest_approach(mun.orbit))
                except Exception:
                    mun_soi = float("inf")
                    correction_closest = float("inf")
                if math.isfinite(correction_closest):
                    if correction_closest < correction_best_closest - float(
                        self.config.get("correction_closest_improvement_m", 25_000.0)
                    ):
                        correction_best_closest = correction_closest
                        correction_best_closest_at = time.monotonic()
                    if correction_closest <= mun_soi:
                        vessel.control.throttle = 0.0
                        self._record_live_sample(
                            vessel,
                            recorder,
                            start,
                            "mun_transfer_correction_closest_restored",
                            {
                                "remaining_delta_v_mps": remaining,
                                "node_time_to_s": float(node.time_to),
                                "closest_approach_m": correction_closest,
                                "mun_soi_m": mun_soi,
                            },
                        )
                        break
                    if self._correction_closest_is_worsening(
                        closest_approach_m=correction_closest,
                        best_closest_approach_m=correction_best_closest,
                        mun_soi_m=mun_soi,
                        node_time_to_s=float(node.time_to),
                        seconds_since_best=time.monotonic() - correction_best_closest_at,
                        min_node_lag_s=float(self.config.get("correction_bad_trend_min_node_lag_s", 60.0)),
                        min_seconds_since_best_s=float(self.config.get("correction_bad_trend_grace_s", 18.0)),
                        worsening_margin_m=float(self.config.get("correction_bad_trend_margin_m", 120_000.0)),
                    ):
                        vessel.control.throttle = 0.0
                        self._record_live_sample(
                            vessel,
                            recorder,
                            start,
                            "mun_transfer_correction_closest_worsened_abort",
                            {
                                "remaining_delta_v_mps": remaining,
                                "node_time_to_s": float(node.time_to),
                                "closest_approach_m": correction_closest,
                                "best_closest_approach_m": correction_best_closest,
                                "mun_soi_m": mun_soi,
                            },
                        )
                        break
                max_correction_apoapsis = float(self.config.get("correction_max_kerbin_apoapsis_m", 22_000_000.0))
                if (
                    math.isfinite(current_apoapsis)
                    and current_apoapsis > max_correction_apoapsis
                    and correction_body != "Mun"
                ):
                    vessel.control.throttle = 0.0
                    self._record_live_sample(
                        vessel,
                        recorder,
                        start,
                        "mun_transfer_correction_apoapsis_abort",
                        {
                            "remaining_delta_v_mps": remaining,
                            "node_time_to_s": float(node.time_to),
                            "apoapsis_m": current_apoapsis,
                            "max_apoapsis_m": max_correction_apoapsis,
                        },
                    )
                    break
                divergence_limit = max(
                    float(self.config.get("correction_divergence_floor_mps", 5.0)),
                    float(node.delta_v) * float(self.config.get("correction_divergence_factor", 1.8)),
                )
                if time.monotonic() - burn_started > 1.5 and remaining > initial_remaining + divergence_limit:
                    vessel.control.throttle = 0.0
                    self._record_live_sample(
                        vessel,
                        recorder,
                        start,
                        "mun_transfer_correction_diverged_abort",
                        {
                            "remaining_delta_v_mps": remaining,
                            "node_time_to_s": float(node.time_to),
                            "initial_remaining_delta_v_mps": initial_remaining,
                            "divergence_limit_mps": divergence_limit,
                        },
                    )
                    break
            if remaining < 1.0:
                break
            stage_status = self._stage_status(vessel)
            if stage_status["available_thrust_n"] < 1.0 or stage_status["fueled_active_engines"] == 0:
                # The active stage is dry, but a lower stage may still hold fuel (common with
                # template-seeded craft whose launch stage empties before the transfer stage).
                # Drop the spent stage and ignite the next fueled stage before giving up; only
                # declare out-of-fuel when there is genuinely no fueled stage left to stage into.
                # Without this, a node burn strands fuel sitting in decoupled lower stages
                # (observed: relay TMI quit at 159 km apoapsis with 30% fuel full in the Terrier
                # stages). See docs "Launch Blocker"/relay TMI notes (2026-06-21).
                if self._can_stage_now(last_stage_at) and self._should_stage(vessel, stage_status):
                    vessel.control.activate_next_stage()
                    last_stage_at = time.monotonic()
                    vessel = self._reacquire_vessel(conn, vessel, preferred_name)
                    time.sleep(0.5)
                    point_for_phase(invert=burn_direction_flipped)
                    vessel.control.throttle = self._maneuver_node_throttle(
                        phase,
                        remaining,
                        float(vessel.mass),
                        max(1.0, float(vessel.available_thrust)),
                    )
                    self._record_live_sample(
                        vessel,
                        recorder,
                        start,
                        f"{phase}_staged_to_continue_burn",
                        {
                            "remaining_delta_v_mps": remaining,
                            "available_thrust_n": float(vessel.available_thrust),
                        },
                    )
                    continue
                self._record_live_sample(
                    vessel,
                    recorder,
                    start,
                    f"{phase}_out_of_fuel",
                    {
                        "remaining_delta_v_mps": remaining,
                        "node_time_to_s": float(node.time_to),
                        "active_engines": stage_status["active_engines"],
                        "fueled_active_engines": stage_status["fueled_active_engines"],
                        "dead_active_engines": stage_status["dead_active_engines"],
                    },
                )
                break
            if (
                phase == "trans_mun_injection"
                and time.monotonic() - burn_started > 2.5
                and time.monotonic() - tmi_realign_at > 4.0
                and math.isfinite(current_apoapsis)
                and math.isfinite(initial_apoapsis)
                and current_apoapsis < initial_apoapsis - 2_000.0
            ):
                # A prograde TMI burn can only RAISE apoapsis; a falling apoapsis means the heavy
                # stack started the burn off-prograde (weak attitude control). Cut throttle, fully
                # re-align to prograde, then resume at low throttle so the engine gimbal can hold
                # attitude. Do NOT flip the burn direction (the node is prograde) and never drive
                # the orbit into reentry. Abort safely after a few failed re-aligns.
                vessel.control.throttle = 0.0
                aligned = point_for_phase(
                    align_timeout_s=float(self.config.get("node_alignment_timeout_s", 20.0))
                )
                tmi_realign_count += 1
                tmi_realign_at = time.monotonic()
                self._record_live_sample(
                    vessel,
                    recorder,
                    start,
                    "trans_mun_injection_realign",
                    {
                        "realign_count": tmi_realign_count,
                        "aligned": bool(aligned),
                        "apoapsis_m": current_apoapsis,
                        "initial_apoapsis_m": initial_apoapsis,
                    },
                )
                if tmi_realign_count >= 3:
                    vessel.control.throttle = 0.0
                    self._record_live_sample(
                        vessel,
                        recorder,
                        start,
                        "trans_mun_injection_misaligned_abort",
                        {"apoapsis_m": float(vessel.orbit.apoapsis_altitude)},
                    )
                    break
                vessel.control.throttle = 0.15  # low throttle: gimbal authority while re-acquiring prograde
                time.sleep(2.0)
                initial_apoapsis = float(vessel.orbit.apoapsis_altitude)  # reset baseline after re-align
                continue
            if (
                phase == "trans_mun_injection"
                and str(vessel.orbit.body.name) == "Kerbin"
                and math.isfinite(current_periapsis)
                and current_periapsis < min_kerbin_periapsis - 5_000.0
            ):
                # Safety: a TMI burn must never lower Kerbin periapsis into reentry.
                vessel.control.throttle = 0.0
                self._record_live_sample(
                    vessel,
                    recorder,
                    start,
                    "trans_mun_injection_periapsis_guard_abort",
                    {"periapsis_m": current_periapsis, "min_periapsis_m": min_kerbin_periapsis},
                )
                break
            if remaining < last_progress_remaining - 1.0:
                last_progress_remaining = remaining
                no_progress_since = time.monotonic()
            elif float(node.time_to) < -30.0 and time.monotonic() - no_progress_since > 20.0:
                self._record_live_sample(
                    vessel,
                    recorder,
                    start,
                    f"{phase}_stale_node_abort",
                    {
                        "remaining_delta_v_mps": remaining,
                        "node_time_to_s": float(node.time_to),
                    },
                )
                break
            if phase == "trans_mun_injection":
                try:
                    time_to_soi = float(vessel.orbit.time_to_soi_change)
                except Exception:
                    time_to_soi = float("inf")
                encounter_body = ""
                apoapsis = float(vessel.orbit.apoapsis_altitude)
                apoapsis_cap = self._tmi_apoapsis_cap_m(planned_safe_mun_transfer)
                if math.isfinite(time_to_soi) and time_to_soi > 0.0:
                    try:
                        next_orbit = vessel.orbit.next_orbit
                        encounter_periapsis = float(next_orbit.periapsis_altitude)
                        encounter_body = str(next_orbit.body.name)
                    except Exception:
                        encounter_periapsis = float("inf")
                        encounter_body = ""
                    if encounter_body == "Mun" and 35_000.0 <= encounter_periapsis <= 180_000.0:
                        break
                if apoapsis > apoapsis_cap and (not math.isfinite(time_to_soi) or encounter_body != "Mun"):
                    self._record_live_sample(
                        vessel,
                        recorder,
                        start,
                        "trans_mun_injection_apoapsis_cap",
                        {
                            "apoapsis_m": apoapsis,
                            "apoapsis_cap_m": apoapsis_cap,
                            "time_to_soi_change_s": time_to_soi,
                            "encounter_body": encounter_body,
                            "planned_safe_mun_transfer": planned_safe_mun_transfer,
                        },
                    )
                    break
            vessel.control.throttle = self._maneuver_node_throttle(
                phase,
                remaining,
                float(vessel.mass),
                float(vessel.available_thrust),
            )
            if self._can_stage_now(last_stage_at) and self._should_stage(vessel, stage_status):
                vessel.control.activate_next_stage()
                last_stage_at = time.monotonic()
                vessel = self._reacquire_vessel(conn, vessel, preferred_name)
            self._record_live_sample(
                vessel,
                recorder,
                start,
                f"{phase}_burn",
                {
                    "remaining_delta_v_mps": remaining,
                    "node_time_to_s": float(node.time_to),
                    "active_engines": stage_status["active_engines"],
                    "fueled_active_engines": stage_status["fueled_active_engines"],
                    "dead_active_engines": stage_status["dead_active_engines"],
                },
            )
            time.sleep(0.25)
        vessel.control.throttle = 0.0
        self._record_live_sample(vessel, recorder, start, f"{phase}_complete", {"remaining_delta_v_mps": float(node.remaining_delta_v)})
        try:
            node.remove()
        except Exception:
            pass
        return vessel

    def _coast_to_mun_soi(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
    ) -> None:
        correction_attempts = 0
        min_kerbin_periapsis = float(self.config.get("min_kerbin_transfer_periapsis_m", 70_000.0))
        while time.monotonic() - start < timeout_s:
            body_name = vessel.orbit.body.name
            if body_name == "Mun":
                self._record_live_sample(vessel, recorder, start, "mun_soi_entered")
                return
            if body_name == "Kerbin":
                try:
                    kerbin_periapsis = float(vessel.orbit.periapsis_altitude)
                except Exception:
                    kerbin_periapsis = float("nan")
                if math.isfinite(kerbin_periapsis) and kerbin_periapsis < min_kerbin_periapsis:
                    self._record_live_sample(
                        vessel,
                        recorder,
                        start,
                        "mun_transfer_reentry_periapsis",
                        {
                            "periapsis_m": kerbin_periapsis,
                            "min_kerbin_periapsis_m": min_kerbin_periapsis,
                        },
                    )
                    return
            try:
                time_to_soi = float(vessel.orbit.time_to_soi_change)
            except Exception:
                time_to_soi = float("inf")
            if not math.isfinite(time_to_soi) or time_to_soi <= 0:
                for _ in range(6):
                    time.sleep(0.5)
                    try:
                        time_to_soi = float(vessel.orbit.time_to_soi_change)
                    except Exception:
                        time_to_soi = float("inf")
                    if math.isfinite(time_to_soi) and time_to_soi > 0:
                        break
            if not math.isfinite(time_to_soi) or time_to_soi <= 0:
                if correction_attempts < 2 and self._correct_missed_mun_transfer(conn, vessel, recorder, start, timeout_s):
                    correction_attempts += 1
                    continue
                if self._top_off_mun_transfer(conn, vessel, recorder, start, timeout_s):
                    continue
                self._record_live_sample(vessel, recorder, start, "mun_encounter_missed", {"time_to_soi_change_s": time_to_soi})
                return
            self._record_live_sample(vessel, recorder, start, "coast_to_mun_soi", {"time_to_soi_change_s": time_to_soi})
            if time_to_soi > 90:
                conn.space_center.warp_to(conn.space_center.ut + time_to_soi - 60.0, max_rails_rate=100_000.0, max_physics_rate=4.0)
            else:
                time.sleep(1.0)
        self._record_live_sample(vessel, recorder, start, "mun_coast_timeout")

    def _correct_missed_mun_transfer(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
    ) -> bool:
        if vessel.orbit.body.name != "Kerbin":
            return False
        try:
            apoapsis = float(vessel.orbit.apoapsis_altitude)
        except Exception:
            return False
        if apoapsis < 10_000_000.0 or apoapsis > 20_000_000.0:
            return False

        mun = conn.space_center.bodies["Mun"]
        now = float(conn.space_center.ut)
        candidate_uts = [now + offset for offset in (60.0, 180.0, 360.0, 600.0, 900.0, 1_500.0)]
        prograde_values = (-45.0, -30.0, -20.0, -12.0, -6.0, 0.0, 6.0, 12.0, 20.0, 30.0, 45.0)
        radial_values = (-60.0, -40.0, -25.0, -12.0, 0.0, 12.0, 25.0, 40.0, 60.0)
        best = self._search_mun_correction_node(vessel, mun, candidate_uts, prograde_values, radial_values)
        if best is None:
            self._record_live_sample(vessel, recorder, start, "mun_transfer_correction_not_found")
            return False

        recorder.append(
            {
                "elapsed_s": time.monotonic() - start,
                "phase": "mun_transfer_correction_selected",
                "node_ut": best["ut"],
                "prograde_mps": best["prograde"],
                "radial_mps": best.get("radial", 0.0),
                "delta_v_mps": best.get("delta_v_mps", 0.0),
                "mun_periapsis_m": best.get("mun_periapsis_m", -1),
                "closest_approach_m": best.get("closest_approach_m", -1),
            }
        )
        node = vessel.control.add_node(
            float(best["ut"]),
            prograde=float(best["prograde"]),
            radial=float(best.get("radial", 0.0)),
        )
        self._execute_node(
            conn,
            vessel,
            node,
            recorder,
            start,
            timeout_s,
            "mun_transfer_correction",
            preferred_name=str(vessel.name),
        )
        return True

    def _top_off_mun_transfer(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
    ) -> bool:
        if vessel.orbit.body.name != "Kerbin":
            return False
        try:
            apoapsis = float(vessel.orbit.apoapsis_altitude)
        except Exception:
            apoapsis = 0.0
        if apoapsis < 8_000_000.0 or apoapsis > 14_500_000.0:
            return False

        vessel.auto_pilot.reference_frame = vessel.orbital_reference_frame
        vessel.auto_pilot.target_direction = (0, 1, 0)
        time.sleep(0.75)
        last_stage_at = time.monotonic()
        topoff_started = time.monotonic()
        while time.monotonic() - start < timeout_s and time.monotonic() - topoff_started < 60.0:
            try:
                time_to_soi = float(vessel.orbit.time_to_soi_change)
            except Exception:
                time_to_soi = float("inf")
            if math.isfinite(time_to_soi) and time_to_soi > 0:
                vessel.control.throttle = 0.0
                self._record_live_sample(
                    vessel,
                    recorder,
                    start,
                    "mun_transfer_topoff_complete",
                    {"time_to_soi_change_s": time_to_soi},
                )
                return True

            apoapsis = float(vessel.orbit.apoapsis_altitude)
            if apoapsis > 14_500_000.0:
                vessel.control.throttle = 0.0
                self._record_live_sample(vessel, recorder, start, "mun_transfer_topoff_apoapsis_cap")
                return False

            if apoapsis < 11_800_000.0:
                vessel.control.throttle = 0.35
            elif apoapsis < 12_800_000.0:
                vessel.control.throttle = 0.12
            else:
                vessel.control.throttle = 0.04

            stage_status = self._stage_status(vessel)
            if self._can_stage_now(last_stage_at) and self._should_stage(vessel, stage_status):
                vessel.control.activate_next_stage()
                last_stage_at = time.monotonic()
            self._record_live_sample(
                vessel,
                recorder,
                start,
                "mun_transfer_topoff_burn",
                {
                    "time_to_soi_change_s": time_to_soi,
                    "active_engines": stage_status["active_engines"],
                    "fueled_active_engines": stage_status["fueled_active_engines"],
                    "dead_active_engines": stage_status["dead_active_engines"],
                },
            )
            time.sleep(0.25)
        vessel.control.throttle = 0.0
        self._record_live_sample(vessel, recorder, start, "mun_transfer_topoff_timeout")
        return False

    def _raise_mun_periapsis(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
        target_periapsis_m: float = 35_000.0,
    ) -> bool:
        if vessel.orbit.body.name != "Mun":
            return False
        try:
            if float(vessel.orbit.periapsis_altitude) >= target_periapsis_m:
                return True
        except Exception:
            return False

        vessel.control.throttle = 0.0
        self._record_live_sample(
            vessel,
            recorder,
            start,
            "mun_periapsis_raise_start",
            {"target_periapsis_m": target_periapsis_m},
        )
        try:
            time_to_apoapsis = float(vessel.orbit.time_to_apoapsis)
            if time_to_apoapsis > 45.0:
                conn.space_center.warp_to(
                    conn.space_center.ut + time_to_apoapsis - 30.0,
                    max_rails_rate=100_000.0,
                    max_physics_rate=4.0,
                )
        except Exception:
            pass

        self._set_physics_warp(conn, 0)
        self._point_orbital_sas_prograde(vessel, align_timeout_s=4.0)
        last_stage_at = time.monotonic()
        raise_started = time.monotonic()
        while time.monotonic() - start < timeout_s and time.monotonic() - raise_started < 120.0:
            periapsis = float(vessel.orbit.periapsis_altitude)
            apoapsis = float(vessel.orbit.apoapsis_altitude)
            if periapsis >= target_periapsis_m:
                vessel.control.throttle = 0.0
                self._record_live_sample(
                    vessel,
                    recorder,
                    start,
                    "mun_periapsis_raised",
                    {"target_periapsis_m": target_periapsis_m},
                )
                return True
            if apoapsis > 1_500_000.0:
                vessel.control.throttle = 0.0
                self._record_live_sample(
                    vessel,
                    recorder,
                    start,
                    "mun_periapsis_raise_apoapsis_cap",
                    {"target_periapsis_m": target_periapsis_m},
                )
                return False

            try:
                available_accel = max(0.1, float(vessel.available_thrust) / max(1.0, float(vessel.mass)))
            except Exception:
                available_accel = 1.0
            throttle = min(0.08, max(0.02, 0.20 / available_accel))
            if periapsis > target_periapsis_m - 10_000.0:
                throttle = min(throttle, 0.03)
            vessel.control.throttle = throttle

            stage_status = self._stage_status(vessel)
            if self._can_stage_now(last_stage_at) and self._should_stage(vessel, stage_status):
                vessel.control.activate_next_stage()
                vessel.control.throttle = throttle
                last_stage_at = time.monotonic()
            try:
                actual_thrust = float(vessel.thrust)
            except Exception:
                actual_thrust = 0.0
            self._record_live_sample(
                vessel,
                recorder,
                start,
                "mun_periapsis_raise_burn",
                {
                    "target_periapsis_m": target_periapsis_m,
                    "raise_throttle": throttle,
                    "actual_thrust_n": actual_thrust,
                    "active_engines": stage_status["active_engines"],
                    "fueled_active_engines": stage_status["fueled_active_engines"],
                    "dead_active_engines": stage_status["dead_active_engines"],
                },
            )
            time.sleep(0.25)

        vessel.control.throttle = 0.0
        self._record_live_sample(
            vessel,
            recorder,
            start,
            "mun_periapsis_raise_timeout",
            {"target_periapsis_m": target_periapsis_m},
        )
        return False

    def _correct_mun_soi_periapsis(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
        relay_capture: bool = False,
    ) -> bool:
        """If the Mun-SOI entry orbit has a periapsis too high to capture (finite-burn drift
        leaves a grazing pass), search a maneuver node that produces a BOUND orbit (apoapsis
        inside the SOI) with a low periapsis, then execute it. Search-based so it is correct on a
        hyperbolic approach (no analytic apoapsis), and it spends the abundant remaining fuel to
        redirect a grazing pass into a capturable approach. For a relay (which wants a HIGH orbit)
        the gate is the relay's max apoapsis, so only genuinely-too-high encounters are corrected."""

        try:
            if vessel.orbit.body.name != "Mun":
                return False
            periapsis = float(vessel.orbit.periapsis_altitude)
        except Exception:
            return False
        if relay_capture:
            # A relay orbit may sit anywhere up to its max apoapsis, so a high periapsis is fine.
            gate = float(self.config.get("mun_relay_max_apoapsis_m", 2_150_000.0))
        else:
            gate = float(self.config.get("max_mun_capture_periapsis_m", 1_000_000.0))
        if periapsis <= gate:
            return True  # already capturable; nothing to do
        target = float(self.config.get("mun_soi_correction_target_periapsis_m", 100_000.0))
        try:
            body = vessel.orbit.body
            soi_altitude = float(body.sphere_of_influence) - float(body.equatorial_radius)
        except Exception:
            soi_altitude = 2_000_000.0
        self._record_live_sample(
            vessel,
            recorder,
            start,
            "mun_soi_periapsis_correction_needed",
            {"periapsis_m": periapsis, "target_periapsis_m": target, "soi_altitude_m": soi_altitude},
        )

        now = float(conn.space_center.ut)
        # Burn soon after entry (small lead) while still far from the close approach; Mun-relative
        # speed is low on a grazing pass, so a few hundred m/s of radial swings the periapsis far.
        candidate_uts = (now + 10.0, now + 60.0, now + 150.0)
        prograde_values = (0.0, -150.0, -300.0, -450.0, -600.0, -800.0)
        radial_values = (-800.0, -600.0, -400.0, -200.0, -100.0, 100.0, 200.0, 400.0, 600.0, 800.0)
        max_apoapsis = min(soi_altitude * 0.9, float(self.config.get("mun_relay_max_apoapsis_m", 2_150_000.0)))

        best = None
        best_score = float("inf")
        for ut in candidate_uts:
            for prograde in prograde_values:
                for radial in radial_values:
                    if abs(prograde) < 1.0 and abs(radial) < 1.0:
                        continue
                    node = vessel.control.add_node(float(ut), prograde=float(prograde), radial=float(radial))
                    try:
                        orbit = node.orbit
                        apoapsis = float(orbit.apoapsis_altitude)
                        pe = float(orbit.periapsis_altitude)
                        bound = 0.0 < apoapsis < max_apoapsis
                        if bound and 10_000.0 < pe < gate:
                            delta_v = math.hypot(prograde, radial)
                            score = abs(pe - target) + delta_v * 0.5
                            if score < best_score:
                                best_score = score
                                best = (float(ut), float(prograde), float(radial), pe, apoapsis)
                    except Exception:
                        pass
                    finally:
                        try:
                            node.remove()
                        except Exception:
                            pass

        if best is None:
            self._record_live_sample(vessel, recorder, start, "mun_soi_periapsis_correction_no_solution", {"periapsis_m": periapsis})
            return False

        node_ut, prograde, radial, pe_pred, ap_pred = best
        self._record_live_sample(
            vessel,
            recorder,
            start,
            "mun_soi_periapsis_correction_selected",
            {"prograde_mps": prograde, "radial_mps": radial, "predicted_periapsis_m": pe_pred, "predicted_apoapsis_m": ap_pred},
        )
        node = vessel.control.add_node(node_ut, prograde=prograde, radial=radial)
        vessel = self._execute_node(
            conn,
            vessel,
            node,
            recorder,
            start,
            timeout_s,
            "mun_soi_periapsis_correction",
            preferred_name=str(vessel.name),
        )
        try:
            new_periapsis = float(vessel.orbit.periapsis_altitude)
        except Exception:
            return False
        corrected = new_periapsis <= gate
        self._record_live_sample(
            vessel,
            recorder,
            start,
            "mun_soi_periapsis_corrected" if corrected else "mun_soi_periapsis_correction_incomplete",
            {"periapsis_m": new_periapsis},
        )
        return corrected

    def _capture_mun_orbit(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
        relay_capture: bool = False,
    ) -> bool:
        try:
            time_to_periapsis = float(vessel.orbit.time_to_periapsis)
        except Exception:
            time_to_periapsis = 0.0
        try:
            periapsis_altitude = float(vessel.orbit.periapsis_altitude)
        except Exception:
            periapsis_altitude = 0.0
        capture_lead_floor_s = 35.0
        if periapsis_altitude < 30_000.0:
            capture_lead_floor_s = 25.0
        if periapsis_altitude < 12_000.0:
            capture_lead_floor_s = 20.0
        capture_lead_s = capture_lead_floor_s
        capture_delta_v_mps = 0.0
        try:
            estimate = capture_burn_estimate(
                mu=float(vessel.orbit.body.gravitational_parameter),
                body_radius_m=float(vessel.orbit.body.equatorial_radius),
                periapsis_altitude_m=periapsis_altitude,
                semi_major_axis_m=float(vessel.orbit.semi_major_axis),
                mass_kg=float(vessel.mass),
                thrust_n=max(1.0, float(vessel.available_thrust)),
                target_capture_altitude_m=35_000.0,
            )
            capture_delta_v_mps = estimate.delta_v_mps
            if estimate.burn_time_s > 0.0:
                capture_lead_s = min(
                    float(self.config.get("max_mun_capture_lead_s", 180.0)),
                    max(capture_lead_floor_s, float(estimate.lead_time_s)),
                )
        except Exception:
            pass
        self._record_live_sample(
            vessel,
            recorder,
            start,
            "mun_capture_setup",
            {
                "time_to_periapsis_s": time_to_periapsis,
                "capture_lead_s": capture_lead_s,
                "capture_delta_v_estimate_mps": capture_delta_v_mps,
            },
        )
        # A relay wants a high orbit, so it may capture a high-periapsis encounter directly
        # (anywhere up to its max apoapsis); a lander must capture low.
        capture_periapsis_gate = (
            float(self.config.get("mun_relay_max_apoapsis_m", 2_150_000.0))
            if relay_capture
            else float(self.config.get("max_mun_capture_periapsis_m", 1_000_000.0))
        )
        if periapsis_altitude > capture_periapsis_gate:
            self._record_live_sample(
                vessel,
                recorder,
                start,
                "mun_capture_periapsis_too_high",
                {"periapsis_m": periapsis_altitude, "gate_m": capture_periapsis_gate},
            )
            return False
        if time_to_periapsis > capture_lead_s + 5.0:
            conn.space_center.warp_to(
                conn.space_center.ut + time_to_periapsis - capture_lead_s,
                max_rails_rate=100_000.0,
                max_physics_rate=4.0,
            )
        self._set_physics_warp(conn, 0)
        time.sleep(0.5)
        if bool(self.config.get("model_orbital_refueling", False)) and self._resource_fraction(vessel, "ElectricCharge") < 0.25:
            try:
                before_charge = self._resource_fraction(vessel, "ElectricCharge")
                result = self._bridge_refill_vessel(fraction=1.0, resources="ElectricCharge")
                time.sleep(0.5)
                self._record_live_sample(
                    vessel,
                    recorder,
                    start,
                    "modeled_capture_recharge",
                    {
                        "electric_charge_before": before_charge,
                        "electric_charge_after": self._resource_fraction(vessel, "ElectricCharge"),
                        "bridge_result": result,
                    },
                )
            except Exception as exc:
                self._record_live_sample(
                    vessel,
                    recorder,
                    start,
                    "modeled_capture_recharge_failed",
                    {
                        "electric_charge_before": self._resource_fraction(vessel, "ElectricCharge"),
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )

        # Hold retrograde with the AUTOPILOT (orbital frame) so the engine gimbal provides strong
        # attitude authority during the burn; SAS-only (reaction wheels) cannot hold this heavy,
        # probe-controlled stack on the fast-rotating retrograde marker, so the burn went far
        # off-axis and wasted >1 km/s without capturing. Align well before igniting.
        self._point_orbital_prograde(
            vessel, align_timeout_s=float(self.config.get("node_alignment_timeout_s", 20.0)), invert=True
        )
        self._set_physics_warp(conn, 0)
        vessel.control.throttle = 1.0
        last_stage_at = time.monotonic()
        burn_started = time.monotonic()
        last_retro_point_at = time.monotonic()
        first_actual_thrust_seen = False
        while time.monotonic() - start < timeout_s:
            self._set_physics_warp(conn, 0)
            if time.monotonic() - last_retro_point_at > 1.0:
                # Re-point retrograde continuously so the burn tracks the moving marker.
                self._point_orbital_prograde(vessel, invert=True)
                last_retro_point_at = time.monotonic()
            apoapsis = float(vessel.orbit.apoapsis_altitude)
            periapsis = float(vessel.orbit.periapsis_altitude)
            orbiting = str(vessel.situation).lower().endswith("orbiting")
            targeted_capture = orbiting and 20_000.0 <= apoapsis <= 360_000.0 and 12_000.0 <= periapsis <= 260_000.0
            emergency_capture = (
                orbiting
                and 0.0 < apoapsis < 2_000_000.0
                and 8_000.0 < periapsis < 500_000.0
                and self._fuel_fraction(vessel) < float(self.config.get("emergency_capture_fuel_fraction", 0.10))
            )
            loose_capture = orbiting and 0.0 < apoapsis < 650_000.0 and 6_000.0 < periapsis < 260_000.0
            # Relay accepts a HIGH bound orbit anywhere in its target band; stop the capture burn
            # as soon as the orbit is bound within [min_apoapsis-ish, max_apoapsis] so it does not
            # over-burn a valid high orbit down to a lander orbit.
            relay_band_capture = (
                relay_capture
                and orbiting
                and 0.0 < apoapsis <= float(self.config.get("mun_relay_max_apoapsis_m", 2_150_000.0))
                and periapsis >= float(self.config.get("mun_relay_min_periapsis_m", 50_000.0))
            )
            if targeted_capture or emergency_capture or loose_capture or relay_band_capture:
                vessel.control.throttle = 0.0
                phase_name = "mun_orbit_captured"
                if relay_band_capture and not (targeted_capture or loose_capture):
                    phase_name = "mun_orbit_captured_relay_band"
                elif emergency_capture:
                    phase_name = "mun_orbit_captured_emergency"
                elif loose_capture and not targeted_capture:
                    phase_name = "mun_orbit_captured_low_periapsis"
                self._record_live_sample(
                    vessel,
                    recorder,
                    start,
                    phase_name,
                )
                if periapsis < 30_000.0:
                    return self._raise_mun_periapsis(conn, vessel, recorder, start, timeout_s)
                return True
            if 10_000.0 <= periapsis <= 80_000.0 and 10_000.0 <= apoapsis <= 150_000.0:
                vessel.control.throttle = 0.0
                self._record_live_sample(vessel, recorder, start, "mun_orbit_captured")
                return True
            if orbiting and 0.0 < apoapsis < 500_000.0 and 4_000.0 < periapsis < 8_000.0:
                vessel.control.throttle = 0.0
                self._record_live_sample(vessel, recorder, start, "mun_orbit_captured_very_low_periapsis")
                return self._raise_mun_periapsis(conn, vessel, recorder, start, timeout_s)
            if orbiting and 0.0 < apoapsis < 500_000.0 and periapsis < 4_000.0:
                vessel.control.throttle = 0.0
                self._record_live_sample(vessel, recorder, start, "mun_capture_periapsis_too_low")
                return False
            if self._fuel_fraction(vessel) <= 0.001 and vessel.available_thrust < 1.0:
                vessel.control.throttle = 0.0
                self._record_live_sample(vessel, recorder, start, "mun_capture_out_of_fuel")
                return False
            if orbiting and 0.0 < apoapsis < 650_000.0 and periapsis > -10_000.0:
                desired_throttle = 0.04
            elif orbiting and 0.0 < apoapsis < 900_000.0 and periapsis > -10_000.0:
                desired_throttle = 0.14
            elif orbiting and 0.0 < apoapsis < 2_000_000.0 and periapsis > -10_000.0:
                desired_throttle = 0.22
            else:
                desired_throttle = 1.0
            vessel.control.throttle = desired_throttle
            try:
                actual_thrust = float(vessel.thrust)
            except Exception:
                actual_thrust = 0.0
            first_actual_thrust_seen = first_actual_thrust_seen or actual_thrust > 1.0
            if time.monotonic() - burn_started > 8.0 and not first_actual_thrust_seen:
                vessel.control.throttle = 0.0
                self._record_live_sample(
                    vessel,
                    recorder,
                    start,
                    "mun_capture_no_actual_thrust",
                    {"available_thrust_n": float(vessel.available_thrust), "actual_thrust_n": actual_thrust},
                )
                return False
            if time.monotonic() - burn_started > 240:
                vessel.control.throttle = 0.0
                self._record_live_sample(vessel, recorder, start, "mun_capture_timeout")
                return False
            stage_status = self._stage_status(vessel)
            if self._can_stage_now(last_stage_at) and self._should_stage(vessel, stage_status):
                vessel.control.activate_next_stage()
                vessel.control.throttle = desired_throttle
                last_stage_at = time.monotonic()
            self._record_live_sample(
                vessel,
                recorder,
                start,
                "mun_capture_burn",
                {
                    "active_engines": stage_status["active_engines"],
                    "fueled_active_engines": stage_status["fueled_active_engines"],
                    "dead_active_engines": stage_status["dead_active_engines"],
                    "actual_thrust_n": actual_thrust,
                    "throttle": float(vessel.control.throttle),
                    "electric_charge_fraction": self._resource_fraction(vessel, "ElectricCharge"),
                },
            )
            time.sleep(0.25)
        return False

    def _execute_mun_apsis_node(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
        *,
        phase: str,
        node_ut: float,
        prograde_delta_v_mps: float,
        target_attr: str,
        target_altitude_m: float,
        max_burn_s: float = 90.0,
        max_throttle: float = 0.18,
    ) -> bool:
        try:
            initial_altitude = float(getattr(vessel.orbit, target_attr))
        except Exception:
            return False
        decreasing = target_altitude_m <= initial_altitude
        if (decreasing and initial_altitude <= target_altitude_m) or (
            not decreasing and initial_altitude >= target_altitude_m
        ):
            self._record_live_sample(
                vessel,
                recorder,
                start,
                f"{phase}_already_met",
                {"target_altitude_m": target_altitude_m, target_attr: initial_altitude},
            )
            return True

        node = None
        try:
            node = vessel.control.add_node(float(node_ut), prograde=float(prograde_delta_v_mps))
            self._record_live_sample(
                vessel,
                recorder,
                start,
                f"{phase}_node",
                {
                    "prograde_delta_v_mps": prograde_delta_v_mps,
                    "target_altitude_m": target_altitude_m,
                    "initial_altitude_m": initial_altitude,
                    "node_time_to_s": float(node.time_to),
                },
            )

            start_ut = float(node.ut) - 8.0
            if start_ut > conn.space_center.ut:
                conn.space_center.warp_to(start_ut, max_rails_rate=100_000.0, max_physics_rate=4.0)
            self._set_physics_warp(conn, 0)
            time.sleep(0.5)
            self._point_at_node(vessel, node, align_timeout_s=float(self.config.get("node_alignment_timeout_s", 20.0)))
            while float(node.time_to) > 0.8 and time.monotonic() - start < timeout_s:
                self._record_live_sample(vessel, recorder, start, f"{phase}_waiting", {"node_time_to_s": float(node.time_to)})
                time.sleep(0.25)

            try:
                available_accel = max(0.1, float(vessel.available_thrust) / max(1.0, float(vessel.mass)))
            except Exception:
                available_accel = 1.0
            delta_v_abs = abs(float(prograde_delta_v_mps))
            base_throttle = min(max_throttle, max(0.015, delta_v_abs / (available_accel * 18.0)))
            if delta_v_abs < 30.0 and max_throttle <= 0.18:
                base_throttle = min(base_throttle, 0.04)

            last_stage_at = time.monotonic()
            burn_started = time.monotonic()
            last_vector_update_at = 0.0
            first_actual_thrust_seen = False
            correction_attempted = False
            reference_altitude = initial_altitude
            while time.monotonic() - start < timeout_s and time.monotonic() - burn_started < max_burn_s:
                try:
                    current_altitude = float(getattr(vessel.orbit, target_attr))
                except Exception:
                    current_altitude = float("inf")
                if (decreasing and current_altitude <= target_altitude_m) or (
                    not decreasing and current_altitude >= target_altitude_m
                ):
                    vessel.control.throttle = 0.0
                    self._record_live_sample(
                        vessel,
                        recorder,
                        start,
                        f"{phase}_complete",
                        {
                            "target_altitude_m": target_altitude_m,
                            target_attr: current_altitude,
                            "prograde_delta_v_mps": prograde_delta_v_mps,
                        },
                    )
                    return True

                if time.monotonic() - last_vector_update_at > 1.0:
                    self._point_at_node(vessel, node, invert=correction_attempted)
                    last_vector_update_at = time.monotonic()

                stage_status = self._stage_status(vessel)
                if self._can_stage_now(last_stage_at) and self._should_stage(vessel, stage_status):
                    vessel.control.activate_next_stage()
                    last_stage_at = time.monotonic()
                    vessel.control.throttle = base_throttle

                remaining_altitude = abs(current_altitude - target_altitude_m)
                if remaining_altitude < 15_000.0:
                    throttle = min(base_throttle, 0.02)
                elif remaining_altitude < 50_000.0:
                    throttle = min(base_throttle, 0.04)
                else:
                    throttle = base_throttle
                vessel.control.throttle = throttle

                try:
                    actual_thrust = float(vessel.thrust)
                except Exception:
                    actual_thrust = 0.0
                first_actual_thrust_seen = first_actual_thrust_seen or actual_thrust > 1.0

                if time.monotonic() - burn_started > 8.0 and not first_actual_thrust_seen:
                    vessel.control.throttle = 0.0
                    self._record_live_sample(
                        vessel,
                        recorder,
                        start,
                        f"{phase}_no_actual_thrust",
                        {
                            "target_altitude_m": target_altitude_m,
                            target_attr: current_altitude,
                            "actual_thrust_n": actual_thrust,
                            "throttle": throttle,
                        },
                    )
                    return False

                wrong_way_threshold = max(10_000.0, abs(reference_altitude - target_altitude_m) * 0.08)
                if (
                    time.monotonic() - burn_started > 5.0
                    and (
                        (decreasing and current_altitude > reference_altitude + wrong_way_threshold)
                        or (not decreasing and current_altitude < reference_altitude - wrong_way_threshold)
                    )
                ):
                    vessel.control.throttle = 0.0
                    self._record_live_sample(
                        vessel,
                        recorder,
                        start,
                        f"{phase}_wrong_direction",
                        {
                            "target_altitude_m": target_altitude_m,
                            target_attr: current_altitude,
                            "reference_altitude_m": reference_altitude,
                            "correction_attempted": correction_attempted,
                        },
                    )
                    if correction_attempted:
                        return False
                    correction_attempted = True
                    reference_altitude = current_altitude
                    first_actual_thrust_seen = False
                    self._point_at_node(
                        vessel,
                        node,
                        align_timeout_s=float(self.config.get("node_alignment_timeout_s", 20.0)),
                        invert=True,
                    )
                    vessel.control.throttle = max(0.012, base_throttle * 0.5)
                    time.sleep(1.0)
                    continue

                self._record_live_sample(
                    vessel,
                    recorder,
                    start,
                    f"{phase}_burn",
                    {
                        "target_altitude_m": target_altitude_m,
                        target_attr: current_altitude,
                        "prograde_delta_v_mps": prograde_delta_v_mps,
                        "throttle": throttle,
                        "actual_thrust_n": actual_thrust,
                        "active_engines": stage_status["active_engines"],
                        "fueled_active_engines": stage_status["fueled_active_engines"],
                        "dead_active_engines": stage_status["dead_active_engines"],
                    },
                )
                time.sleep(0.25)
        finally:
            vessel.control.throttle = 0.0
            if node is not None:
                try:
                    node.remove()
                except Exception:
                    pass

        self._record_live_sample(
            vessel,
            recorder,
            start,
            f"{phase}_timeout",
            {"target_altitude_m": target_altitude_m},
        )
        return False

    def _prepare_mun_landing_orbit(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
    ) -> bool:
        try:
            apoapsis = float(vessel.orbit.apoapsis_altitude)
            periapsis = float(vessel.orbit.periapsis_altitude)
        except Exception:
            return False
        target_apoapsis = float(self.config.get("hls_landing_target_apoapsis_m", 360_000.0))
        max_apoapsis = float(self.config.get("hls_landing_max_apoapsis_m", 450_000.0))
        if apoapsis <= max_apoapsis:
            self._record_live_sample(
                vessel,
                recorder,
                start,
                "mun_landing_orbit_ready",
                {"target_apoapsis_m": target_apoapsis, "max_apoapsis_m": max_apoapsis},
            )
            return True

        try:
            time_to_periapsis = float(vessel.orbit.time_to_periapsis)
            if time_to_periapsis > 45.0:
                conn.space_center.warp_to(
                    conn.space_center.ut + time_to_periapsis - 30.0,
                    max_rails_rate=100_000.0,
                    max_physics_rate=4.0,
                )
        except Exception:
            pass
        self._set_physics_warp(conn, 0)
        time.sleep(0.5)
        try:
            apoapsis = float(vessel.orbit.apoapsis_altitude)
            periapsis = float(vessel.orbit.periapsis_altitude)
            body_radius = float(vessel.orbit.body.equatorial_radius)
            mu = float(vessel.orbit.body.gravitational_parameter)
            node_ut = conn.space_center.ut + max(1.0, float(vessel.orbit.time_to_periapsis))
        except Exception:
            return False
        delta_v = self._opposite_apsis_delta_v_mps(
            mu=mu,
            body_radius_m=body_radius,
            burn_altitude_m=periapsis,
            current_opposite_altitude_m=apoapsis,
            target_opposite_altitude_m=target_apoapsis,
        )
        if delta_v >= -0.5:
            self._record_live_sample(
                vessel,
                recorder,
                start,
                "mun_landing_orbit_lower_not_possible",
                {"apoapsis_m": apoapsis, "periapsis_m": periapsis, "delta_v_mps": delta_v},
            )
            return False
        return self._execute_mun_apsis_node(
            conn,
            vessel,
            recorder,
            start,
            timeout_s,
            phase="mun_landing_orbit_lower",
            node_ut=node_ut,
            prograde_delta_v_mps=delta_v,
            target_attr="apoapsis_altitude",
            target_altitude_m=target_apoapsis,
            max_burn_s=120.0,
        )

    def _deorbit_mun_for_landing(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
    ) -> bool:
        target_periapsis = float(self.config.get("hls_landing_target_periapsis_m", -5_000.0))
        try:
            time_to_apoapsis = float(vessel.orbit.time_to_apoapsis)
            if time_to_apoapsis > 45.0:
                conn.space_center.warp_to(
                    conn.space_center.ut + time_to_apoapsis - 30.0,
                    max_rails_rate=100_000.0,
                    max_physics_rate=4.0,
                )
        except Exception:
            pass
        self._set_physics_warp(conn, 0)
        time.sleep(0.5)
        try:
            apoapsis = float(vessel.orbit.apoapsis_altitude)
            periapsis = float(vessel.orbit.periapsis_altitude)
            body_radius = float(vessel.orbit.body.equatorial_radius)
            mu = float(vessel.orbit.body.gravitational_parameter)
            node_ut = conn.space_center.ut + max(1.0, float(vessel.orbit.time_to_apoapsis))
        except Exception:
            return False
        delta_v = self._opposite_apsis_delta_v_mps(
            mu=mu,
            body_radius_m=body_radius,
            burn_altitude_m=apoapsis,
            current_opposite_altitude_m=periapsis,
            target_opposite_altitude_m=target_periapsis,
        )
        if delta_v >= -0.5:
            self._record_live_sample(
                vessel,
                recorder,
                start,
                "mun_landing_deorbit_not_possible",
                {"apoapsis_m": apoapsis, "periapsis_m": periapsis, "delta_v_mps": delta_v},
            )
            return False
        return self._execute_mun_apsis_node(
            conn,
            vessel,
            recorder,
            start,
            timeout_s,
            phase="mun_landing_deorbit",
            node_ut=node_ut,
            prograde_delta_v_mps=delta_v,
            target_attr="periapsis_altitude",
            target_altitude_m=target_periapsis,
            # Deorbit is a LARGE burn (not a precision apsis tweak); a low-thrust lander engine at
            # the default 0.18 cap times out. Allow full throttle and more time.
            max_burn_s=200.0,
            max_throttle=1.0,
        )

    def _shape_mun_relay_orbit(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
    ) -> bool:
        if vessel.orbit.body.name != "Mun":
            self._record_live_sample(vessel, recorder, start, "mun_relay_not_in_mun_orbit")
            return False

        target_apoapsis = float(self.config.get("mun_relay_target_apoapsis_m", 1_000_000.0))
        target_periapsis = float(self.config.get("mun_relay_target_periapsis_m", 200_000.0))
        min_apoapsis = float(self.config.get("mun_relay_min_apoapsis_m", 700_000.0))
        max_apoapsis = float(self.config.get("mun_relay_max_apoapsis_m", 2_150_000.0))
        min_periapsis = float(self.config.get("mun_relay_min_periapsis_m", 120_000.0))

        try:
            apoapsis = float(vessel.orbit.apoapsis_altitude)
            periapsis = float(vessel.orbit.periapsis_altitude)
        except Exception:
            return False

        if apoapsis < target_apoapsis:
            try:
                time_to_periapsis = float(vessel.orbit.time_to_periapsis)
                if time_to_periapsis > 45.0:
                    conn.space_center.warp_to(
                        conn.space_center.ut + time_to_periapsis - 30.0,
                        max_rails_rate=100_000.0,
                        max_physics_rate=4.0,
                    )
            except Exception:
                pass
            self._set_physics_warp(conn, 0)
            time.sleep(0.5)
            try:
                apoapsis = float(vessel.orbit.apoapsis_altitude)
                periapsis = float(vessel.orbit.periapsis_altitude)
                body_radius = float(vessel.orbit.body.equatorial_radius)
                mu = float(vessel.orbit.body.gravitational_parameter)
                node_ut = conn.space_center.ut + max(1.0, float(vessel.orbit.time_to_periapsis))
            except Exception:
                return False
            delta_v = self._opposite_apsis_delta_v_mps(
                mu=mu,
                body_radius_m=body_radius,
                burn_altitude_m=periapsis,
                current_opposite_altitude_m=apoapsis,
                target_opposite_altitude_m=target_apoapsis,
            )
            if delta_v <= 0.5:
                self._record_live_sample(
                    vessel,
                    recorder,
                    start,
                    "mun_relay_apoapsis_raise_not_possible",
                    {"apoapsis_m": apoapsis, "periapsis_m": periapsis, "delta_v_mps": delta_v},
                )
                return False
            if not self._execute_mun_apsis_node(
                conn,
                vessel,
                recorder,
                start,
                timeout_s,
                phase="mun_relay_apoapsis_raise",
                node_ut=node_ut,
                prograde_delta_v_mps=delta_v,
                target_attr="apoapsis_altitude",
                target_altitude_m=target_apoapsis,
                max_burn_s=150.0,
            ):
                return False

        try:
            periapsis = float(vessel.orbit.periapsis_altitude)
        except Exception:
            return False
        if periapsis < target_periapsis:
            try:
                time_to_apoapsis = float(vessel.orbit.time_to_apoapsis)
                if time_to_apoapsis > 45.0:
                    conn.space_center.warp_to(
                        conn.space_center.ut + time_to_apoapsis - 30.0,
                        max_rails_rate=100_000.0,
                        max_physics_rate=4.0,
                    )
            except Exception:
                pass
            self._set_physics_warp(conn, 0)
            time.sleep(0.5)
            try:
                apoapsis = float(vessel.orbit.apoapsis_altitude)
                periapsis = float(vessel.orbit.periapsis_altitude)
                body_radius = float(vessel.orbit.body.equatorial_radius)
                mu = float(vessel.orbit.body.gravitational_parameter)
                node_ut = conn.space_center.ut + max(1.0, float(vessel.orbit.time_to_apoapsis))
            except Exception:
                return False
            delta_v = self._opposite_apsis_delta_v_mps(
                mu=mu,
                body_radius_m=body_radius,
                burn_altitude_m=apoapsis,
                current_opposite_altitude_m=periapsis,
                target_opposite_altitude_m=target_periapsis,
            )
            if delta_v <= 0.5:
                self._record_live_sample(
                    vessel,
                    recorder,
                    start,
                    "mun_relay_periapsis_raise_not_possible",
                    {"apoapsis_m": apoapsis, "periapsis_m": periapsis, "delta_v_mps": delta_v},
                )
                return False
            if not self._execute_mun_apsis_node(
                conn,
                vessel,
                recorder,
                start,
                timeout_s,
                phase="mun_relay_periapsis_raise",
                node_ut=node_ut,
                prograde_delta_v_mps=delta_v,
                target_attr="periapsis_altitude",
                target_altitude_m=target_periapsis,
                max_burn_s=150.0,
            ):
                return False

        try:
            apoapsis = float(vessel.orbit.apoapsis_altitude)
            periapsis = float(vessel.orbit.periapsis_altitude)
            orbiting = str(vessel.situation).lower().endswith("orbiting")
        except Exception:
            return False
        relay_ok = orbiting and min_apoapsis <= apoapsis <= max_apoapsis and periapsis >= min_periapsis
        self._record_live_sample(
            vessel,
            recorder,
            start,
            "artemis_mun_relay_deployed" if relay_ok else "mun_relay_orbit_out_of_bounds",
            {
                "relay_deployed": relay_ok,
                "target_apoapsis_m": target_apoapsis,
                "target_periapsis_m": target_periapsis,
                "min_apoapsis_m": min_apoapsis,
                "max_apoapsis_m": max_apoapsis,
                "min_periapsis_m": min_periapsis,
                "mun_stationary_altitude_m": 2_970_563.4,
                "mun_sphere_of_influence_m": 2_429_559.1,
                "stock_constraint": "true Mun-stationary altitude is outside the Mun SOI; using high stable relay orbit",
            },
        )
        return relay_ok

    def _perform_mun_surface_science(self, vessel, recorder: TelemetryRecorder, start: float) -> bool:
        try:
            body_name = str(vessel.orbit.body.name)
            situation = str(vessel.situation).lower()
        except Exception:
            body_name = ""
            situation = ""
        if body_name != "Mun" or not situation.endswith("landed"):
            self._record_live_sample(vessel, recorder, start, "mun_surface_science_not_landed")
            return False

        triggered: list[str] = []
        science_keywords = (
            "experiment",
            "science",
            "observe",
            "log",
            "crew report",
            "surface sample",
            "materials",
            "temperature",
            "pressure",
            "seismic",
            "gravity",
        )
        try:
            for part_obj in vessel.parts.all:
                try:
                    modules = list(part_obj.modules)
                except Exception:
                    modules = []
                for module in modules:
                    events = []
                    try:
                        events = list(module.events)
                    except Exception:
                        pass
                    for event in events:
                        event_name = str(event)
                        if any(keyword in event_name.lower() for keyword in science_keywords):
                            try:
                                module.trigger_event(event_name)
                                triggered.append(event_name)
                            except Exception:
                                pass
        except Exception:
            pass

        self._record_live_sample(
            vessel,
            recorder,
            start,
            "mun_surface_science_completed",
            {
                "science_completed": True,
                "science_events_triggered": len(triggered),
                "modeled_surface_sample": len(triggered) == 0,
                "science_notes": "automated part experiments when available; otherwise modeled crew surface sample and observations",
            },
        )
        return True

    def _land_on_mun(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
    ) -> bool:
        self._record_live_sample(vessel, recorder, start, "mun_landing_deorbit_start")
        if not self._prepare_mun_landing_orbit(conn, vessel, recorder, start, timeout_s):
            return False
        if not self._deorbit_mun_for_landing(conn, vessel, recorder, start, timeout_s):
            return False

        try:
            if vessel.orbit.time_to_periapsis > 660:
                conn.space_center.warp_to(
                    conn.space_center.ut + float(vessel.orbit.time_to_periapsis) - 600.0,
                    max_rails_rate=100_000.0,
                    max_physics_rate=4.0,
                )
        except Exception:
            pass

        try:
            from krpc.services import spacecenter

            vessel.control.speed_mode = spacecenter.SpeedMode.surface
            vessel.control.sas = False
        except Exception:
            vessel.auto_pilot.reference_frame = vessel.surface_velocity_reference_frame
            vessel.auto_pilot.target_direction = (0, -1, 0)
        vessel.control.legs = True
        try:
            vessel.auto_pilot.engage()
        except Exception:
            pass
        last_stage_at = time.monotonic()
        while time.monotonic() - start < timeout_s:
            situation = str(vessel.situation).lower()
            if situation.endswith("landed"):
                vessel.control.throttle = 0.0
                flight = vessel.flight(vessel.orbit.body.reference_frame)
                touchdown_speed = float(flight.speed)
                touchdown_vertical = float(flight.vertical_speed)
                touchdown_horizontal = max(0.0, (touchdown_speed * touchdown_speed - touchdown_vertical * touchdown_vertical) ** 0.5)
                if touchdown_speed <= 3.0 and touchdown_horizontal <= 1.5:
                    self._record_live_sample(
                        vessel,
                        recorder,
                        start,
                        "mun_landed",
                        {
                            "landed": True,
                            "touchdown_speed_mps": touchdown_speed,
                            "horizontal_speed_mps": touchdown_horizontal,
                        },
                    )
                    return True
                self._record_live_sample(
                    vessel,
                    recorder,
                    start,
                    "mun_landed_unstable",
                    {
                        "landed": True,
                        "touchdown_speed_mps": touchdown_speed,
                        "horizontal_speed_mps": touchdown_horizontal,
                    },
                )
                return False

            flight = vessel.flight(vessel.orbit.body.reference_frame)
            surface_altitude = max(0.0, float(flight.surface_altitude))
            vertical_speed = float(flight.vertical_speed)
            surface_speed = float(flight.speed)
            horizontal_speed = float(flight.horizontal_speed)
            available_thrust = max(1.0, float(vessel.available_thrust))
            mass = max(0.1, float(vessel.mass))
            max_accel = max(0.1, available_thrust / mass)
            gravity = float(vessel.orbit.body.surface_gravity)
            descent_speed = max(0.0, -vertical_speed)

            # === Falcon-9 hoverslam ===
            # Freefall (engine off) while the total speed is below the reference curve, then ignite a
            # single decisive burn that HOLDS the speed on the curve so all velocity nulls right at
            # the ground. v_ref(h) = sqrt(2*(0.92*a_max - g)*h); reserving 8% thrust gives catch-up
            # headroom. Maximizes freefall (least time, least fuel) per the owner's Falcon-9 spec.
            reference_speed = hoverslam_reference_speed_mps(
                altitude_m=surface_altitude,
                mass_kg=mass,
                thrust_n=available_thrust,
                gravity_mps2=gravity,
                throttle_fraction=0.92,
            )
            # Total-speed braking distance, used ONLY for the high-altitude rails/warp gate.
            surface_stopping_distance = suicide_burn_distance_m(
                speed_mps=surface_speed,
                mass_kg=mass,
                thrust_n=available_thrust,
                gravity_mps2=gravity,
                command_delay_s=0.75,
                settle_s=0.75,
                safety_margin_m=80.0,
            )
            burn_gate = surface_stopping_distance * 1.35 + 3_000.0
            speed_margin = reference_speed - surface_speed  # >0 => still coasting toward ignition
            freefalling = speed_margin > 1.5

            # Below ~70 m the hoverslam reference curve drops too steeply for a 0.92-throttle burn to
            # track (it lags and touches down a few m/s fast), so hand off to a RELIABLE terminal
            # flare that brakes to a gentle, leg-saving touchdown. The hoverslam handles the bulk
            # high-speed brake above this; the flare guarantees the last 70 m.
            terminal_flare = surface_altitude < 70.0

            # Pointing: null the FULL velocity vector (surface-retrograde) while there is real speed
            # to kill (Falcon-9 style, kills horizontal too); flip to local-up for the slow flare so
            # the legs touch down level.
            try:
                if surface_speed > 4.0 and horizontal_speed > 2.5:
                    vessel.auto_pilot.reference_frame = vessel.surface_velocity_reference_frame
                    vessel.auto_pilot.target_direction = (0, -1, 0)
                    vessel.auto_pilot.engage()
                else:
                    self._target_local_up(vessel)
            except Exception:
                pass

            if surface_altitude > burn_gate and surface_altitude > 35_000.0:
                # Far above the burn: hold attitude and warp down toward ignition.
                throttle = 0.0
                self._set_physics_warp(conn, int(self.config.get("physics_warp_factor", 0)))
            elif freefalling and not terminal_flare:
                # FREEFALL band: engine off. Physics-warp only while comfortably above ignition so a
                # coarse warp step can't overshoot the burn start.
                throttle = 0.0
                warp = (
                    int(self.config.get("physics_warp_factor", 0))
                    if (speed_margin > 90.0 and surface_altitude > 4_000.0)
                    else 0
                )
                self._set_physics_warp(conn, warp)
            elif terminal_flare:
                # Reliable terminal flare: ramp the target descent rate down to a soft touchdown and
                # brake at full throttle if falling faster than target. Kills the last lateral drift.
                self._set_physics_warp(conn, 0)
                if surface_altitude > 45.0:
                    target_v = -9.0
                elif surface_altitude > 25.0:
                    target_v = -5.5
                elif surface_altitude > 12.0:
                    target_v = -3.0
                elif surface_altitude > 5.0:
                    target_v = -1.8
                else:
                    target_v = -1.0
                throttle = vertical_landing_throttle(
                    vertical_speed_mps=vertical_speed,
                    target_vertical_mps=target_v,
                    mass_kg=mass,
                    thrust_n=available_thrust,
                    gravity_mps2=gravity,
                    response_time_s=1.0,
                )
                if vertical_speed < target_v - 1.5:
                    throttle = max(throttle, 0.95)  # falling too fast for a soft touchdown -> brake hard
                if horizontal_speed > 3.0:
                    throttle = max(throttle, 0.35)  # help null residual lateral drift
                if vertical_speed > -0.5 and surface_altitude < 3.0:
                    throttle = 0.0
            else:
                # THE HOVERSLAM BURN: track the reference curve at near-full throttle.
                self._set_physics_warp(conn, 0)
                throttle = hoverslam_throttle(
                    speed_mps=surface_speed,
                    reference_speed_mps=reference_speed,
                    mass_kg=mass,
                    thrust_n=available_thrust,
                    gravity_mps2=gravity,
                    deadband_mps=1.5,
                )
            stopping_distance = surface_stopping_distance
            target_vertical = -reference_speed
            throttle = min(1.0, max(0.0, throttle))
            vessel.control.throttle = throttle

            stage_status = self._stage_status(vessel)
            if self._can_stage_now(last_stage_at) and self._should_stage(vessel, stage_status):
                vessel.control.activate_next_stage()
                last_stage_at = time.monotonic()
            self._record_live_sample(
                vessel,
                recorder,
                start,
                "mun_landing_descent",
                {
                    "landing_throttle": throttle,
                    "hoverslam_reference_speed_mps": reference_speed,
                    "hoverslam_speed_margin_mps": speed_margin,
                    "freefalling": freefalling,
                    "stopping_distance_m": stopping_distance,
                    "surface_stopping_distance_m": surface_stopping_distance,
                    "burn_gate_m": burn_gate,
                    "target_vertical_speed_mps": target_vertical,
                    "horizontal_speed_mps": horizontal_speed,
                    "active_engines": stage_status["active_engines"],
                    "fueled_active_engines": stage_status["fueled_active_engines"],
                    "dead_active_engines": stage_status["dead_active_engines"],
                },
            )
            if surface_altitude < 5.0 and abs(vertical_speed) > 12.0:
                self._record_live_sample(vessel, recorder, start, "mun_landing_hard_contact_risk")
            if self._fuel_fraction(vessel) <= 0.001 and vessel.available_thrust < 1.0:
                vessel.control.throttle = 0.0
                self._record_live_sample(vessel, recorder, start, "mun_landing_out_of_fuel")
                return False
            time.sleep(0.25)
        vessel.control.throttle = 0.0
        self._record_live_sample(vessel, recorder, start, "mun_landing_timeout")
        return False

    def _return_from_mun(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
    ) -> bool:
        if not self._launch_from_mun(conn, vessel, recorder, start, timeout_s):
            return False
        return self._return_to_kerbin_from_mun_orbit(conn, vessel, recorder, start, timeout_s)

    def _return_to_kerbin_from_mun_orbit(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
    ) -> bool:
        node = self._find_kerbin_return_node(conn, vessel, recorder, start)
        if node is None:
            self._record_live_sample(vessel, recorder, start, "kerbin_return_node_not_found")
            return False
        vessel = self._execute_node(
            conn,
            vessel,
            node,
            recorder,
            start,
            timeout_s,
            "trans_kerbin_injection",
            preferred_name=str(vessel.name),
        )
        if not self._coast_to_kerbin_soi(conn, vessel, recorder, start, timeout_s):
            return False
        return self._recover_on_kerbin(conn, vessel, recorder, start, timeout_s)

    def _launch_from_mun(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
    ) -> bool:
        self._set_physics_warp(conn, 0)
        vessel.control.throttle = 0.0
        vessel.control.legs = True
        vessel.control.sas = False
        try:
            self._target_local_up(vessel)
        except Exception:
            pass
        settle_started = time.monotonic()
        calm_since = None
        settled = False
        while time.monotonic() - start < timeout_s and time.monotonic() - settle_started < 18.0:
            now = time.monotonic()
            flight = vessel.flight(vessel.orbit.body.reference_frame)
            calm = abs(float(flight.vertical_speed)) < 0.25 and float(flight.speed) < 0.55
            if calm:
                calm_since = calm_since or now
            else:
                calm_since = None
            self._record_live_sample(vessel, recorder, start, "mun_return_landed_settle", {"settled": calm})
            if calm_since is not None and now - calm_since > 1.5 and now - settle_started > 3.0:
                settled = True
                break
            time.sleep(0.25)
        if not settled:
            vessel.control.throttle = 0.0
            self._record_live_sample(vessel, recorder, start, "mun_return_unstable_landing")
            return False
        vessel.control.throttle = 1.0
        last_stage_at = time.monotonic()
        target_apoapsis = 20_000.0
        insertion_periapsis = 10_000.0
        legs_retracted = False

        while time.monotonic() - start < timeout_s:
            flight = vessel.flight(vessel.orbit.body.reference_frame)
            surface_altitude = max(0.0, float(flight.surface_altitude))
            vertical_speed = float(flight.vertical_speed)
            apoapsis = float(vessel.orbit.apoapsis_altitude)
            if apoapsis >= target_apoapsis:
                break
            vessel.control.throttle = 1.0
            pitch = self._mun_ascent_pitch(surface_altitude, apoapsis, target_apoapsis, vertical_speed)
            if pitch >= 89.0:
                try:
                    self._target_local_up(vessel)
                except Exception:
                    pass
            else:
                try:
                    self._target_surface_pitch_heading(vessel, pitch, 90.0)
                except Exception:
                    pass
            if surface_altitude > 80.0 and not legs_retracted:
                vessel.control.legs = False
                legs_retracted = True
            stage_status = self._stage_status(vessel)
            if surface_altitude > 140.0 and self._can_stage_now(last_stage_at) and self._should_stage(vessel, stage_status):
                vessel.control.activate_next_stage()
                last_stage_at = time.monotonic()
            self._record_live_sample(
                vessel,
                recorder,
                start,
                "mun_return_ascent",
                {
                    "target_apoapsis_m": target_apoapsis,
                    "active_engines": stage_status["active_engines"],
                    "fueled_active_engines": stage_status["fueled_active_engines"],
                    "dead_active_engines": stage_status["dead_active_engines"],
                },
            )
            if self._fuel_fraction(vessel) <= 0.001 and vessel.available_thrust < 1.0:
                vessel.control.throttle = 0.0
                self._record_live_sample(vessel, recorder, start, "mun_return_ascent_out_of_fuel")
                return False
            time.sleep(0.25)
        vessel.control.throttle = 0.0

        while time.monotonic() - start < timeout_s and vessel.orbit.time_to_apoapsis > 18.0:
            self._set_physics_warp(conn, int(self.config.get("physics_warp_factor", 0)))
            self._record_live_sample(vessel, recorder, start, "mun_return_coast_to_apoapsis")
            time.sleep(0.25)
        self._set_physics_warp(conn, 0)

        try:
            self._target_surface_pitch_heading(vessel, 0.0, 90.0)
        except Exception:
            pass
        vessel.control.throttle = 1.0
        burn_started = time.monotonic()
        while time.monotonic() - start < timeout_s:
            periapsis = float(vessel.orbit.periapsis_altitude)
            apoapsis = float(vessel.orbit.apoapsis_altitude)
            if periapsis >= insertion_periapsis and apoapsis < 90_000.0:
                vessel.control.throttle = 0.0
                self._record_live_sample(vessel, recorder, start, "mun_return_orbit_established")
                return True
            try:
                self._target_surface_pitch_heading(vessel, 0.0, 90.0)
            except Exception:
                pass
            if time.monotonic() - burn_started > 180.0:
                vessel.control.throttle = 0.0
                self._record_live_sample(vessel, recorder, start, "mun_return_orbit_timeout")
                return False
            stage_status = self._stage_status(vessel)
            if self._can_stage_now(last_stage_at) and self._should_stage(vessel, stage_status):
                vessel.control.activate_next_stage()
                last_stage_at = time.monotonic()
            self._record_live_sample(
                vessel,
                recorder,
                start,
                "mun_return_circularization_burn",
                {
                    "active_engines": stage_status["active_engines"],
                    "fueled_active_engines": stage_status["fueled_active_engines"],
                    "dead_active_engines": stage_status["dead_active_engines"],
                },
            )
            if self._fuel_fraction(vessel) <= 0.001 and vessel.available_thrust < 1.0:
                vessel.control.throttle = 0.0
                self._record_live_sample(vessel, recorder, start, "mun_return_orbit_out_of_fuel")
                return False
            time.sleep(0.25)
        vessel.control.throttle = 0.0
        return False

    def _find_kerbin_return_node(self, conn, vessel, recorder: TelemetryRecorder, start: float):
        now = float(conn.space_center.ut)
        period = max(1_000.0, min(9_000.0, float(vessel.orbit.period)))
        best: dict[str, float | bool | str] | None = None
        best_score = float("inf")
        candidate_dvs = list(range(-560, -80, 10)) + list(range(80, 561, 10))
        for dt_index in range(0, 49):
            dt = 60.0 + period * dt_index / 48.0
            for dv in candidate_dvs:
                node = vessel.control.add_node(now + dt, prograde=float(dv))
                try:
                    candidate = self._score_kerbin_return_node(node)
                    score = float(candidate["score"])
                    if score < best_score:
                        best_score = score
                        best = {"ut": now + dt, "prograde": float(dv), **candidate}
                finally:
                    try:
                        node.remove()
                    except Exception:
                        pass

        if best is None or not best.get("encounter", False):
            return None
        recorder.append(
            {
                "elapsed_s": time.monotonic() - start,
                "phase": "kerbin_return_node_selected",
                "node_ut": best["ut"],
                "prograde_mps": best["prograde"],
                "kerbin_periapsis_m": best.get("kerbin_periapsis_m", -1),
                "encounter": best.get("encounter", False),
            }
        )
        return vessel.control.add_node(float(best["ut"]), prograde=float(best["prograde"]))

    @staticmethod
    def _score_kerbin_return_node(node) -> dict[str, float | bool | str]:
        encounter = False
        kerbin_periapsis = float("inf")
        next_body = ""
        try:
            next_orbit = node.orbit.next_orbit
            next_body = str(next_orbit.body.name)
            encounter = next_body == "Kerbin"
            if encounter:
                kerbin_periapsis = float(next_orbit.periapsis_altitude)
        except Exception:
            pass
        if encounter:
            target = 30_000.0
            if 24_000.0 <= kerbin_periapsis <= 38_000.0:
                score = abs(kerbin_periapsis - target)
            elif 18_000.0 <= kerbin_periapsis <= 45_000.0:
                score = abs(kerbin_periapsis - target) + 50_000.0
            elif -80_000.0 <= kerbin_periapsis <= 60_000.0:
                score = abs(kerbin_periapsis - target) + 350_000.0
            else:
                score = abs(kerbin_periapsis - target) + 1_500_000.0
        else:
            score = float("inf")
        return {
            "score": score,
            "encounter": encounter,
            "kerbin_periapsis_m": kerbin_periapsis,
            "next_body": next_body,
        }

    def _coast_to_kerbin_soi(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
    ) -> bool:
        while time.monotonic() - start < timeout_s:
            if vessel.orbit.body.name == "Kerbin":
                self._record_live_sample(vessel, recorder, start, "kerbin_soi_entered")
                return True
            try:
                time_to_soi = float(vessel.orbit.time_to_soi_change)
            except Exception:
                time_to_soi = float("inf")
            if not math.isfinite(time_to_soi) or time_to_soi <= 0:
                self._record_live_sample(vessel, recorder, start, "kerbin_return_encounter_missed")
                return False
            self._record_live_sample(vessel, recorder, start, "coast_to_kerbin_soi", {"time_to_soi_change_s": time_to_soi})
            if time_to_soi > 90.0:
                conn.space_center.warp_to(
                    conn.space_center.ut + time_to_soi - 60.0,
                    max_rails_rate=100_000.0,
                    max_physics_rate=4.0,
                )
            else:
                time.sleep(1.0)
        self._record_live_sample(vessel, recorder, start, "kerbin_return_coast_timeout")
        return False

    def _recover_on_kerbin(
        self,
        conn,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        timeout_s: int,
    ) -> bool:
        vessel.control.throttle = 0.0
        try:
            from krpc.services import spacecenter

            vessel.control.sas = True
            vessel.control.speed_mode = spacecenter.SpeedMode.surface
            vessel.control.sas_mode = spacecenter.SASMode.retrograde
        except Exception:
            pass

        while time.monotonic() - start < timeout_s:
            situation = str(vessel.situation).lower()
            if situation.endswith("landed") or situation.endswith("splashed"):
                self._set_physics_warp(conn, 0)
                vessel.control.throttle = 0.0
                self._record_live_sample(vessel, recorder, start, "recovered", {"landed": True, "recovered": True})
                return True
            flight = vessel.flight(vessel.orbit.body.reference_frame)
            altitude = float(flight.mean_altitude)
            surface_altitude = float(flight.surface_altitude)
            if altitude > 80_000.0:
                try:
                    time_to_periapsis = float(vessel.orbit.time_to_periapsis)
                except Exception:
                    time_to_periapsis = 0.0
                if time_to_periapsis > 120.0:
                    conn.space_center.warp_to(
                        conn.space_center.ut + time_to_periapsis - 90.0,
                        max_rails_rate=100_000.0,
                        max_physics_rate=4.0,
                    )
            elif altitude > 30_000.0:
                self._set_physics_warp(conn, int(self.config.get("physics_warp_factor", 0)))
            else:
                self._set_physics_warp(conn, 0)

            if surface_altitude < 8_000.0:
                try:
                    vessel.control.parachutes = True
                except Exception:
                    if int(vessel.control.current_stage) > 0:
                        vessel.control.activate_next_stage()

            self._record_live_sample(vessel, recorder, start, "kerbin_reentry_recovery")
            time.sleep(0.5)
        self._set_physics_warp(conn, 0)
        self._record_live_sample(vessel, recorder, start, "kerbin_recovery_timeout")
        return False

    def _record_live_sample(
        self,
        vessel,
        recorder: TelemetryRecorder,
        start: float,
        phase: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        flight = vessel.flight(vessel.orbit.body.reference_frame)
        sample = {
            "elapsed_s": time.monotonic() - start,
            "phase": phase,
            "altitude_m": float(flight.mean_altitude),
            "apoapsis_m": float(vessel.orbit.apoapsis_altitude),
            "periapsis_m": float(vessel.orbit.periapsis_altitude),
            "fuel_fraction_left": self._fuel_fraction(vessel),
            "current_stage": int(vessel.control.current_stage),
            "available_thrust_n": float(vessel.available_thrust),
            "orbit_body": str(vessel.orbit.body.name),
            "surface_altitude_m": float(flight.surface_altitude),
            "vertical_speed_mps": float(flight.vertical_speed),
            "surface_speed_mps": float(flight.speed),
        }
        if extra:
            sample.update(extra)
        recorder.append(sample)
