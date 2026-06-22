from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import RocketDesign
from .parts import STOCK_PARTS, part

CRAFT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$")


class CraftValidationError(ValueError):
    pass


def _part_block_lines(text: str, part_name: str) -> list[str] | None:
    """Return the lines of the first depth-balanced ``PART {...}`` block whose
    ``part = <part_name>_<uid>`` line matches ``part_name`` exactly (the uid digit after the
    underscore guards against prefix collisions like fuelTank vs fuelTank.long)."""

    lines = text.splitlines()
    needle = f"part = {part_name}_"
    i = 0
    while i < len(lines):
        if lines[i].strip() == "PART" and i + 1 < len(lines) and lines[i + 1].strip() == "{":
            depth = 0
            j = i + 1
            while j < len(lines):
                stripped = lines[j].strip()
                if stripped == "{":
                    depth += 1
                elif stripped == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            block = lines[i : j + 1]
            for ln in block:
                s = ln.strip()
                if s.startswith(needle) and s[len(needle) :][:1].isdigit():
                    return block
            i = j + 1
        else:
            i += 1
    return None


def _extract_part_body(text: str, part_name: str) -> str | None:
    """Extract a real part's module/resource body so it can be spliced under a generated header.

    Returns the lines from the part's ``EVENTS`` block to just before its closing brace
    (EVENTS/ACTIONS/PARTDATA/MODULE.../RESOURCE...), preserving KSP's own serialization so the
    editor's launch finalization (``EditorLogic.FinalizeAnalytics``) does not NullReference on a
    generated craft."""

    block = _part_block_lines(text, part_name)
    if block is None:
        return None
    start = None
    for idx, ln in enumerate(block):
        if ln.strip() == "EVENTS":
            start = idx
            break
    if start is None:
        return None
    return "\n".join(block[start:-1])  # drop the final closing "}" of the PART block


@dataclass(slots=True)
class CraftNode:
    part_name: str
    uid: int
    stage_index: int
    y: float = 0.0
    parent: "CraftNode | None" = None
    parent_node: str = ""
    child_node: str = ""
    children: list["CraftNode"] = field(default_factory=list)
    # Surface (radial) attachment: when set, the part is srfAttach-mounted to srf_parent at an
    # explicit (x, y, z) position and orientation, instead of stack-attached via attN nodes.
    srf_parent: "CraftNode | None" = None
    pos_xyz: tuple[float, float, float] | None = None
    rot_quat: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)

    @property
    def craft_id(self) -> str:
        return f"{self.part_name}_{self.uid}"

    @property
    def is_surface(self) -> bool:
        return self.srf_parent is not None


def validate_craft_name(name: str) -> str:
    cleaned = name.strip()
    if not CRAFT_NAME_RE.match(cleaned):
        raise CraftValidationError(
            "Craft name must be 1-80 characters and may contain letters, digits, spaces, underscores, dots, and hyphens."
        )
    if ".." in cleaned or "/" in cleaned or "\\" in cleaned:
        raise CraftValidationError("Craft name cannot contain path separators or traversal.")
    return cleaned


def resolve_craft_path(save_vab_dir: str | Path, craft_name: str) -> Path:
    name = validate_craft_name(craft_name)
    base = Path(save_vab_dir).resolve()
    target = (base / f"{name}.craft").resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise CraftValidationError(f"Craft path escapes save folder: {target}") from exc
    return target


