from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import astro
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
        # RelayAntenna100 (RA-100) MUST be harvested or the bus_layout silently falls back to the weak
        # longAntenna (Communotron 16) — which is exactly why the relays could not hold the Kerbin<->Duna
        # link across conjunction. Harvest it so a relay craft actually carries the 100 Gm relay dish.
        names.update({"longAntenna", "RelayAntenna100", "solarPanels5", "batteryBankMini", "basicFin", "asasmodule1-2"})
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
        # The .craft `description` MUST be a single line; design.notes (the multi-line design log)
        # would otherwise spill subsequent lines into the parser as junk keys.
        desc = " | ".join(s.strip() for s in design.notes.replace("\r", "").split("\n") if s.strip())
        lines: list[str] = [
            f"ship = {design.name}",
            "version = 1.12.5",
            f"description = Generated by ksp1-automation-lab. {desc}",
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

        # CALCULATED parachute count: design.py sizes it for the target body's LIVE atmospheric density
        # (estimates["parachutes"]); a propulsive Starship-style lander gets 0. Designs without the
        # field default to 1 (Kerbin recovery), preserving older craft.
        n_chute = int(round(design.estimates.get("parachutes", 1)))
        chute_r = part(root.part_name).height_m * 0.55 + 0.15
        for i in range(n_chute):
            chute = new_node("parachuteSingle", 0)
            if i == 0 and not design.docking_port:
                # First chute on the nose unless the docking port reserves it.
                self._attach(root, chute, "top", "bottom", up=True)
            else:
                # Remaining chutes distributed radially around the command pod.
                ang = 2.0 * math.pi * i / max(1, n_chute)
                self._attach_surface(root, chute, (chute_r * math.cos(ang), root.y, chute_r * math.sin(ang)))
            nodes.append(chute)

        current = root
        if design.crewed or design.heatshield:
            heat = new_node("HeatShield1", 0)
            self._attach(current, heat, "bottom", "top")
            nodes.append(heat)
            current = heat
            # SEPARABLE CREW CAPSULE: a decoupler below the heatshield lets the return driver jettison
            # the probe/cabin/service section before reentry, so the reentry vehicle is the short,
            # stable pod+heatshield+parachute capsule whose chute lands the POD crew (proven live:
            # Boke Kerman returned alive). NOTE: extra crew in the crewCabin below this decoupler are
            # NOT recovered — landing the WHOLE crew needs a single 3-seat pod (mk1-3) as the capsule
            # instead of pod+cabin (putting the cabin ABOVE the decoupler raised the CoM and the
            # ascent stalled at ~39 km). Inverse-stage 0 so it never auto-fires during ascent/staging.
            if design.crewed:
                capsule_decoupler = new_node("Decoupler.1", 0)
                self._attach(current, capsule_decoupler, "bottom", "top")
                nodes.append(capsule_decoupler)
                current = capsule_decoupler
        if design.crewed:
            # Inline probe core: a guaranteed control source for the headless launch.
            probe = new_node("probeCoreOcto.v2", 0)
            self._attach(current, probe, "bottom", "top")
            nodes.append(probe)
            current = probe
            # Crew cabin gives extra seats for transfers (kept BELOW the decoupler so the launch CoM
            # stays low and stable — see the capsule note above for the all-crew-recovery caveat).
            if part_bodies is None or "crewCabin" in part_bodies:
                cabin = new_node("crewCabin", 0)
                self._attach(current, cabin, "bottom", "top")
                nodes.append(cabin)
                current = cabin

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
        lander_engine: CraftNode | None = None  # engine of the lander stage (footpads must clear its bell)
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
            if render_index == 1:
                lander_engine = engine  # bottom of the kept stack — legs' footpads go below this bell
            # CALCULATED engine cluster: design.py sizes engine_count for the phase's TWR. The central
            # engine is node-attached on the stack axis; the rest clip into a ring around it on the
            # bottom tank, all pointing down (identity rotation) and crossfeeding from the tank — the
            # Starship / Super-Heavy way to make thrust without a single impossibly-large engine.
            n_extra = max(0, stage.engine_count - 1)
            if n_extra > 0:
                ring_r = 0.5 + 0.12 * n_extra
                for k in range(n_extra):
                    ang = 2.0 * math.pi * k / n_extra
                    sat = new_node(stage.engine, render_index)
                    self._attach_surface(current, sat, (ring_r * math.cos(ang), engine.y, ring_r * math.sin(ang)))
                    nodes.append(sat)
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
            # RA-100 relay (not the weak Communotron 16): keeps signal to Kerbin from Duna and lets the
            # craft relay — the fix for no-signal-at-Duna. Falls back to longAntenna if RA-100 isn't
            # in the part library.
            ("RelayAntenna100" if (part_bodies is None or "RelayAntenna100" in part_bodies) else "longAntenna",
             (bus_radius, bus_y, 0.0), (0.0, 0.0, 0.0, 1.0)),
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

        # Aerodynamic stabilisers — CALCULATED static margin. A rocket is stable on ascent only if its
        # centre of pressure (CoP, the lateral-area centroid) sits BELOW its centre of mass (CoM, the
        # mass centroid) along the flight axis. We compute both from the assembled stack and add just
        # enough passive base fins to push the CoP at least one body-diameter below the CoM. This is the
        # missing factor that let a marginal-TWR stack tumble; the proven Orion launched at the same TWR
        # because it happened to be stable. Passive fins (not active AV-R8) — a weak probe autopilot
        # over-rotates large control surfaces and tumbles the stack.
        if bottom_tank is not None and can_emit("basicFin"):
            fin_y = bottom_tank.y - part(bottom_tank.part_name).height_m * 0.25
            # CoM (mass-weighted) and body CoP (lateral-area-weighted) over the stacked structural parts.
            m_sum = m_mom = a_sum = a_mom = 0.0
            max_d = 1.25
            for n in nodes:
                if n.is_surface:
                    continue  # radial accessories do not define the stack axis
                pp = part(n.part_name)
                m_sum += pp.wet_mass_t
                m_mom += pp.wet_mass_t * n.y
                lat = pp.height_m * pp.diameter_m
                a_sum += lat
                a_mom += lat * n.y
                max_d = max(max_d, pp.diameter_m)
            com_y = m_mom / m_sum if m_sum else fin_y
            margin = max_d  # require ~1 calibre of static margin (CoP this far below CoM)
            fin_name, fin_count = "basicFin", 0
            for cand in ("basicFin", "R8winglet"):
                fa = part(cand).fin_area_m2
                for nfin in range(4, 25):
                    cop_y = (a_mom + nfin * fa * fin_y) / (a_sum + nfin * fa) if (a_sum + nfin * fa) else fin_y
                    if com_y - cop_y >= margin:
                        fin_name, fin_count = cand, nfin
                        break
                if fin_count:
                    break
            fin_count = max(4, fin_count)
            cop_final = (a_mom + fin_count * part(fin_name).fin_area_m2 * fin_y) / (a_sum + fin_count * part(fin_name).fin_area_m2)
            static_margin = com_y - cop_final
            self.last_stability = {"com_y": round(com_y, 2), "cop_y": round(cop_final, 2),
                                   "static_margin_m": round(static_margin, 2), "fins": f"{fin_count}x {fin_name}"}
            # Persist onto the design so the result is gateable/serialisable, not a transient attribute.
            # ascent_stable iff CoP sits at least one calibre below CoM (else the fin cap was hit and the
            # stack launches understabilised — the caller should treat that as an invalid design).
            design.static_margin_m = round(static_margin, 2)
            design.ascent_stable = static_margin >= margin - 1e-6
            if design.stages:
                design.stages[0].fin_count = fin_count  # fins sit on the first-firing (booster) stage
            fin_r = part(bottom_tank.part_name).diameter_m * 0.5 + 0.4
            for k in range(fin_count):
                th = 2.0 * math.pi * k / fin_count
                rot = (0.0, math.sin(th / 2.0), 0.0, math.cos(th / 2.0))  # yaw the fin to face outward
                fin = new_node(fin_name, bottom_tank.stage_index)
                self._attach_surface(bottom_tank, fin, (fin_r * math.cos(th), fin_y, fin_r * math.sin(th)), rot)
                nodes.append(fin)

        # Landing legs — CALCULATED for landed TIP-OVER stability (the fix for the crew that tipped over
        # and was lost). The industry rule: footpad SPAN >= center-of-gravity HEIGHT, i.e. the tip-over
        # angle theta = atan(half_span / CoG_height) must be large (target >= 35-45 deg). A propulsive
        # (no-chute) lander needs legs just as much as a chute lander — `landing_legs` now covers it.
        # We compute the LANDED CoG height of the parts that STAY (bus + lander stage, inverse-stage 0/1,
        # after the booster + transfer drop) above the footpad plane, then splay the legs wide enough
        # that span = 1.4 x CoG height (theta ~ 35 deg) — so it cannot topple on touchdown.
        if design.landing_legs and lander_tank is not None and can_emit("landingLeg1"):
            eng_h = part(lander_engine.part_name).height_m if lander_engine is not None else 1.0
            anchor = lander_engine if lander_engine is not None else lander_tank
            foot_y = anchor.y - eng_h * 0.5 - 0.2            # footpad plane just below the engine bell
            kept = [n for n in nodes if not n.is_surface and getattr(n, "stage_index", 0) in (0, 1)]
            mk = sum(part(n.part_name).wet_mass_t for n in kept) or 1.0
            cog_y = sum(part(n.part_name).wet_mass_t * n.y for n in kept) / mk
            h_cog = max(0.5, cog_y - foot_y)                 # CoG height above the footpad plane
            hull_r = part(lander_tank.part_name).diameter_m * 0.5
            leg_radius = max(hull_r + 0.4, 0.85 * h_cog)     # span ~1.7 x CoG height -> ~40 deg tip-over
            leg_count = 6 if (mk > 12.0 or h_cog > 6.0) else 4   # hexagon base for tall/heavy landers
            theta = math.degrees(math.atan2(leg_radius, h_cog))
            design.cog_height_m = round(h_cog, 2)
            design.leg_span_m = round(2.0 * leg_radius, 2)
            design.tipover_angle_deg = round(theta, 1)
            design.landed_stable = theta >= 35.0
            for k in range(leg_count):
                th = 2.0 * math.pi * k / leg_count
                rot = (0.0, math.sin(th / 2.0), 0.0, math.cos(th / 2.0))  # yaw the footpad outward
                leg = new_node("landingLeg1", 0)
                self._attach_surface(lander_tank, leg, (leg_radius * math.cos(th), foot_y, leg_radius * math.sin(th)), rot)
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

        # AERODYNAMIC sign-off — turn the assembled SHAPE into air-resistance numbers (the aerospace
        # refinement): a streamlined nose (cone or docking port) + a faired payload (service bay) give a
        # low Cd; the widest tank sets the frontal area; the launch wet mass sets the ballistic
        # coefficient and the ascent drag-loss Δv; max-Q is what the airframe/fairing must survive.
        has_nose = any(n.part_name in ("noseCone", "dockingPort2") for n in nodes)
        faired = any(n.part_name == "ServiceBay.125.v2" for n in nodes)
        fin_n = sum(1 for n in nodes if n.part_name in ("basicFin", "R8winglet"))
        max_dia = max((part(n.part_name).diameter_m for n in nodes if not n.is_surface), default=1.25)
        cd = astro.drag_coefficient(has_nose, faired, fin_n)
        fa = astro.frontal_area(max_dia)
        wet_t = float(design.estimates.get("wet_mass_t", 0.0)) or sum(
            part(n.part_name).wet_mass_t for n in nodes if not n.is_surface)
        design.drag_cd = round(cd, 3)
        design.frontal_area_m2 = round(fa, 2)
        design.ballistic_coeff_kgm2 = round(astro.ballistic_coefficient(wet_t, cd, fa), 0)
        # Ascent drag loss + max-Q at the LAUNCH body (Kerbin sea level: rho 1.225, atmosphere 70 km).
        design.ascent_drag_loss_mps = round(astro.ascent_drag_loss(wet_t, cd, fa, 1.225, 70_000.0), 0)
        design.max_q_kpa = round(astro.max_dynamic_pressure(1.225) / 1000.0, 1)

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
