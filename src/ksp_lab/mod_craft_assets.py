from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .craft_writer import validate_craft_name


@dataclass(frozen=True, slots=True)
class CraftSource:
    path: Path
    description_prefix: str
    source_key: str
    notes: str


def ksp_root_from_save_vab(save_vab_dir: Path) -> Path:
    """Resolve KSP root from saves/<save>/Ships/VAB."""

    path = save_vab_dir.resolve()
    if len(path.parents) < 4:
        raise ValueError(f"Cannot derive KSP root from craft directory: {path}")
    # VAB -> Ships -> save folder -> saves -> KSP root
    return path.parents[3]


def write_renamed_craft(source: Path, target_dir: Path, craft_name: str, description_prefix: str) -> Path:
    name = validate_craft_name(craft_name)
    if not source.exists():
        raise FileNotFoundError(f"Source craft not found: {source}")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{name}.craft"
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines or not lines[0].startswith("ship = "):
        raise ValueError(f"Source craft does not look like a KSP craft file: {source}")
    lines[0] = f"ship = {name}"
    replaced_description = False
    for idx, line in enumerate(lines):
        if line.startswith("description = "):
            lines[idx] = f"description = {description_prefix} Source craft: {source.name}."
            replaced_description = True
            break
    if not replaced_description:
        lines.insert(2, f"description = {description_prefix} Source craft: {source.name}.")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return target


def select_artemis_hls_craft_source(project_root: Path, ksp_root: Path) -> CraftSource | None:
    """Pick the best known Starship/HLS craft source available locally."""

    candidates = [
        CraftSource(
            path=project_root / "work" / "downloads" / "mega" / "MUNSHIP.craft",
            description_prefix=(
                "Downloaded Starship Moonship/HLS reference craft from Matt Lowne public video description, "
                "prepared by ksp1-automation-lab for an Artemis-style HLS predeployment test."
            ),
            source_key="matt-lowne-munship-public-video-link",
            notes=(
                "Public Moonship craft used as the first live HLS/Starship analogue because it has a complete "
                "KSP-authored craft serialization and loaded successfully through the bridge."
            ),
        ),
        CraftSource(
            path=ksp_root / "Ships" / "VAB" / "Kerbal Landing System.craft",
            description_prefix=(
                "Artemis Construction Kit Kerbal Landing System craft prepared by ksp1-automation-lab as a "
                "known-loadable HLS fallback."
            ),
            source_key="artemis-construction-kit-kerbal-landing-system",
            notes=(
                "Known-loadable Artemis Construction Kit HLS fallback. It is lander-like but not a full "
                "Starship/Super Heavy predeployment stack."
            ),
        ),
    ]
    for candidate in candidates:
        if candidate.path.exists():
            return candidate
    return None


def write_artemis_hls_craft(project_root: Path, ksp_root: Path, target_dir: Path, craft_name: str) -> Path:
    source = select_artemis_hls_craft_source(project_root, ksp_root)
    if source is not None:
        return write_renamed_craft(source.path, target_dir, craft_name, source.description_prefix)
    return HlsProjectCraftWriter().write(ksp_root, target_dir, craft_name)