class CraftWriter:
    def write(self, design: RocketDesign, save_vab_dir: str | Path, template_path: str | Path | None = None) -> Path:
        target = resolve_craft_path(save_vab_dir, design.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if template_path:
            text = self.render_from_template(design, template_path)
        else:
            text = self.render(design, part_bodies=self._part_body_library(design, save_vab_dir))
        target.write_text(text, encoding="utf-8", newline="\n")
        return target

    @staticmethod
    def _design_part_names(design: RocketDesign) -> set[str]:
        names = {"parachuteSingle", "Decoupler.1", "ServiceBay.125.v2"}
        names.add("mk1pod.v2" if design.crewed else "probeCoreOcto.v2")
        if design.crewed:
            names.add("probeCoreOcto.v2")  # inline control source for headless crewed launch
            names.add("crewCabin")  # extra seats so astronauts can be transferred between modules
        if design.crewed or design.heatshield:
            names.add("HeatShield1")
        # Avionics/power/comms bus + aero fins added by _build_nodes; harvest their real
        # serializations too so they are not skipped (can_emit) or spliced module-less.
        names.update({"longAntenna", "solarPanels5", "batteryBankMini", "basicFin", "asasmodule1-2"})
        if design.landing_legs:
            names.add("landingLeg1")
        if design.docking_port:
            names.update({"dockingPort2", "RCSBlock", "rcsTankRadialLong"})
        names.add("noseCone")
        for stage in design.stages:
            names.add(stage.engine)
            names.add(stage.tank)
        return names

    @staticmethod
    def _part_source_dirs(save_vab_dir: str | Path) -> list[Path]:
        """Real KSP-authored craft to harvest part serializations from: the stock Ships/VAB
        library plus the save's own VAB folder."""

        dirs: list[Path] = []
        base = Path(save_vab_dir)
        try:
            from .mod_craft_assets import ksp_root_from_save_vab

            stock_vab = ksp_root_from_save_vab(base) / "Ships" / "VAB"
            if stock_vab.exists():
                dirs.append(stock_vab)
        except Exception:
            pass
        if base.exists():
            dirs.append(base)
        return dirs

    def _part_body_library(self, design: RocketDesign, save_vab_dir: str | Path) -> dict[str, str] | None:
        """Harvest complete module/resource serializations for the design's parts from real
        craft. Returns None when no sources are available (e.g. offline tests), so render()
        falls back to the minimal serialization."""

        needed = set(self._design_part_names(design))
        bodies: dict[str, str] = {}
        for directory in self._part_source_dirs(save_vab_dir):
            if not needed:
                break
            try:
                crafts = sorted(directory.glob("*.craft"))
            except Exception:
                continue
            for craft in crafts:
                if not needed:
                    break
                # Skip our own generated AI-* crafts: they may carry the same minimal bodies.
                if craft.name.startswith("AI-"):
                    continue
                try:
                    text = craft.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                for part_name in list(needed):
                    body = _extract_part_body(text, part_name)
                    if body:
                        bodies[part_name] = body
                        needed.discard(part_name)
        return bodies or None

    def render_from_template(self, design: RocketDesign, template_path: str | Path | None) -> str:
        if not template_path:
            return self.render(design)
        template = Path(template_path)
        if not template.exists():
            raise CraftValidationError(f"Craft template not found: {template}")
        lines = template.read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines or not lines[0].startswith("ship = "):
            raise CraftValidationError(f"Craft template does not look like a KSP craft file: {template}")
        lines[0] = f"ship = {validate_craft_name(design.name)}"
        for idx, line in enumerate(lines):
            if line.startswith("description = "):
                lines[idx] = f"description = Template-seeded by ksp1-automation-lab from {template.name}. {design.notes}"
                break
        reserve_template_missions = {"mun_landing_return", "artemis_hls_predeploy"}
        if design.mission_type in reserve_template_missions and template.name == "PT Series Munsplorer.craft":
            lines = self._add_pt_munsplorer_lander_reserve(lines)
        return "\n".join(lines) + "\n"

    @staticmethod
    def _add_pt_munsplorer_lander_reserve(lines: list[str]) -> list[str]:
        target_parts = {"fuelTankSmallFlat_4294629874", "fuelTankSmall_4294644996"}
        reserves = {
            "fuelTankSmallFlat_4294629874": {"LiquidFuel": "90", "Oxidizer": "110"},
            "fuelTankSmall_4294644996": {"LiquidFuel": "180", "Oxidizer": "220"},
        }
        updated = list(lines)
        current_part = ""
        current_resource = ""
        in_target_part = False
        for idx, line in enumerate(updated):
            stripped = line.strip()
            if stripped.startswith("part = "):
                current_part = stripped.split("=", 1)[1].strip()
                in_target_part = current_part in target_parts
                current_resource = ""
                continue
            if not in_target_part:
                continue
            if stripped.startswith("name = "):
                resource_name = stripped.split("=", 1)[1].strip()
                if resource_name in reserves[current_part]:
                    current_resource = resource_name
                continue
            if current_resource and (stripped.startswith("amount = ") or stripped.startswith("maxAmount = ")):
                key = stripped.split("=", 1)[0].strip()
                updated[idx] = f"\t\t{key} = {reserves[current_part][current_resource]}"
        return updated

    def render(self, design: RocketDesign, part_bodies: dict[str, str] | None = None) -> str:
        validate_craft_name(design.name)
        nodes = self._build_nodes(design, part_bodies)
        lines: list[str] = [
            f"ship = {design.name}",
            "version = 1.12.5",
            f"description = Generated by ksp1-automation-lab. {design.notes}",
            "type = VAB",
            "size = 1.25,10,1.25",
            "steamPublishedFileId = 0",
            "persistentId = 2147483001",
            "rot = 0,0,0,1",
            "missionFlag = Squad/Flags/default",
            f"vesselType = {'Ship' if design.crewed else 'Probe'}",
        ]
        # NOTE: do not emit a top-level ACTIONGROUPS block or the Override* header fields.
        # Real KSP-authored craft omit them, and the malformed ACTIONGROUPS block made the
        # editor's launch finalization (EditorLogic.FinalizeAnalytics) NullReference, so every
        # generated craft failed to launch. (2026-06-21)
        for node in nodes:
            lines.extend(self._render_part(node, part_bodies))
        # Real KSP .craft files have no top-level STAGES block (staging is encoded per-part via
        # istg/dstg). Emitting one made the editor's launch finalization NullReference.
        lines.append("")
        return "\n".join(lines)

    def _build_nodes(self, design: RocketDesign, part_bodies: dict[str, str] | None = None) -> list[CraftNode]:
        uid = 4294900000

        def new_node(part_name: str, stage_index: int) -> CraftNode:
            nonlocal uid
            if part_name not in STOCK_PARTS:
                raise CraftValidationError(f"Unknown stock part in design: {part_name}")
            node = CraftNode(part_name=part_name, uid=uid, stage_index=stage_index)
            uid -= 173
            return node

        root = new_node("mk1pod.v2" if design.crewed else "probeCoreOcto.v2", 0)
        nodes = [root]

        chute = new_node("parachuteSingle", 0)
        if design.docking_port:
            # Docking craft reserve the nose (top node) for the docking port — the mating surface —
            # so the recovery chute mounts radially on the command pod instead of on the nose.
            chute_r = part(root.part_name).height_m * 0.55 + 0.15
            self._attach_surface(root, chute, (chute_r, root.y, 0.0))
        else:
            self._attach(root, chute, "top", "bottom", up=True)
        nodes.append(chute)

        current = root
        # A crew cabin gives extra seats (the 1-seat pod alone leaves nowhere to transfer kerbals).
        # It sits DIRECTLY BELOW THE POD — i.e. ABOVE the capsule decoupler — so ALL crew (pod + cabin)
        # ride inside the reentry capsule and the whole crew comes home, not just the pod's one seat.
        if design.crewed and (part_bodies is None or "crewCabin" in part_bodies):
            cabin = new_node("crewCabin", 0)
            self._attach(current, cabin, "bottom", "top")
            nodes.append(cabin)
            current = cabin
        if design.crewed or design.heatshield:
            heat = new_node("HeatShield1", 0)
            self._attach(current, heat, "bottom", "top")
            nodes.append(heat)
            current = heat
            # SEPARABLE CREW CAPSULE: a decoupler at the BASE of the pod+cabin+heatshield capsule lets
            # the return driver JETTISON the probe/service section before reentry, so the reentry
            # vehicle is the short, stable capsule whose chute lands the WHOLE crew. Without this the
            # long command bus tumbles and the crew pod splits off chuteless. Inverse-stage 0 so it
            # never auto-fires during ascent/staging — the driver triggers it manually.
            if design.crewed:
                capsule_decoupler = new_node("Decoupler.1", 0)
                self._attach(current, capsule_decoupler, "bottom", "top")
                nodes.append(capsule_decoupler)
                current = capsule_decoupler
        if design.crewed:
            # Inline probe core BELOW the decoupler: a guaranteed control source for the headless
            # launch (before crew portraits settle). It stays with the jettisoned service section —
            # fine, because the crewed pod controls the capsule after separation.
            probe = new_node("probeCoreOcto.v2", 0)
            self._attach(current, probe, "bottom", "top")
            nodes.append(probe)
            current = probe

        # Payload service bays model payload mass. Only emit them when a real serialization is
        # available (or when running with no part-body library at all, i.e. offline/minimal),
        # so we never splice a minimal ServiceBay that would re-trigger the launch NullReference.
        service_bay_ok = part_bodies is None or "ServiceBay.125.v2" in part_bodies
        if service_bay_ok:
            payload_units = max(0, round(design.payload_mass_t / 0.1))
            for _ in range(min(payload_units, 8)):
                payload = new_node("ServiceBay.125.v2", 0)
                self._attach(current, payload, "bottom", "top")
                nodes.append(payload)
                current = payload

        # Reaction wheels INLINE in the command bus (stack-attached, no clipping). The probe core
        # alone is far too weak to align this heavy upper stack to prograde/retrograde before a
        # finite burn, so burns ignited off-axis and lost energy (TMI and capture both). One large
        # wheel (~5 kN*m) is only borderline for the ~30 t transfer stack, so stack three (~15
        # kN*m) for comfortable, fast alignment. They stay with the satellite (inverse-stage 0).
        if part_bodies is None or "asasmodule1-2" in part_bodies:
            for _ in range(3):
                reaction_wheel = new_node("asasmodule1-2", 0)
                self._attach(current, reaction_wheel, "bottom", "top")
                nodes.append(reaction_wheel)
                current = reaction_wheel

        # Designs list stages in ignition order, so render them from upper stage down.
        rendered_stages = list(reversed(design.stages))
        bottom_tank: CraftNode | None = None
        lander_tank: CraftNode | None = None  # tank of the top stage that STAYS (the lander)
        for render_index, stage in enumerate(rendered_stages, start=1):
            if stage.decoupler_above:
                # The inter-stage decoupler must ACTIVATE one stage later than the engine/tanks it
                # sits above (it fires when the stage below it is spent and the next engine lights),
                # NOT at the same inverse-stage as those parts. Giving it the stage's own index made
                # the launch-stage decoupler fire at lift-off and split the craft on the pad. Use a
                # higher inverse-stage (= render_index - 1) so it fires after, not during, this stage.
                decoupler = new_node("Decoupler.1", max(0, render_index - 1))
                self._attach(current, decoupler, "bottom", "top")
                nodes.append(decoupler)
                current = decoupler
            for _ in range(stage.tank_count):
                tank = new_node(stage.tank, render_index)
                self._attach(current, tank, "bottom", "top")
                nodes.append(tank)
                current = tank
                bottom_tank = tank  # last tank built = bottom of the first-ignition (launch) stage
                if render_index == 1:
                    lander_tank = tank  # top propulsive stage = the part that stays = the lander
            engine = new_node(stage.engine, render_index)
            self._attach(current, engine, "bottom", "top")
            nodes.append(engine)
            current = engine

        def can_emit(part_name: str) -> bool:
            # Only attach a part when a real serialization is available (or in minimal mode),
            # so we never splice a module-less body that re-triggers the launch NullReference.
            return part_bodies is None or part_name in part_bodies

        # Avionics / power / comms bus on the command part: a satellite needs a comm link, power
        # generation, and storage to function and stay controllable away from Kerbin. Mounted
        # radially on the command module so it adds negligible ascent drag.
        bus_radius = part(root.part_name).height_m * 0.6 + 0.25
        bus_y = root.y - 0.05
        bus_layout = [
            ("longAntenna", (bus_radius, bus_y, 0.0), (0.0, 0.0, 0.0, 1.0)),
            ("batteryBankMini", (-bus_radius, bus_y, 0.0), (0.0, 0.0, 0.0, 1.0)),
            ("solarPanels5", (0.0, bus_y, bus_radius), (0.0, 0.0, 0.0, 1.0)),
            ("solarPanels5", (0.0, bus_y, -bus_radius), (0.0, 1.0, 0.0, 0.0)),
            # NOTE: a separate large reaction-wheel module was tried here but, surface-mounted on
            # the small probe core, it clipped badly and destabilised the craft. Attitude authority
            # for finite burns is instead handled in the controller: the TMI burn re-aligns and
            # resumes at low throttle so the engine gimbal holds prograde (_execute_node).
        ]
        for part_name, pos, rot in bus_layout:
            if can_emit(part_name):
                acc = new_node(part_name, 0)
                self._attach_surface(root, acc, pos, rot)
                nodes.append(acc)

        # RCS translation authority for docking: four thruster blocks at 90 deg around the command
        # pod plus a monopropellant tank. A docking craft must translate laterally to align ports —
        # reaction wheels rotate but cannot translate, and the main engine only pushes axially.
        if design.docking_port and can_emit("RCSBlock"):
            rcs_r = part(root.part_name).height_m * 0.5 + 0.18
            rcs_y = root.y - 0.02
            sqrt_half = 0.70710678
            rcs_layout = [
                ((rcs_r, rcs_y, 0.0), (0.0, 0.0, 0.0, 1.0)),
                ((-rcs_r, rcs_y, 0.0), (0.0, 1.0, 0.0, 0.0)),
                ((0.0, rcs_y, rcs_r), (0.0, sqrt_half, 0.0, sqrt_half)),
                ((0.0, rcs_y, -rcs_r), (0.0, sqrt_half, 0.0, -sqrt_half)),
            ]
            for pos, rot in rcs_layout:
                blk = new_node("RCSBlock", 0)
                self._attach_surface(root, blk, pos, rot)
                nodes.append(blk)
            if can_emit("rcsTankRadialLong"):
                tank = new_node("rcsTankRadialLong", 0)
                self._attach_surface(root, tank, (0.0, root.y - 0.4, rcs_r))
                nodes.append(tank)

        # Aerodynamic stabilisers: four PASSIVE base fins put the centre-of-pressure behind the
        # centre-of-mass so the gravity turn is stable. Passive fins (basicFin), not active control
        # surfaces (AV-R8): a weak probe-core autopilot driving large control surfaces over-rotates
        # and tumbles the stack on launch.
        if bottom_tank is not None and can_emit("basicFin"):
            # Mount fins just outboard of the tank wall (Rockomax/large tanks are ~2.5 m across).
            fin_radius = 1.5
            fin_y = bottom_tank.y - part(bottom_tank.part_name).height_m * 0.25
            sqrt_half = 0.70710678
            fin_layout = [
                ((fin_radius, fin_y, 0.0), (0.0, 0.0, 0.0, 1.0)),
                ((-fin_radius, fin_y, 0.0), (0.0, 1.0, 0.0, 0.0)),
                ((0.0, fin_y, fin_radius), (0.0, sqrt_half, 0.0, sqrt_half)),
                ((0.0, fin_y, -fin_radius), (0.0, sqrt_half, 0.0, -sqrt_half)),
            ]
            for pos, rot in fin_layout:
                fin = new_node("basicFin", bottom_tank.stage_index)
                self._attach_surface(bottom_tank, fin, pos, rot)
                nodes.append(fin)

        # Landing legs for a lander (HLS): four legs around the LANDER stage tank (the top stage
        # that stays after the booster/transfer drop), so the craft can touch down on its own
        # descent engine. inverse-stage 0 = always deployed/kept with the lander.
        if design.landing_legs and lander_tank is not None and can_emit("landingLeg1"):
            leg_radius = part(lander_tank.part_name).height_m * 0.12 + 1.35
            leg_y = lander_tank.y - part(lander_tank.part_name).height_m * 0.45
            sqrt_half = 0.70710678
            leg_layout = [
                ((leg_radius, leg_y, 0.0), (0.0, 0.0, 0.0, 1.0)),
                ((-leg_radius, leg_y, 0.0), (0.0, 1.0, 0.0, 0.0)),
                ((0.0, leg_y, leg_radius), (0.0, sqrt_half, 0.0, sqrt_half)),
                ((0.0, leg_y, -leg_radius), (0.0, sqrt_half, 0.0, -sqrt_half)),
            ]
            for pos, rot in leg_layout:
                leg = new_node("landingLeg1", 0)
                self._attach_surface(lander_tank, leg, pos, rot)
                nodes.append(leg)

        if design.docking_port and can_emit("dockingPort2"):
            # Clamp-O-Tron on the nose = the mating surface for an in-orbit rendezvous (Orion<->HLS
            # crew transfer). It IS the streamlined nose, so no separate nose cone. inverse-stage 0.
            dock = new_node("dockingPort2", 0)
            self._attach(root, dock, "top", "bottom", up=True)
            nodes.append(dock)
        elif can_emit("noseCone"):
            # Nose cone above the parachute for streamlining (owner's aero requirement). Attached to
            # the chute's top node (the command's top node already holds the chute).
            nose = new_node("noseCone", 0)
            self._attach(chute, nose, "top", "bottom", up=True)
            nodes.append(nose)

        return nodes

    @staticmethod
    def _attach(parent: CraftNode, child: CraftNode, parent_node: str, child_node: str, up: bool = False) -> None:
        child.parent = parent
        child.parent_node = parent_node
        child.child_node = child_node
        offset = (part(parent.part_name).height_m + part(child.part_name).height_m) / 2
        child.y = parent.y + offset if up else parent.y - offset
        parent.children.append(child)

    @staticmethod
    def _attach_surface(
        parent: CraftNode,
        child: CraftNode,
        pos_xyz: tuple[float, float, float],
        rot_quat: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0),
    ) -> None:
        child.srf_parent = parent
        child.pos_xyz = pos_xyz
        child.rot_quat = rot_quat
        child.y = pos_xyz[1]
        parent.children.append(child)

    def _render_part(self, node: CraftNode, part_bodies: dict[str, str] | None = None) -> list[str]:
        p = part(node.part_name)
        if node.pos_xyz is not None:
            pos_str = f"{node.pos_xyz[0]:.4f},{node.pos_xyz[1]:.4f},{node.pos_xyz[2]:.4f}"
        else:
            pos_str = f"0,{node.y:.4f},0"
        rot_str = ",".join(f"{v:g}" for v in node.rot_quat)
        lines = [
            "PART",
            "{",
            f"\tpart = {node.craft_id}",
            "\tpartName = Part",
            f"\tpersistentId = {node.uid}",
            f"\tpos = {pos_str}",
            "\tattPos = 0,0,0",
            f"\tattPos0 = {pos_str}",
            f"\trot = {rot_str}",
            "\tattRot = 0,0,0,1",
            "\tattRot0 = 0,0,0,1",
            "\tmir = 1,1,1",
            "\tsymMethod = Radial",
            "\tautostrutMode = Off",
            "\trigidAttachment = False",
            f"\tistg = {node.stage_index}",
            "\tresPri = 0",
            f"\tdstg = {node.stage_index}",
            "\tsidx = -1",
            "\tsqor = -1",
            "\tsepI = -1",
            "\tattm = 0",
            "\tmodCost = 0",
            "\tmodMass = 0",
            "\tmodSize = 0,0,0",
        ]
        for child in node.children:
            lines.append(f"\tlink = {child.craft_id}")
        if node.is_surface:
            lines.append(f"\tsrfN = srfAttach,{node.srf_parent.craft_id}")
        elif node.parent is not None:
            lines.append(f"\tattN = {node.child_node},{node.parent.craft_id}_{self._node_position(node.part_name, node.child_node)}")
        for child in node.children:
            if child.is_surface:
                continue  # surface-attached children carry their own srfN; parent only links them
            lines.append(f"\tattN = {child.parent_node},{child.craft_id}_{self._node_position(node.part_name, child.parent_node)}")
        body = (part_bodies or {}).get(node.part_name)
        if body:
            # Splice the part's real KSP serialization (EVENTS/ACTIONS/PARTDATA/MODULE/RESOURCE)
            # so launch finalization has full module state and does not NullReference.
            lines.extend(body.split("\n"))
        else:
            lines.extend(["\tEVENTS", "\t{", "\t}", "\tACTIONS", "\t{", "\t}", "\tPARTDATA", "\t{", "\t}"])
            lines.extend(self._resources(p))
        lines.append("}")
        return lines

    @staticmethod
    def _node_position(part_name: str, node_name: str) -> str:
        height = part(part_name).height_m
        if node_name == "top":
            y = height / 2.0
        elif node_name == "bottom":
            y = -height / 2.0
        else:
            y = 0.0
        return f"0|{y:.4f}|0"

    @staticmethod
    def _resources(p) -> list[str]:
        resources: list[str] = []
        for name, amount in [("LiquidFuel", p.liquid_fuel), ("Oxidizer", p.oxidizer), ("SolidFuel", p.solid_fuel)]:
            if amount <= 0:
                continue
            resources.extend(
                [
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
            )
        return resources
