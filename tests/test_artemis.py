from pathlib import Path

from ksp_lab.artemis import build_artemis_architecture
from ksp_lab.craft_writer import CraftWriter
from ksp_lab.mission import MissionPlanner
from ksp_lab.runner import AutomationRunner


def test_artemis_architecture_has_hls_and_orion_designs():
    mission = MissionPlanner().interpret("Artemis SLS Orion Starship HLS Mun landing and return")
    plan = build_artemis_architecture(mission)

    relay = plan.vehicle("relay")
    hls = plan.vehicle("hls")
    orion = plan.vehicle("orion")

    assert relay.launch_order == 1
    assert relay.design.mission_type == "artemis_mun_relay"
    assert "relay" in relay.design.tags
    assert hls.launch_order == 2
    assert hls.design.mission_type == "artemis_hls_predeploy"
    assert "hls" in hls.design.tags
    assert orion.launch_order == 3
    assert orion.design.mission_type == "artemis_orion_sls_return"
    # Probe-controlled (headless-launchable; crew modeled) but carries a heat shield for Kerbin
    # reentry/recovery.
    assert not orion.design.crewed
    assert orion.design.heatshield
    assert orion.design.estimates["delta_v_mps"] > 8000


def test_artemis_relay_live_craft_is_probe_controlled_render_stack(tmp_path: Path):
    # The relay is uncrewed: render() must produce a probe-controlled simple expendable stack
    # (probe-core command module, no crewed pod, no solid boosters) whose staging matches the
    # autostage controller. PT-Munsplorer template seeding is no longer used.
    mission = MissionPlanner().interpret("Artemis SLS Orion Starship HLS Mun landing and return")
    relay_design = build_artemis_architecture(mission).vehicle("relay").design
    relay_design.name = "AI-Mun-Relay-Test"

    runner = AutomationRunner.__new__(AutomationRunner)
    runner.writer = CraftWriter()
    runner.config = {"craft_writer": {"template_path": ""}}

    path = AutomationRunner._write_artemis_relay_craft(runner, relay_design, tmp_path)
    text = path.read_text(encoding="utf-8")

    assert text.startswith("ship = AI-Mun-Relay-Test\n")
    assert "probeCoreOcto.v2" in text  # probe-core command module (headless-controllable)
    assert "mk1pod" not in text  # not a crewed pod
    assert "SolidFuel" not in text  # liquid-only stages match the controller
    assert "Space Launch System Block 1B Cargo" not in text
    assert "attN =" in text and "RESOURCE" in text
