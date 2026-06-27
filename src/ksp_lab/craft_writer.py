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
    # For a payload-fairing base: the computed ModuleProceduralFairing XSECTION shell (an ogive that
    # wraps the payload above it). _render_part splices this in place of the harvested craft's XSECTIONS.
    fairing_xsections: str | None = None
    # INTERSTAGE SHROUD: an upper-stage ENGINE that sits above a lower stage is left as a bare bell
    # mid-stack unless it is wrapped in an interstage shroud (the cylindrical tube between two stage
    # diameters, e.g. an Atlas/Delta interstage). When set on an engine node, this records the shroud's
    # outer radius + the y it extends UP to (the upper tank base), so the chart draws the tube and the
    # geometry gate can confirm the exposed engine is housed, not naked. (h_top, r)
    interstage_shroud: tuple[float, float] | None = None
    # The kept LEGGED LANDER engine: a bare bell + legs that fires only in vacuum. It is deliberately NOT
    # shrouded (an interstage tube under the bell fires the plume back into the same vessel and cancels
    # thrust), so the geometry gate exempts it from the interstage-shroud requirement.
    lander_base_engine: bool = False

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
        # Crewed -> Mk1 pod; uncrewed -> the 1.25 m RC-001S (probeStackSmall) root (see _build_nodes:
        # the 0.625 m OKTO would invert the taper gate). Crewed adds probeStackSmall as an INLINE guidance
        # unit too, so it is harvested in both modes.
        names.add("mk1pod.v2" if design.crewed else "probeStackSmall")
        if design.crewed:
            names.add("probeStackSmall")  # 1.25 m inline control source for the headless crewed launch
            names.add("kv2Pod")  # genuine 1.25 m 2-seat pod so astronauts can be transferred between modules
        if design.crewed or design.heatshield:
            names.add("HeatShield1")
        # Avionics/power/comms bus + aero fins added by _build_nodes; harvest their real
        # serializations too so they are not skipped (can_emit) or spliced module-less.
        # RelayAntenna100 (RA-100) MUST be harvested or the bus_layout silently falls back to the weak
        # longAntenna (Communotron 16) — which is exactly why the relays could not hold the Kerbin<->Duna
        # link across conjunction. Harvest it so a relay craft actually carries the 100 Gm relay dish.
        names.update({
            "longAntenna", "RelayAntenna100", "solarPanels5", "batteryBankMini", "batteryBank",
            "rtg", "basicFin", "R8winglet", "advSasModule", "adapterSize2-Size1", "Size3To2Adapter_v2",
        })
        # Payload fairing harvest. An uncrewed no-legs probe comsat rides in one; a CREWED REENTRY CAPSULE
        # (heat-shield + chutes, no docking port) ALSO rides in one (the blunt exposed pod+heat-shield was
        # the +0.12 Cd ascent penalty that made the crew vehicle draggy/unstable and burn all its Δv
        # suborbital); and a DOCKING / SERVICE PAYLOAD (docking port + RCS + bus hardware on top) rides in
        # one too so the Clamp-O-Tron + RCS + antenna/solar do not protrude during ascent. The fairing
        # shrouds the top through max-Q, then jettisons in orbit leaving everything intact (exposing the
        # docking port for rendezvous, the heat-shield+chutes for reentry). See has_fairing in
        # _build_nodes + the deploy fairing jettison. Mirror that predicate so harvest+emission stay in
        # lock-step.
        _crewed_reentry_capsule = bool(
            design.crewed and design.heatshield and not design.docking_port
            and int(round(design.estimates.get("parachutes", 0))) > 0
        )
        if ((not design.crewed and not design.landing_legs)
                or _crewed_reentry_capsule or design.docking_port):
            names.add("fairingSize1")
        # INTERSTAGE SHROUD (flaw #4): any multi-stage design wraps each exposed upper-stage engine in an
        # interstage tube at the LOWER stage's diameter — harvest the 2.5 m / 3.75 m procedural fairing
        # bases so the shroud emits with full module state (mirrors the payload-fairing harvest). Stages
        # are in fire order (stages[0] = bottom booster); stage i (i>=1) sits above stage i-1.
        for i in range(1, len(design.stages)):
            lower_dia = design.stages[i - 1].diameter_m
            names.add("fairingSize2" if lower_dia < 3.0 else "fairingSize3")
        if design.landing_legs:
            names.add("landingLeg1")
        if design.docking_port:
            names.update({"dockingPort2", "RCSBlock", "rcsTankRadialLong"})
        names.add("noseCone")
        for stage in design.stages:
            names.add(stage.engine)
            names.add(stage.tank)
        # Radial boosters: harvest the pod engine/tank + the radial decoupler so the strap-ons emit with
        # full module state (else can_emit drops them and the chart/craft show a bare core).
        rb = getattr(design, "radial_boosters", None)
        if rb is not None and rb.count > 0:
            names.update({rb.tank, rb.decoupler})
            if not getattr(rb, "is_drop_tank", False) and rb.engine:
                names.add(rb.engine)        # a powered pod also needs its engine; a drop tank has none
        # Canonicalize every harvest name to its LIVE loadable id (resolving legacy aliases like
        # RCSBlock -> RCSBlock.v2), so the harvested part-body library is keyed exactly like the nodes
        # new_node() builds — keeping harvest and emission in lock-step under the live names.
        return {part(n).name for n in names if n in STOCK_PARTS}

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
            # Canonicalize a legacy ALIAS to the live loadable part id (e.g. RCSBlock -> RCSBlock.v2) so the
            # emitted ``part = `` id is one the game can actually load. The catalog resolves the alias to the
            # live StockPart whose ``.name`` is that loadable id; the node carries it everywhere downstream.
            live_name = part(part_name).name
            node = CraftNode(part_name=live_name, uid=uid, stage_index=stage_index)
            uid -= 173
            return node

        # ROOT command part. Crewed -> the Mk1 pod (1.25 m). Uncrewed -> the RC-001S Remote Guidance Unit
        # (probeStackSmall), a genuine 1.25 m ModuleCommand probe core, NOT the Probodobodyne OKTO
        # (probeCoreOcto.v2). Now that the catalog carries the real cfg diameters, the OKTO is its true
        # 0.625 m, so rooting an uncrewed bus on it wedges a 0.625 m "neck" between the 1.25 m docking
        # port / service bays above and below — which inverts the monotonic-taper geometry gate (the tug
        # regression). probeStackSmall keeps the whole uncrewed bus a clean 1.25 m column at the SAME
        # 0.1 t dry mass, so the cfg-derived geometry reproduces a valid stack with no curated diameter.
        root = new_node("mk1pod.v2" if design.crewed else "probeStackSmall", 0)
        nodes = [root]

        # CALCULATED parachute count: design.py sizes it for the target body's LIVE atmospheric density
        # (estimates["parachutes"]); a propulsive Starship-style lander gets 0. Designs without the
        # field default to 1 (Kerbin recovery), preserving older craft.
        n_chute = int(round(design.estimates.get("parachutes", 1)))
        chute_r = part(root.part_name).height_m * 0.55 + 0.15
        # A CREWED capsule keeps the nose cone (the pod apex) as the very top part and mounts EVERY chute
        # radially around the pod — the real Mk16-radial capsule layout. Stacking the first chute on the
        # nose put a body-role part ABOVE the nose cone, so the top part was no longer a NOSE_PARTS member
        # and the geometry gate's "payload housed (fairing or capsule top)" check failed a sound crew
        # vehicle. An uncrewed probe keeps the original nose-stacked first chute (its comsat rides in a
        # fairing, so the gate passes on the fairing branch regardless).
        stack_first_chute = (not design.crewed) and (not design.docking_port)
        for i in range(n_chute):
            chute = new_node("parachuteSingle", 0)
            if i == 0 and stack_first_chute:
                # First chute on the nose unless the docking port reserves it (uncrewed probe only).
                self._attach(root, chute, "top", "bottom", up=True)
            else:
                # Chutes distributed radially around the command pod (all of them on a crewed capsule).
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
            # Inline probe core: a guaranteed control source for the headless launch. Use the 1.25 m
            # RC-001S Remote Guidance Unit (probeStackSmall) so the crew column stays a clean 1.25 m
            # cylinder. (The Probodobodyne OKTO probeCoreOcto.v2 is a genuine 0.625 m part — now that the
            # catalog carries the real cfg diameter, tucking a 0.625 m core into a 1.25 m stack inverts the
            # monotonic-taper gate; probeStackSmall is the correct 1.25 m inline guidance unit.)
            probe = new_node("probeStackSmall", 0)
            self._attach(current, probe, "bottom", "top")
            nodes.append(probe)
            current = probe
            # Extra crew seats for transfers (kept BELOW the decoupler so the launch CoM stays low and
            # stable — see the capsule note above for the all-crew-recovery caveat). Use the KV-2 'Onion'
            # reentry module (kv2Pod): a genuine 1.25 m 2-seat pod, so the crew section is a clean 1.25 m
            # column. (The Mk2 Command Pod "Mk2Pod" is really a 1.875 m Making-History part — now that the
            # catalog carries its true cfg diameter, stacking it on the 1.25 m bus inverts the taper gate;
            # the cfg-named "crewCabin" is the 2.5 m Hitchhiker, also wrong here.)
            if part_bodies is None or "kv2Pod" in part_bodies:
                cabin = new_node("kv2Pod", 0)
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
        # Use the 1.25 m Advanced Inline Stabilizer (advSasModule) — the correct command-bus-diameter
        # inline wheel. (The old hardcoded "asasmodule1-2" is, per its real cfg, the LARGE 2.5 m wheel;
        # stacking three of those in the 1.25 m bus put 2.5 m parts above the 1.25 m service bays and
        # broke the monotonic-taper gate once the catalog became authoritative. advSasModule is 1.25 m,
        # so the bus stays a clean 1.25 m column.)
        if part_bodies is None or "advSasModule" in part_bodies:
            for _ in range(3):
                reaction_wheel = new_node("advSasModule", 0)
                self._attach(current, reaction_wheel, "bottom", "top")
                nodes.append(reaction_wheel)
                current = reaction_wheel

        # Designs list stages in ignition order, so render them from upper stage down.
        rendered_stages = list(reversed(design.stages))
        bottom_tank: CraftNode | None = None
        lander_tank: CraftNode | None = None  # tank of the stage that lands (footpads reference its hull)
        lander_engine: CraftNode | None = None  # engine of the lander stage (footpads must clear its bell)
        prev_dia: float | None = None          # diameter of the stage ABOVE (for the adapter step check)
        # The lander = the stage firing DURING the landing burn (role contains "land"). By touchdown the
        # stages below it have all dropped, so its engine bell is the LOWEST kept part and the legs go
        # just under it — not on the top stage (the old render_index==1 bug floated legs mid-stack). For
        # a one-way craft with no explicit landing phase the top stage (render_index 1) is the lander.
        # render_index counts from the TOP (1 = last-firing); a landing phase at fire-order j renders at
        # len(stages) - j.
        n_stages = len(design.stages)
        _land_fire_idx = next((i for i, s in enumerate(design.stages) if "land" in s.role.lower()), None)
        lander_render_index = (n_stages - _land_fire_idx) if _land_fire_idx is not None else 1
        launch_stage_nodes: list[CraftNode] = []  # the first-firing stage's engine+tanks (re-staged for boosters)
        # PAYLOAD FAIRING: a probe comsat rides INSIDE an ogive shroud — never an exposed antenna on the
        # nose. The fairing base node-attaches BELOW the payload bus; its ModuleProceduralFairing shell
        # (computed XSECTIONS) wraps everything above it into a pointed nose and is jettisoned in orbit
        # before the dish + solar deploy. The base diameter matches the payload body.
        #
        # Three payloads ride faired:
        #  - an UNCREWED, no-legs probe comsat (the relay), and
        #  - a CREWED REENTRY CAPSULE: a crewed vehicle that returns by heat-shield + chutes (no docking
        #    port, no propulsive landing legs). The blunt EXPOSED Mk1 pod + forward heat-shield was the
        #    +0.12 Cd ascent penalty (faired False) that made the crew vehicle draggy and aerodynamically
        #    unstable — it tumbled the gravity turn and burned all ~10 km/s of Δv suborbital. Shrouding the
        #    capsule in the same ogive the relay uses brings Cd back to ~0.23. The fairing is jettisoned in
        #    orbit (deploy_relay/crewed_eve_roundtrip fairing jettison), which removes ONLY the shell and
        #    leaves the pod + heat-shield + chutes intact for the Kerbin-return reentry, and
        #  - a DOCKING / SERVICE PAYLOAD (Codex final flaw): any vehicle whose top bus carries a DOCKING
        #    PORT (+ the RCS quad + monoprop tank) — the crew ferry and the return tug. Without a fairing
        #    the Clamp-O-Tron + RCS blocks + radial antenna/solar/RTG STUCK OUT past the narrow payload
        #    body during ascent (the "top service hardware protrudes" flaw). They must ride inside the same
        #    ogive the relay uses; the fairing is jettisoned IN ORBIT (eve_two_ship_return jettison ->
        #    deploy_relay.jettison_payload_fairings), which removes ONLY the shell and EXPOSES the docking
        #    port (+ heat shield + chutes on the tug) for the in-orbit rendezvous/dock and reentry. A crewed
        #    Starship-style propulsive lander (docking_port + legs but mid-mission descent) is the natural
        #    candidate too — its top hardware is shrouded for launch and bared at the same in-orbit jettison.
        crewed_reentry_capsule = bool(
            design.crewed and design.heatshield and not design.docking_port
            and int(round(design.estimates.get("parachutes", 0))) > 0
        )
        # A docking / service payload: the top bus carries a docking port and (always, with it) the radial
        # RCS quad + monoprop tank + the avionics bus, so the ENTIRE top must be shrouded for ascent.
        docking_service_payload = bool(design.docking_port)
        has_fairing = (
            ((not design.crewed and not design.landing_legs)
             or crewed_reentry_capsule or docking_service_payload)
            and (part_bodies is None or "fairingSize1" in part_bodies))
        if has_fairing:
            payload_top = max(n.y + part(n.part_name).height_m / 2.0 for n in nodes if not n.is_surface)
            # The bus rides radially-mounted accessories on `root` at bus_y = root.y - 0.05; the tallest
            # (solar wing, h~1 m) reaches ABOVE the probe-core top. Fold that into payload_top so the shroud
            # is tall enough to enclose the solar wings (they were poking out the nose otherwise).
            bus_acc_top = root.y - 0.05 + self._bus_vertical_halfspan(part_bodies)
            payload_top = max(payload_top, bus_acc_top)
            # The DOCKING PORT is stack-attached ABOVE `root` (top node, up=True) further down in this
            # method, so it is not yet in `nodes` here. Fold its top into payload_top analytically (same
            # _attach geometry: root top + the port's own height) so the shroud rises ABOVE the Clamp-O-Tron
            # — otherwise the docking port poked out the nose (the "top service hardware protrudes" flaw).
            if design.docking_port:
                dock_top = root.y + part(root.part_name).height_m / 2.0 + part("dockingPort2").height_m
                payload_top = max(payload_top, dock_top)
            fb = new_node("fairingSize1", 0)
            self._attach(current, fb, "bottom", "top")
            # FAIRING ENCLOSURE (Codex flaw #3): the shroud must CONTAIN every payload appendage, not just
            # the stack column. The bus rides radially-mounted hardware — the RA-100 dish, solar wings,
            # battery, RTG, and (when docking) the RCS quad + monoprop tank — that reach OUTSIDE the bus
            # core radius. Compute that radial extent from the SAME geometry the accessory placement uses
            # below (_bus_radial_extent / _rcs_radial_extent), and size the fairing base radius to swallow
            # it, so nothing pokes through the shroud. The old base_r = bus-part-radius left the antenna +
            # solar sticking out past the shell (the protruding hardware Codex saw).
            stack_payload_r = max((part(n.part_name).diameter_m / 2.0
                                   for n in nodes if not n.is_surface and n.y > fb.y), default=0.0)
            appendage_r = self._bus_radial_extent(root, part_bodies)
            if design.docking_port:
                appendage_r = max(appendage_r, self._rcs_radial_extent(root, part_bodies))
            base_r = max(part(current.part_name).diameter_m / 2.0, stack_payload_r, appendage_r) + 0.10
            shell_h = max(1.0, payload_top - (fb.y + part("fairingSize1").height_m / 2.0))
            tip = shell_h + base_r * 2.6                     # ogive nose extends above the payload
            xs = [(0.0, base_r), (shell_h * 0.6, base_r), (shell_h, base_r * 0.8), (tip, 0.2)]
            # Two-tab indentation matches the donor craft's XSECTIONS (the shell sits inside the
            # MODULE block of the spliced part body); the override regex below replaces them in place.
            fb.fairing_xsections = "\n".join(
                f"\t\tXSECTION\n\t\t{{\n\t\t\th = {h:.4f}\n\t\t\tr = {r:.4f}\n\t\t}}" for h, r in xs)
            nodes.append(fb)
            current = fb
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
            # ADAPTER at a diameter STEP: this (lower) stage is WIDER than the one above it (the common
            # 2.5 m booster under a 1.25 m upper) -> insert the conical Rockomax adapter (1.25 m top mating
            # the decoupler, 2.5 m base mating the wide tank) so there is no exposed flat shoulder.
            if (prev_dia is not None and abs(stage.diameter_m - 2.5) < 0.1 and abs(prev_dia - 1.25) < 0.1
                    and (part_bodies is None or "adapterSize2-Size1" in part_bodies)):
                adapter = new_node("adapterSize2-Size1", render_index)
                self._attach(current, adapter, "bottom", "top")
                nodes.append(adapter)
                current = adapter
            if (prev_dia is not None and abs(stage.diameter_m - 3.75) < 0.1 and abs(prev_dia - 1.25) < 0.1
                    and (part_bodies is None or (
                        "adapterSize2-Size1" in part_bodies and "Size3To2Adapter.v2" in part_bodies
                    ))):
                # Smooth the full 1.25 -> 3.75 m step with two real conical adapters instead of leaving
                # a flat aerodynamic shoulder under the upper stack.
                adapter_125_250 = new_node("adapterSize2-Size1", render_index)
                self._attach(current, adapter_125_250, "bottom", "top")
                nodes.append(adapter_125_250)
                current = adapter_125_250
                adapter_250_375 = new_node("Size3To2Adapter_v2", render_index)
                self._attach(current, adapter_250_375, "bottom", "top")
                nodes.append(adapter_250_375)
                current = adapter_250_375
            if (prev_dia is not None and abs(stage.diameter_m - 3.75) < 0.1 and abs(prev_dia - 2.5) < 0.1
                    and (part_bodies is None or "Size3To2Adapter.v2" in part_bodies)):
                adapter = new_node("Size3To2Adapter_v2", render_index)
                self._attach(current, adapter, "bottom", "top")
                nodes.append(adapter)
                current = adapter
            # Defensive cap: a feasible stage is fineness-bounded to a handful of tanks; an INFEASIBLE
            # stage (rejected by the gate) can carry the 9999 sentinel, which would build 9999 nodes and
            # stall the renderer. Cap the drawn stack so an infeasible design still charts (visibly absurd)
            # instead of hanging — the feasibility gate is what actually blocks the launch.
            for _ in range(min(stage.tank_count, 120)):
                tank = new_node(stage.tank, render_index)
                self._attach(current, tank, "bottom", "top")
                nodes.append(tank)
                current = tank
                bottom_tank = tank  # last tank built = bottom of the first-ignition (launch) stage
                if render_index == lander_render_index:
                    lander_tank = tank  # the stage that lands = where the footpads reference the hull
                if render_index == n_stages:
                    launch_stage_nodes.append(tank)
            engine = new_node(stage.engine, render_index)
            self._attach(current, engine, "bottom", "top")
            nodes.append(engine)
            if render_index == n_stages:
                launch_stage_nodes.append(engine)
            if render_index == lander_render_index:
                lander_engine = engine  # bottom of the kept lander stack — legs' footpads go below this bell
            # CALCULATED engine cluster: design.py sizes engine_count for the phase's TWR AND guarantees
            # (via max_cluster_in_tank) that the cluster physically fits under the tank. The central
            # engine is node-attached on the stack axis; the rest sit on a ring of radius
            # r_tank - r_engine so every bell stays INSIDE the tank footprint (never the old fixed
            # 0.5 + 0.12*n that hung engines off the side). All point down and crossfeed from the tank.
            n_extra = max(0, stage.engine_count - 1)
            if n_extra > 0:
                from .design import cluster_ring_radius, engine_bell_radius
                ring_r = cluster_ring_radius(n_extra, engine_bell_radius(stage.engine))
                for k in range(n_extra):
                    ang = 2.0 * math.pi * k / n_extra
                    sat = new_node(stage.engine, render_index)
                    self._attach_surface(current, sat, (ring_r * math.cos(ang), engine.y, ring_r * math.sin(ang)))
                    nodes.append(sat)
                    if render_index == n_stages:
                        launch_stage_nodes.append(sat)
            # INTERSTAGE SHROUD (Codex flaw #4): an upper-stage engine that sits ABOVE a lower stage is a
            # bare bell mid-stack unless it is wrapped in an interstage tube (Atlas/Delta/Saturn interstage).
            # Any stage with render_index < n_stages has a lower stage beneath it, so shroud its engine: a
            # cylinder at the LOWER stage's diameter from the engine's bottom up to the engine top (the base
            # of this stage's own tank). The lower stage is the NEXT one in the top-down render order, so its
            # diameter is rendered_stages[render_index].diameter_m. The lower stage decouples before this
            # engine fires (the inter-stage TD-12 below), so the shroud drops WITH the spent lower stage.
            #
            # EXCEPTION — the KEPT LEGGED LANDER engine is NEVER shrouded. The shroud is surface-attached
            # under the bell; a long lander engine (e.g. the high-Isp LV-N the vacuum sizer favours) fires
            # its exhaust plume straight into that tube — which is part of the SAME vessel — so the reaction
            # cancels the thrust to ZERO. Live proof: the crewed-Mun LV-N made NO net thrust at TMI (full
            # throttle, fuel draining, g_force 0) until this shroud was removed. A bare lander bell is correct
            # (it touches down on its legs). The design-chart gate exempts it via ``lander_base_engine``.
            # Comsat upper engines (no legs, short bells) still get the shroud, which works for them.
            is_legged_lander_engine = bool(getattr(design, "landing_legs", False)) and render_index == lander_render_index
            if is_legged_lander_engine:
                engine.lander_base_engine = True
            if render_index < n_stages and not is_legged_lander_engine:
                lower_dia = rendered_stages[render_index].diameter_m
                shroud_r = max(part(stage.engine).diameter_m, lower_dia) / 2.0
                shroud_top = engine.y + part(stage.engine).height_m / 2.0  # up to the tank base above
                engine.interstage_shroud = (shroud_top, shroud_r)
                # Emit the shroud as a procedural fairing base SURFACE-attached to the engine (so the
                # load-bearing attN stack chain is untouched), with a computed cylinder-XSECTION shell that
                # wraps the bell up to the tank base. It rides the lower stage's inverse-stage so it drops
                # with that stage when it separates (the engine is then clear to fire). Gated on can_emit so
                # the offline/minimal render and the harvested-body launch stay in lock-step.
                shroud_part = "fairingSize2" if lower_dia < 3.0 else "fairingSize3"
                if part_bodies is None or shroud_part in part_bodies:
                    eng_bot = engine.y - part(stage.engine).height_m / 2.0
                    shroud = new_node(shroud_part, max(0, render_index))
                    self._attach_surface(engine, shroud, (0.0, eng_bot, 0.0))
                    sh_h = max(0.6, shroud_top - eng_bot)
                    xs = [(0.0, shroud_r), (sh_h, shroud_r)]  # straight interstage tube (no nose taper)
                    shroud.fairing_xsections = "\n".join(
                        f"\t\tXSECTION\n\t\t{{\n\t\t\th = {h:.4f}\n\t\t\tr = {r:.4f}\n\t\t}}" for h, r in xs)
                    nodes.append(shroud)
            current = engine
            prev_dia = stage.diameter_m  # the next (lower) stage compares its diameter to this one

        # RADIAL (strap-on) BOOSTERS — the asparagus / Soyuz / Falcon-Heavy ascent. design.py sized N
        # symmetric tank+engine pods (RadialBoosterSpec); render each as its OWN vertical stack clustered
        # around the launch CORE's bottom tank, hung on a RADIAL DECOUPLER so it jettisons when spent.
        #   * staging: the booster engines ignite WITH the core at T0 (same inverse-stage as the core
        #     engine, bumped to launch_istg = n_stages + 1 so the launch event sits above everything);
        #     the radial decouplers fire ONE stage later (launch_istg - 1) — boosters drop FIRST — and the
        #     core/upper inter-stage decoupler (already at n_stages - 1) fires after that. So the order is
        #     ignite(core+boosters) -> drop boosters -> separate core/upper, exactly the asparagus sequence.
        #   * geometry: each pod is offset (core_r + dec + pod_r) from the axis; the pod engine bell sits
        #     at the SAME plane as the core engine (boosters fire alongside the core), and the pod tanks
        #     stack up from there — a clean Soyuz silhouette, every bell at the base, nothing floating.
        rb = getattr(design, "radial_boosters", None)
        _rb_is_drop_tank = rb is not None and getattr(rb, "is_drop_tank", False)
        # A DROP-TANK pod has no engine, so the harvest only needs the tank + decoupler bodies; a powered
        # booster pod also needs its engine body. (Only meaningful when there ARE boosters — guard rb None.)
        _rb_bodies_ok = rb is not None and (part_bodies is None or (
            rb.tank in part_bodies and rb.decoupler in part_bodies
            and (_rb_is_drop_tank or rb.engine in part_bodies)))
        if rb is not None and rb.count > 0 and bottom_tank is not None and _rb_bodies_ok:
            core_tank = part(bottom_tank.part_name)
            core_r = core_tank.diameter_m / 2.0
            pod_tank = part(rb.tank)
            pod_eng = part(rb.engine) if not _rb_is_drop_tank else None
            pod_dec = part(rb.decoupler)
            pod_r = pod_tank.diameter_m / 2.0
            launch_istg = n_stages + 1                       # core + boosters ignite here (T0)
            radial_istg = max(0, launch_istg - 1)            # boosters decouple one stage later (drop first)
            # Re-stage the launch CORE engine + its tanks + the cluster satellites to launch_istg so the
            # core lights at the SAME T0 event as the strap-ons (otherwise the bumped boosters would fire a
            # stage before the core). The core's inter-stage decoupler stays at n_stages-1 (fires last).
            for n in launch_stage_nodes:
                n.stage_index = launch_istg
            # Pod engine bell plane = the core engine plane, so all bells fire at the base together. A
            # DROP-TANK pod has no engine, so its tank column starts at the core engine plane directly.
            core_engine_y = engine.y
            pod_eng_y = core_engine_y
            # Lateral offset: clear the core hull + the decoupler + the pod's own radius, plus a hair.
            off = core_r + pod_dec.diameter_m * 0.5 + pod_r + 0.15
            for b in range(rb.count):
                ang = 2.0 * math.pi * b / rb.count
                px, pz = off * math.cos(ang), off * math.sin(ang)
                # Build the pod bottom-up: a POWERED pod puts its engine at the bell plane first; a DROP
                # TANK has no engine and starts its tanks at the bell plane. Then stack tank_count tanks.
                if not _rb_is_drop_tank:
                    pod_eng_node = new_node(rb.engine, launch_istg)
                    self._attach_surface(bottom_tank, pod_eng_node, (px, pod_eng_y, pz))
                    nodes.append(pod_eng_node)
                    y_cursor = pod_eng_y + pod_eng.height_m / 2.0
                else:
                    y_cursor = pod_eng_y
                first_tank_node: CraftNode | None = None
                for t in range(min(rb.tank_count, 60)):
                    ty = y_cursor + pod_tank.height_m / 2.0
                    pod_tank_node = new_node(rb.tank, launch_istg)
                    self._attach_surface(bottom_tank, pod_tank_node, (px, ty, pz))
                    nodes.append(pod_tank_node)
                    if first_tank_node is None:
                        first_tank_node = pod_tank_node
                    y_cursor = ty + pod_tank.height_m / 2.0
                # The RADIAL DECOUPLER mates the pod to the core's bottom tank at the pod's mid-height; it
                # fires at radial_istg so the spent pod drops as one piece. Placed just inboard of the pod
                # tank, surface-attached to the core hull at the same (px,pz) direction.
                dec_y = (pod_eng_y + y_cursor) / 2.0
                dec_off = core_r + pod_dec.diameter_m * 0.5
                dx, dz = dec_off * math.cos(ang), dec_off * math.sin(ang)
                # Orient the decoupler to face outward (yaw to the pod's azimuth).
                rot = (0.0, math.sin(ang / 2.0), 0.0, math.cos(ang / 2.0))
                dec_node = new_node(rb.decoupler, radial_istg)
                self._attach_surface(bottom_tank, dec_node, (dx, dec_y, dz), rot)
                nodes.append(dec_node)

        def can_emit(part_name: str) -> bool:
            # Only attach a part when a real serialization is available (or in minimal mode),
            # so we never splice a module-less body that re-triggers the launch NullReference.
            # Resolve a legacy alias (RCSBlock) to its live id (RCSBlock.v2) so the lookup matches the
            # live-keyed harvest library; an unknown name is checked verbatim.
            live = part(part_name).name if part_name in STOCK_PARTS else part_name
            return part_bodies is None or live in part_bodies

        # Avionics / power / comms bus on the command part: a satellite needs a comm link, power
        # generation, and storage to function and stay controllable away from Kerbin. Mounted
        # radially on the command module so it adds negligible ascent drag.
        bus_radius = self._bus_mount_radius(root)
        bus_y = root.y - 0.05
        bus_layout = [
            # RA-100 relay (not the weak Communotron 16): keeps signal to Kerbin from Duna and lets the
            # craft relay — the fix for no-signal-at-Duna. Falls back to longAntenna if RA-100 isn't
            # in the part library.
            ("RelayAntenna100" if (part_bodies is None or "RelayAntenna100" in part_bodies) else "longAntenna",
             (bus_radius, bus_y, 0.0), (0.0, 0.0, 0.0, 1.0)),
            # Z-1k battery buffers the bursty reaction-wheel drain; falls back to the Z-200 if unharvested.
            ("batteryBank" if (part_bodies is None or "batteryBank" in part_bodies) else "batteryBankMini",
             (-bus_radius, bus_y, 0.0), (0.0, 0.0, 0.0, 1.0)),
            ("solarPanels5", (0.0, bus_y, bus_radius), (0.0, 0.0, 0.0, 1.0)),
            ("solarPanels5", (0.0, bus_y, -bus_radius), (0.0, 1.0, 0.0, 0.0)),
            # RTG: continuous sun-independent power so the probe stays controllable through eclipse (the
            # fix for the keo circularise burns dying on a flat battery in shadow). Mounted on a diagonal.
            ("rtg", (bus_radius * 0.7, bus_y - 0.18, bus_radius * 0.7), (0.0, 0.0, 0.0, 1.0)),
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
            rcs_r = self._rcs_mount_radius(root)
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
            # Target ~0.6 calibre of static margin from a SMALL fin set — a real rocket holds the rest with
            # engine GIMBAL + reaction wheels, not a forest of passive fins. Cap at 8 (a 3-4 fin set is
            # typical; >8 looks wrong and adds drag). Requiring a full calibre forced ~13 fins.
            # Aim for ~0.5 calibre of static margin with a SMALL fin set (3-8). A real rocket holds the
            # rest with engine GIMBAL + reaction wheels — not a forest of passive fins (a full calibre
            # forced ~13). If 8 basic fins can't reach the target, switch to the larger AV-R8 (2x area)
            # and use the full 8; the gimbal does the rest.
            margin = max_d * 0.5
            fin_name, fin_count = "", 0
            for cand in ("basicFin", "R8winglet"):
                fa = part(cand).fin_area_m2
                for nfin in range(3, 9):
                    cop_y = (a_mom + nfin * fa * fin_y) / (a_sum + nfin * fa) if (a_sum + nfin * fa) else fin_y
                    if com_y - cop_y >= margin:
                        fin_name, fin_count = cand, nfin
                        break
                if fin_count:
                    break
            if not fin_count:                       # target unreached even at 8 -> use 8 big fins + gimbal
                fin_name, fin_count = "R8winglet", 8
            fin_count = max(4, min(8, fin_count))
            cop_final = (a_mom + fin_count * part(fin_name).fin_area_m2 * fin_y) / (a_sum + fin_count * part(fin_name).fin_area_m2)
            static_margin = com_y - cop_final
            self.last_stability = {"com_y": round(com_y, 2), "cop_y": round(cop_final, 2),
                                   "static_margin_m": round(static_margin, 2), "fins": f"{fin_count}x {fin_name}"}
            # Persist onto the design. ascent_stable = the rocket is flyable on engine GIMBAL + reaction
            # wheels + fins. Real launchers are routinely 0.2-0.5 calibre aerodynamically UNSTABLE and fly
            # fine on thrust-vector control, so the flyable bound is a mild -0.30 calibre (a payload-on-top
            # heavy-lift stack on a wide base is never perfectly passively stable). The full-catalog sizer
            # legitimately picks wider, heavier-lift bases now (a Mammoth/3.75 m core under a light high-Isp
            # Nerv upper sits ~0.23 cal unstable), which TVC flies out; only a markedly top-heavy stack
            # (CoP well above CoG, worse than -0.30 cal) is rejected as genuinely uncontrollable. The old
            # 2.5 m Duna tower (~-1.7 cal) still fails.
            design.static_margin_m = round(static_margin, 2)
            design.ascent_stable = static_margin >= -0.30 * max_d
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
            kept = [n for n in nodes if not n.is_surface and getattr(n, "stage_index", 0) <= lander_render_index]
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
            # crew transfer). For ASCENT it is NOT the exposed nose — a docking-service payload is now
            # SHROUDED in the payload fairing built above (has_fairing is True when design.docking_port),
            # so the Clamp-O-Tron + RCS + bus hardware ride inside the ogive and nothing protrudes. The
            # fairing is jettisoned in orbit (deploy_relay.jettison_payload_fairings), exposing this port
            # for the in-orbit dock. No separate nose cone either way. inverse-stage 0.
            dock = new_node("dockingPort2", 0)
            self._attach(root, dock, "top", "bottom", up=True)
            nodes.append(dock)
        elif can_emit("noseCone") and not has_fairing:
            # Nose cone for streamlining (the aero requirement): above a STACK-mounted parachute if one
            # exists (uncrewed probe), else straight on the command pod's top node. SKIPPED when a payload
            # fairing is present — the fairing's ogive shell IS the nose (never a cone on top of a fairing).
            # On a crewed capsule the chutes are radial (stack_first_chute is False), so the nose cone caps
            # the pod directly — keeping a NOSE_PARTS member as the very top part (the "capsule top" gate).
            nose = new_node("noseCone", 0)
            top = chute if (n_chute > 0 and stack_first_chute) else root
            self._attach(top, nose, "top", "bottom", up=True)
            nodes.append(nose)

        # AERODYNAMIC sign-off — turn the assembled SHAPE into air-resistance numbers (the aerospace
        # refinement): a streamlined nose (cone or docking port) + a faired payload (service bay) give a
        # low Cd; the widest tank sets the frontal area; the launch wet mass sets the ballistic
        # coefficient and the ascent drag-loss Δv; max-Q is what the airframe/fairing must survive.
        has_nose = any(n.part_name in ("noseCone", "dockingPort2", "fairingSize1", "fairingSize2") for n in nodes)
        faired = any(n.part_name in ("ServiceBay.125.v2", "fairingSize1", "fairingSize2") for n in nodes)
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

    # --- bus accessory geometry (shared by the placement below AND the fairing-enclosure sizing) ------
    # The radially-mounted avionics/power/comms bus and the docking RCS quad sit at fixed radii on the
    # command pod. The fairing must enclose them, so both the placement and the fairing sizing read the
    # SAME formulas here — change one and the shroud follows, so the payload can never poke through again.
    @staticmethod
    def _bus_mount_radius(root: CraftNode) -> float:
        return part(root.part_name).height_m * 0.6 + 0.25

    @staticmethod
    def _rcs_mount_radius(root: CraftNode) -> float:
        return part(root.part_name).height_m * 0.5 + 0.18

    @classmethod
    def _bus_accessory_names(cls, part_bodies: dict[str, str] | None) -> list[str]:
        present = (lambda n: part_bodies is None or n in part_bodies)
        return [
            "RelayAntenna100" if present("RelayAntenna100") else "longAntenna",
            "batteryBank" if present("batteryBank") else "batteryBankMini",
            "solarPanels5", "rtg",
        ]

    @classmethod
    def _bus_radial_extent(cls, root: CraftNode, part_bodies: dict[str, str] | None) -> float:
        """Outermost radius the bus avionics/power/comms accessories reach (mount radius + part radius)."""
        r0 = cls._bus_mount_radius(root)
        return max((r0 + part(n).diameter_m / 2.0 for n in cls._bus_accessory_names(part_bodies)
                    if part_bodies is None or n in part_bodies), default=r0)

    @classmethod
    def _bus_vertical_halfspan(cls, part_bodies: dict[str, str] | None) -> float:
        """Half-height of the TALLEST bus accessory — how far above the mount plane the bus hardware
        reaches (the solar wing is the tallest). Used to make the payload fairing tall enough to enclose it."""
        return max((part(n).height_m / 2.0 for n in cls._bus_accessory_names(part_bodies)
                    if part_bodies is None or n in part_bodies), default=0.0)

    @classmethod
    def _rcs_radial_extent(cls, root: CraftNode, part_bodies: dict[str, str] | None) -> float:
        """Outermost radius the docking RCS quad + monoprop tank reach."""
        r0 = cls._rcs_mount_radius(root)
        ext = r0 + part("RCSBlock").diameter_m / 2.0 if (part_bodies is None or "RCSBlock.v2" in part_bodies) else r0
        if part_bodies is None or "rcsTankRadialLong" in part_bodies:
            ext = max(ext, r0 + part("rcsTankRadialLong").diameter_m / 2.0)
        return ext

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
        if body and node.fairing_xsections:
            # Replace the harvested fairing's XSECTION shell (sized for the donor craft) with the
            # computed ogive that wraps THIS payload, keeping it inside the ModuleProceduralFairing.
            # A donor XSECTION can contain nested ATTACHEDFLAG { ... } sub-blocks (e.g. the Ariane 5
            # interstage fairing harvested for the shroud), so the run of XSECTIONs is removed with a
            # BRACE-BALANCED scan — NOT a flat `[^}]*` regex, which stopped at the first nested `}` and
            # left orphaned ATTACHEDFLAG blocks + stray closing braces that closed the PART early and
            # null-ref'd KSPUtil.GetPartName on load (the legged-Mun launch failure).
            body = self._replace_xsections(body, node.fairing_xsections)
        if body and node.part_name == "Size3To2Adapter.v2":
            # The ADTP-2-3 is used here as an aerodynamic structural adapter, not as unplanned fuel
            # storage. Remove stock RESOURCE blocks so the live vessel mass matches the calculated dry
            # adapter mass in parts.py.
            body = re.sub(r"(?:\n\tRESOURCE\n\t\{(?:\n\t\t[^\n]*)+\n\t\})+", "", body)
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
    def _replace_xsections(body: str, new_xsections: str) -> str:
        """Replace the harvested fairing's contiguous run of ``XSECTION { ... }`` blocks with the
        computed ``new_xsections`` shell, using a BRACE-BALANCED scan so a donor XSECTION that nests
        ``ATTACHEDFLAG { ... }`` sub-blocks (the Ariane 5 interstage fairing) is consumed whole.

        The previous flat regex ``(?:\\n\\t\\tXSECTION\\n\\t\\t\\{[^}]*\\})+`` matched only up to the
        FIRST ``}`` — the closing brace of the first nested ATTACHEDFLAG, not the XSECTION's own brace —
        so it replaced one truncated XSECTION and left the rest of the donor serialization in place. The
        orphaned tail had unbalanced braces that closed the ModuleProceduralFairing MODULE and the PART
        early, leaving the remaining lines as junk PART fields: KSP then null-ref'd in
        ``KSPUtil.GetPartName`` while ``ShipConstruct.LoadShip`` parsed the .craft, so the craft would
        not load or launch. This scanner finds the start of the first ``XSECTION`` line, walks forward
        counting ``{``/``}`` to swallow every consecutive XSECTION block (with any nested children), and
        splices the computed shell in their place. Indentation (two tabs) matches the donor body so the
        shell sits correctly inside the MODULE block. If no XSECTION is found the body is returned
        unchanged."""
        lines = body.split("\n")
        start = None
        for idx, ln in enumerate(lines):
            if ln.strip() == "XSECTION":
                start = idx
                break
        if start is None:
            return body
        i = start
        end = start  # exclusive index just past the last consumed XSECTION block
        n = len(lines)
        while i < n and lines[i].strip() == "XSECTION":
            # The next non-blank line must open the block. Walk braces to its matching close.
            j = i + 1
            depth = 0
            opened = False
            while j < n:
                s = lines[j].strip()
                if s == "{":
                    depth += 1
                    opened = True
                elif s == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if not opened or j >= n:
                break  # malformed donor — stop here rather than over-consume
            end = j + 1
            i = end
        # Splice: keep everything before the first XSECTION and after the last consumed block, with the
        # computed shell (no leading newline — it is inserted as its own joined lines) in between.
        head = lines[:start]
        tail = lines[end:]
        replacement = new_xsections.split("\n")
        return "\n".join(head + replacement + tail)

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
