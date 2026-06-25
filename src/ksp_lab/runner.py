from __future__ import annotations

import json
import time
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from .artemis import artemis_phase_mission, build_artemis_architecture
from .bridge_client import BridgeClient, BridgeError
from .ai_provider import ExternalDesignProvider
from .config import load_config
from .craft_writer import CraftWriter
from .flight_controller import KrpcFlightController, OfflineSurrogateController
from .mission import MissionPlanner
from .models import ScoreResult, TelemetrySummary, TrialRecord
from .mod_craft_assets import (
    ksp_root_from_save_vab,
    write_artemis_hls_craft,
    write_renamed_craft,
)
from .optimizer import HistoryOptimizer
from .parts import estimate_design
from .research import KnowledgeBase
from .scoring import MissionScorer
from .storage import TrialDatabase
from .telemetry import TelemetryRecorder


class AutomationRunner:
    def __init__(self, config_path: str | Path | None = None, offline: bool = False):
        self.config_path = Path(config_path).resolve() if config_path else None
        self.config = load_config(self.config_path)
        self.project_root = self.config_path.parent.parent if self.config_path else Path.cwd()
        self.offline = offline
        self.run_dir = self._resolve(self.config["paths"]["run_dir"])
        self.db = TrialDatabase(self._resolve(self.config["paths"]["database"]))
        self.writer = CraftWriter()
        self.scorer = MissionScorer()

    def run(self, goal: str, max_trials: int | None = None) -> dict:
        mission = MissionPlanner().interpret(goal)
        if mission.mission_type == "artemis_hls_orion_return":
            return self._run_artemis(mission, max_trials)

        optimizer = HistoryOptimizer(mission)
        last_score: ScoreResult | None = None
        success_streak = 0
        max_trials = max_trials or int(self.config["runner"]["max_trials"])
        success_required = int(self.config["runner"].get("success_streak_required", mission.reliability_trials))
        controller = (
            OfflineSurrogateController()
            if self.offline
            else KrpcFlightController(self.config["krpc"])
        )
        bridge = None if self.offline else BridgeClient(**self.config["bridge"])
        provider = self._external_provider()
        results: list[dict] = []

        for trial_index in range(1, max_trials + 1):
            fallback = optimizer.first_design() if trial_index == 1 else optimizer.next_design(last_score)
            if provider is not None:
                design = provider.propose(mission, fallback, last_score, self.db.list_trials(50))
            else:
                design = fallback
            design.estimates = estimate_design(design)
            trial_id = f"trial-{trial_index:04d}-{uuid4().hex[:8]}"
            trial_dir = self.run_dir / trial_id
            craft_dir = self._craft_dir()
            craft_path = self.writer.write(design, craft_dir, template_path=self._craft_template_path())
            telemetry_path = trial_dir / "telemetry.jsonl"
            record = TrialRecord(
                trial_id=trial_id,
                mission=mission,
                design=design,
                craft_path=str(craft_path),
                telemetry_path=str(telemetry_path),
                mode="offline" if self.offline else "ksp",
                status="running",
            )
            trial_dir.mkdir(parents=True, exist_ok=True)
            (trial_dir / "design.json").write_text(json.dumps(design.to_dict(), indent=2), encoding="utf-8")
            (trial_dir / "mission.json").write_text(json.dumps(mission.to_dict(), indent=2), encoding="utf-8")
            self.db.start_trial(record)

            try:
                if bridge is not None:
                    bridge.load_craft(design.name)
                    time.sleep(float(self.config["runner"].get("post_load_settle_s", 6)))
                    self._wait_for_bridge_state(bridge, "loadedSceneIsEditor", True)
                    bridge.launch()
                    self._wait_for_bridge_state(bridge, "loadedSceneIsFlight", True)
                telemetry = controller.fly(
                    mission,
                    design,
                    telemetry_path,
                    timeout_s=int(self.config["runner"]["flight_timeout_s"]),
                )
                score = self.scorer.score(mission, design, telemetry)
                status = "succeeded" if score.success else "failed"
                self.db.finish_trial(trial_id, status, score, telemetry)
                if bridge is not None and self.config["runner"].get("revert_after_trial", True):
                    bridge.revert()
            except Exception as exc:
                self.db.mark_failed(trial_id, f"{type(exc).__name__}: {exc}")
                score = ScoreResult(0.0, False, str(exc), {"mission": 0.0})
                status = "failed"

            last_score = score
            success_streak = success_streak + 1 if score.success else 0
            result = {
                "trial_id": trial_id,
                "status": status,
                "success": score.success,
                "score": score.score,
                "failure_reason": score.failure_reason,
                "design": design.name,
                "estimates": design.estimates,
                "craft_path": str(craft_path),
                "telemetry_path": str(telemetry_path),
            }
            results.append(result)
            if success_streak >= success_required:
                break

        return {
            "mission": mission.to_dict(),
            "success": success_streak >= success_required,
            "trials_run": len(results),
            "success_streak": success_streak,
            "results": results,
            "database": str(self.db.path),
        }

    def _run_artemis(self, mission, max_trials: int | None = None) -> dict:
        max_trials = max_trials or int(self.config["runner"]["max_trials"])
        controller = (
            OfflineSurrogateController()
            if self.offline
            else KrpcFlightController(self.config["krpc"])
        )
        bridge = None if self.offline else BridgeClient(**self.config["bridge"])
        results: list[dict] = []
        success_streak = 0

        for trial_index in range(1, max_trials + 1):
            plan = build_artemis_architecture(mission)
            relay_design = deepcopy(plan.vehicle("relay").design)
            hls_design = deepcopy(plan.vehicle("hls").design)
            orion_design = deepcopy(plan.vehicle("orion").design)
            trial_suffix = uuid4().hex[:8]
            trial_id = f"artemis-{trial_index:04d}-{trial_suffix}"
            relay_design.name = f"AI-Mun-Relay-{trial_index:02d}-{trial_suffix}"
            hls_design.name = f"AI-HLS-Starship-{trial_index:02d}-{trial_suffix}"
            orion_design.name = f"AI-Orion-SLS-{trial_index:02d}-{trial_suffix}"
            relay_design.estimates = estimate_design(relay_design)
            hls_design.estimates = estimate_design(hls_design)
            orion_design.estimates = estimate_design(orion_design)

            trial_dir = self.run_dir / trial_id
            craft_dir = self._craft_dir()
            relay_path = self._write_artemis_relay_craft(relay_design, craft_dir)
            hls_path = self._write_artemis_hls_craft(hls_design.name, craft_dir)
            orion_path = self._write_artemis_orion_craft(orion_design.name, craft_dir)
            phase_paths = {
                "relay_predeploy": trial_dir / "relay_predeploy.telemetry.jsonl",
                "hls_predeploy": trial_dir / "hls_predeploy.telemetry.jsonl",
                "orion_mun_orbit": trial_dir / "orion_mun_orbit.telemetry.jsonl",
                "hls_surface_sortie": trial_dir / "hls_surface_sortie.telemetry.jsonl",
                "orion_return": trial_dir / "orion_return.telemetry.jsonl",
            }
            record = TrialRecord(
                trial_id=trial_id,
                mission=mission,
                design=orion_design,
                craft_path=f"{relay_path};{hls_path};{orion_path}",
                telemetry_path=str(phase_paths["orion_return"]),
                mode="offline" if self.offline else "ksp",
                status="running",
            )
            trial_dir.mkdir(parents=True, exist_ok=True)
            architecture_payload = plan.to_dict()
            design_by_key = {
                "relay": relay_design,
                "hls": hls_design,
                "orion": orion_design,
            }
            for vehicle in architecture_payload["vehicles"]:
                key = str(vehicle["key"])
                if key in design_by_key:
                    vehicle["design"] = design_by_key[key].to_dict()
            (trial_dir / "architecture.json").write_text(
                json.dumps(architecture_payload, indent=2),
                encoding="utf-8",
            )
            (trial_dir / "mission.json").write_text(json.dumps(mission.to_dict(), indent=2), encoding="utf-8")
            (trial_dir / "relay_design.json").write_text(json.dumps(relay_design.to_dict(), indent=2), encoding="utf-8")
            (trial_dir / "hls_design.json").write_text(json.dumps(hls_design.to_dict(), indent=2), encoding="utf-8")
            (trial_dir / "orion_design.json").write_text(json.dumps(orion_design.to_dict(), indent=2), encoding="utf-8")
            self.db.start_trial(record)

            phase_summaries: dict[str, TelemetrySummary] = {}
            try:
                if self.offline:
                    phase_summaries = self._offline_artemis_summaries(phase_paths)
                else:
                    assert bridge is not None
                    self._load_and_launch(bridge, relay_design.name)
                    phase_summaries["relay_predeploy"] = controller.fly(
                        artemis_phase_mission(mission, "artemis_mun_relay", "mun relay"),
                        relay_design,
                        phase_paths["relay_predeploy"],
                        timeout_s=int(self.config["runner"]["flight_timeout_s"]),
                    )

                    if self._phase_ok(phase_summaries["relay_predeploy"], "artemis_mun_relay_deployed"):
                        self._prepare_next_launch_without_revert(bridge)
                        self._load_and_launch(bridge, hls_design.name)
                        phase_summaries["hls_predeploy"] = controller.fly(
                            artemis_phase_mission(mission, "artemis_hls_predeploy", "hls predeploy"),
                            hls_design,
                            phase_paths["hls_predeploy"],
                            timeout_s=int(self.config["runner"]["flight_timeout_s"]),
                        )

                    if self._phase_ok(phase_summaries.get("hls_predeploy"), "artemis_hls_parked_in_mun_orbit"):
                        self._prepare_next_launch_without_revert(bridge)
                        self._load_and_launch(bridge, orion_design.name)
                        phase_summaries["orion_mun_orbit"] = controller.fly(
                            artemis_phase_mission(mission, "artemis_orion_mun_orbit_only", "orion mun orbit"),
                            orion_design,
                            phase_paths["orion_mun_orbit"],
                            timeout_s=int(self.config["runner"]["flight_timeout_s"]),
                        )

                    if self._phase_ok(phase_summaries.get("orion_mun_orbit"), "artemis_orion_waiting_in_mun_orbit"):
                        phase_summaries["hls_surface_sortie"] = controller.run_hls_surface_sortie(
                            hls_design.name,
                            phase_paths["hls_surface_sortie"],
                            timeout_s=int(self.config["runner"]["flight_timeout_s"]),
                        )

                    if self._phase_ok(phase_summaries.get("hls_surface_sortie"), "artemis_hls_returned_to_mun_orbit"):
                        phase_summaries["orion_return"] = controller.run_orion_return(
                            orion_design.name,
                            phase_paths["orion_return"],
                            timeout_s=int(self.config["runner"]["flight_timeout_s"]),
                        )

                score, telemetry = self._score_artemis_phases(mission, orion_design, phase_summaries)
                status = "succeeded" if score.success else "failed"
                self.db.finish_trial(trial_id, status, score, telemetry)
            except Exception as exc:
                self.db.mark_failed(trial_id, f"{type(exc).__name__}: {exc}")
                score = ScoreResult(0.0, False, str(exc), {"mission": 0.0})
                status = "failed"

            success_streak = success_streak + 1 if score.success else 0
            results.append(
                {
                    "trial_id": trial_id,
                    "status": status,
                    "success": score.success,
                    "score": score.score,
                    "failure_reason": score.failure_reason,
                    "relay_design": relay_design.name,
                    "hls_design": hls_design.name,
                    "orion_design": orion_design.name,
                    "relay_craft_path": str(relay_path),
                    "hls_craft_path": str(hls_path),
                    "orion_craft_path": str(orion_path),
                    "phase_telemetry": {key: str(path) for key, path in phase_paths.items()},
                    "phase_summaries": {key: summary.to_dict() for key, summary in phase_summaries.items()},
                }
            )
            if success_streak >= int(self.config["runner"].get("success_streak_required", mission.reliability_trials)):
                break

        return {
            "mission": mission.to_dict(),
            "success": success_streak >= int(self.config["runner"].get("success_streak_required", mission.reliability_trials)),
            "trials_run": len(results),
            "success_streak": success_streak,
            "results": results,
            "database": str(self.db.path),
        }

    def _load_and_launch(self, bridge: BridgeClient, craft_name: str) -> None:
        bridge.load_craft(craft_name)
        time.sleep(float(self.config["runner"].get("post_load_settle_s", 6)))
        self._wait_for_bridge_state(bridge, "loadedSceneIsEditor", True)
        bridge.launch()
        self._wait_for_bridge_state(bridge, "loadedSceneIsFlight", True)

    def _prepare_next_launch_without_revert(self, bridge: BridgeClient) -> None:
        try:
            bridge.space_center()
            time.sleep(2.0)
        except BridgeError:
            pass

    def _ksp_root_for_craft_sources(self, craft_dir: Path) -> Path:
        derived = ksp_root_from_save_vab(craft_dir)
        if (derived / "Ships" / "VAB").exists():
            return derived
        template_path = Path(self.config.get("craft_writer", {}).get("template_path", ""))
        if template_path.exists():
            return template_path.parent.parent.parent
        return derived

    def _write_artemis_relay_craft(self, design, craft_dir: Path) -> Path:
        # The relay is uncrewed and needs a simple expendable stack whose staging matches the
        # autostage controller. render() builds exactly that (probeCoreOcto.v2 command module +
        # clean liquid stages), and now splices each part's real KSP serialization from stock
        # craft (craft_writer._part_body_library) so it launches without the FinalizeAnalytics
        # NullReference. PT-Munsplorer template seeding is avoided: its multi-stage lander layout
        # made the autostage unreliable (under-staged at TMI, over-staged on ascent).
        # See docs/artemis_mun_engineering_notebook.md (2026-06-21).
        return self.writer.write(design, craft_dir, template_path=None)

    def _write_artemis_hls_craft(self, craft_name: str, craft_dir: Path) -> Path:
        ksp_root = self._ksp_root_for_craft_sources(craft_dir)
        return write_artemis_hls_craft(self.project_root, ksp_root, craft_dir, craft_name)

    def _write_artemis_orion_craft(self, craft_name: str, craft_dir: Path) -> Path:
        ksp_root = self._ksp_root_for_craft_sources(craft_dir)
        source = ksp_root / "Ships" / "VAB" / "Space Launch System Block 1.craft"
        return write_renamed_craft(
            source,
            craft_dir,
            craft_name,
            "Artemis Construction Kit SLS Block 1 / Orion craft prepared by ksp1-automation-lab.",
        )

    @staticmethod
    def _phase_ok(summary: TelemetrySummary | None, expected_phase: str) -> bool:
        return summary is not None and summary.mission_phase == expected_phase

    @staticmethod
    def _offline_artemis_summaries(phase_paths: dict[str, Path]) -> dict[str, TelemetrySummary]:
        phases = {
            "relay_predeploy": "artemis_mun_relay_deployed",
            "hls_predeploy": "artemis_hls_parked_in_mun_orbit",
            "orion_mun_orbit": "artemis_orion_waiting_in_mun_orbit",
            "hls_surface_sortie": "artemis_hls_returned_to_mun_orbit",
            "orion_return": "recovered",
        }
        summaries: dict[str, TelemetrySummary] = {}
        for key, phase in phases.items():
            recorder = TelemetryRecorder(phase_paths[key])
            recorder.append(
                {
                    "elapsed_s": 600.0,
                    "phase": phase,
                    "altitude_m": 12_000_000.0,
                    "apoapsis_m": 45_000.0,
                    "periapsis_m": 35_000.0,
                    "fuel_fraction_left": 0.25,
                    "landed": key == "hls_surface_sortie",
                    "recovered": key == "orion_return",
                    "science_completed": key == "hls_surface_sortie",
                    "relay_deployed": key == "relay_predeploy",
                    "offline": True,
                }
            )
            summaries[key] = recorder.summarize()
        return summaries

    @staticmethod
    def _score_artemis_phases(
        mission,
        design,
        phase_summaries: dict[str, TelemetrySummary],
    ) -> tuple[ScoreResult, TelemetrySummary]:
        expected = {
            "relay_predeploy": "artemis_mun_relay_deployed",
            "hls_predeploy": "artemis_hls_parked_in_mun_orbit",
            "orion_mun_orbit": "artemis_orion_waiting_in_mun_orbit",
            "hls_surface_sortie": "artemis_hls_returned_to_mun_orbit",
            "orion_return": "recovered",
        }
        components = {}
        first_failure = ""
        for key, expected_phase in expected.items():
            summary = phase_summaries.get(key)
            ok = summary is not None and summary.mission_phase == expected_phase
            if key == "orion_return" and summary is not None:
                ok = ok or summary.recovered
            if key == "relay_predeploy" and summary is not None:
                ok = ok or bool(summary.extra.get("relay_deployed", False))
            components[key] = 18.0 if ok else 0.0
            if not ok and not first_failure:
                first_failure = f"{key}:{summary.mission_phase if summary else 'not_started'}"

        sortie_summary = phase_summaries.get("hls_surface_sortie")
        science_ok = False
        if sortie_summary is not None:
            phases_seen = set(str(value) for value in sortie_summary.extra.get("phases_seen", []))
            science_ok = bool(sortie_summary.extra.get("science_completed", False)) or "mun_surface_science_completed" in phases_seen
        components["surface_science"] = 10.0 if science_ok else 0.0
        if not science_ok and not first_failure:
            first_failure = "surface_science:not_completed"

        fuel_margin = sum(summary.fuel_fraction_left for summary in phase_summaries.values())
        components["fuel_margin"] = min(10.0, max(0.0, fuel_margin * 2.5))
        components["cost"] = max(0.0, 5.0 - design.estimates.get("cost", 0.0) / 20_000.0)
        success = all(value > 0 for key, value in components.items() if key in expected) and science_ok
        telemetry = TelemetrySummary(
            max_altitude_m=max((summary.max_altitude_m for summary in phase_summaries.values()), default=0.0),
            apoapsis_m=max((summary.apoapsis_m for summary in phase_summaries.values()), default=0.0),
            periapsis_m=max((summary.periapsis_m for summary in phase_summaries.values()), default=-1.0),
            landed=bool(phase_summaries.get("hls_surface_sortie", TelemetrySummary()).landed),
            recovered=bool(phase_summaries.get("orion_return", TelemetrySummary()).recovered),
            vessel_destroyed=any(summary.vessel_destroyed for summary in phase_summaries.values()),
            mission_phase="recovered" if success else first_failure,
            elapsed_s=sum(summary.elapsed_s for summary in phase_summaries.values()),
            fuel_fraction_left=phase_summaries.get("orion_return", TelemetrySummary()).fuel_fraction_left,
            extra={
                "mission_type": mission.mission_type,
                "phase_summaries": {key: summary.to_dict() for key, summary in phase_summaries.items()},
            },
        )
        score = ScoreResult(round(sum(components.values()), 3), success, "" if success else first_failure, components)
        return score, telemetry

    def _craft_dir(self) -> Path:
        configured = self.config["paths"].get("ksp_save_ships_vab") or ""
        if configured and not self.offline:
            cfg = Path(configured).expanduser().resolve()
            # AUTO-DETECT the ACTIVE save. The bridge loads a craft from <saves>/<active>/Ships/VAB,
            # where <active> is whichever save KSP is currently running — i.e. the freshest
            # persistent.sfs. The configured path can name a STALE save (e.g. "默认" while the game is
            # on "codex-audit"), which makes /craft/load 404 even though the craft was written. Redirect
            # the write to the active save's VAB dir so the deploy follows whatever save is loaded.
            try:
                saves_root = next((p for p in cfg.parents if p.name == "saves"), None)
                if saves_root and saves_root.is_dir():
                    persists = [d / "persistent.sfs" for d in saves_root.iterdir() if d.is_dir()]
                    persists = [p for p in persists if p.exists()]
                    if persists:
                        vab = max(persists, key=lambda p: p.stat().st_mtime).parent / "Ships" / "VAB"
                        vab.mkdir(parents=True, exist_ok=True)
                        return vab.resolve()
            except Exception:
                pass
            return cfg
        return (self.run_dir / "generated_crafts").resolve()

    def _resolve(self, value: str | Path) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        return (self.project_root / path).resolve()

    def _external_provider(self) -> ExternalDesignProvider | None:
        command = self.config.get("optimizer", {}).get("external_command", "")
        if not command:
            return None
        kb = KnowledgeBase(self._resolve(self.config["paths"]["knowledge_base"]))
        return ExternalDesignProvider(
            command,
            knowledge_base=kb,
            timeout_s=int(self.config.get("optimizer", {}).get("external_timeout_s", 120)),
        )

    def _craft_template_path(self) -> Path | None:
        template = self.config.get("craft_writer", {}).get("template_path", "")
        if not template:
            return None
        return Path(template).expanduser().resolve()

    def _wait_for_bridge_state(self, bridge: BridgeClient, key: str, expected: bool) -> None:
        deadline = time.monotonic() + float(self.config["runner"].get("scene_transition_timeout_s", 60))
        last_state = {}
        while time.monotonic() < deadline:
            last_state = bridge.state()
            if bool(last_state.get(key)) is expected:
                return
            time.sleep(1.0)
        raise TimeoutError(f"Timed out waiting for bridge state {key}={expected}; last state: {last_state}")