class HlsProjectCraftWriter:
    """Experimental fallback generator for a minimal HLS Project parts craft."""

    def write(self, ksp_root: Path, target_dir: Path, craft_name: str) -> Path:
        self._validate_installed(ksp_root)
        name = validate_craft_name(craft_name)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{name}.craft"
        target.write_text(self.render(name), encoding="utf-8", newline="\n")
        return target

    @staticmethod
    def _validate_installed(ksp_root: Path) -> None:
        required = [
            ksp_root / "GameData" / "HLS_project" / "parts" / "nose_cone" / "HLS_NOSE_CONE.cfg",
            ksp_root / "GameData" / "HLS_project" / "parts" / "Main_tank" / "Main_Fuel_Tank_HLS.cfg",
            ksp_root / "GameData" / "HLS_project" / "parts" / "engine_hls" / "engine.cfg",
        ]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise FileNotFoundError("HLS Project is not installed or is incomplete: " + "; ".join(missing))

    def render(self, craft_name: str) -> str:
        parts = [
            _Part("HLS_NOSE_CONE", 4294800000, (0.0, 16.0, 0.0), -1, 0, -1, -1, -1),
            _Part("HLS_MAIN_TANK", 4294799800, (0.0, 7.5, 0.0), -1, 0, -1, -1, -1),
            _Part("draco_hls", 4294799600, (0.0, -2.0, 0.0), 0, 0, 0, 0, 0),
            _Part("draco_hls", 4294799400, (2.0, -2.0, 0.0), 0, 0, 1, 0, 0),
            _Part("draco_hls", 4294799200, (-2.0, -2.0, 0.0), 0, 0, 2, 0, 0),
            _Part("draco_hls", 4294799000, (0.0, -2.0, 2.0), 0, 0, 3, 0, 0),
            _Part("draco_hls", 4294798800, (0.0, -2.0, -2.0), 0, 0, 4, 0, 0),
            _Part("draco_hls", 4294798600, (1.414, -2.0, 1.414), 0, 0, 5, 0, 0),
        ]
        nose, tank, *engines = parts
        nose.links = [tank]
        nose.attached_nodes = [("node_bottom", tank, "0|-7.5|0_0|-1|0_0|-7.5|0_0|-1|0")]
        tank.links = engines
        tank.attached_nodes = [("node_top", nose, "0|7.5|0_0|1|0_0|7.5|0_0|1|0")]
        engine_nodes = ["rv1S", "rv2S", "rv3S", "rv4S", "rv5S", "rv6S"]
        for node_name, engine in zip(engine_nodes, engines):
            tank.attached_nodes.append((node_name, engine, "0|-7.5|0_0|-1|0_0|-7.5|0_0|-1|0"))
            engine.attached_nodes = [("node_top", tank, "0|0|0_0|1|0_0|0|0_0|1|0")]

        lines = [
            f"ship = {craft_name}",
            "version = 1.12.5",
            "description = Generated by ksp1-automation-lab from HLS Project parts: HLS nose, main tank, and six HLS landing engines. Not a stock/default craft template.",
            "type = VAB",
            "size = 9.0,22.0,9.0",
            "steamPublishedFileId = 0",
            "persistentId = 2147483001",
            "rot = 0,0,0,1",
            "missionFlag = Squad/Flags/default",
            "vesselType = Lander",
            "OverrideDefault = False,False,False,False",
            "OverrideActionControl = 0,0,0,0",
            "OverrideAxisControl = 0,0,0,0",
            "OverrideGroupNames = ,,,",
        ]
        for part in parts:
            lines.extend(self._render_part(part))
        lines.extend(["STAGES", "{", "\tstage = 0", "}", ""])
        return "\n".join(lines)

    def _render_part(self, part: "_Part") -> list[str]:
        x, y, z = part.pos
        lines = [
            "PART",
            "{",
            f"\tpart = {part.craft_id}",
            "\tpartName = Part",
            f"\tpersistentId = {part.uid}",
            f"\tpos = {x:.6g},{y:.6g},{z:.6g}",
            "\tattPos = 0,0,0",
            f"\tattPos0 = {x:.6g},{y:.6g},{z:.6g}",
            "\trot = 0,0,0,1",
            "\tattRot = 0,0,0,1",
            "\tattRot0 = 0,0,0,1",
            "\tmir = 1,1,1",
            "\tsymMethod = Radial",
            "\tautostrutMode = Grandparent",
            "\trigidAttachment = False",
            f"\tistg = {part.istg}",
            "\tresPri = 0",
            f"\tdstg = {part.dstg}",
            f"\tsidx = {part.sidx}",
            f"\tsqor = {part.sqor}",
            f"\tsepI = {part.sepI}",
            "\tattm = 0",
            "\tsameVesselCollision = False",
            "\tmodCost = 0",
            "\tmodMass = 0",
            "\tmodSize = 0,0,0",
        ]
        for child in part.links:
            lines.append(f"\tlink = {child.craft_id}")
        for node_name, other, node_data in part.attached_nodes:
            lines.append(f"\tattN = {node_name},{other.craft_id}_{node_data}")
        lines.extend(["\tEVENTS", "\t{", "\t}", "\tACTIONS", "\t{", "\t}", "\tPARTDATA", "\t{", "\t}"])
        if part.part_name == "HLS_MAIN_TANK":
            lines.extend(self._resource("LiquidFuel", 7200))
            lines.extend(self._resource("Oxidizer", 8800))
        if part.part_name == "HLS_NOSE_CONE":
            lines.extend(self._resource("ElectricCharge", 5000))
        lines.append("}")
        return lines

    @staticmethod
    def _resource(name: str, amount: float) -> list[str]:
        return [
            "\tRESOURCE",
            "\t{",
            f"\t\tname = {name}",
            f"\t\tamount = {amount}",
            f"\t\tmaxAmount = {amount}",
            "\t\tflowState = True",
            "\t\tisTweakable = True",
            "\t\thideFlow = False",
            "\t\tisVisible = True",
            "\t\tflowMode = Both",
            "\t}",
        ]


class _Part:
    def __init__(
        self,
        part_name: str,
        uid: int,
        pos: tuple[float, float, float],
        istg: int,
        dstg: int,
        sidx: int,
        sqor: int,
        sepI: int,
    ):
        self.part_name = part_name
        self.uid = uid
        self.pos = pos
        self.istg = istg
        self.dstg = dstg
        self.sidx = sidx
        self.sqor = sqor
        self.sepI = sepI
        self.links: list[_Part] = []
        self.attached_nodes: list[tuple[str, _Part, str]] = []

    @property
    def craft_id(self) -> str:
        return f"{self.part_name.replace('_', '.')}_{self.uid}"
