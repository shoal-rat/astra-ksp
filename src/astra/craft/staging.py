"""Staging: which parts each spacebar press activates, and which parts each one drops.

KSP numbers stages inversely: the launch stage has the highest number, stage 0 fires last. A part's
``istg`` is the stage that activates it; its decouple stage (``dstg`` here) is the stage whose
decoupler separates it from the root part's vessel (-1 = never).

``"auto"`` staging reads the tree. Decouplers cut it into *sections*; a section's child sections
hang below it on a stack decoupler (the next lower stage) or beside it on a radial decoupler
(boosters, drop tanks, radial payloads). Events, in flight order:

1. launch: engines of the bottom section of the main stack, plus every booster attached to it,
   plus launch clamps;
2. booster separation, one event per booster group (shortest burn first);
3. each stack separation together with the ignition of the engines it exposes;
4. payload fairings, then engine-less separations (payloads, heat shields), each on its own stage;
5. parachutes last.

Explicit per-part ``stage`` values override the automatic number for that part; the automatic
sequence is then numbered without those parts. The stage table is always returned, with a warning
wherever different separators share a stage, so the AI can review and correct the sequence.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from astra.craft.geometry import Placed, Placement

PROPELLANTS = ("LiquidFuel", "Oxidizer", "SolidFuel", "MonoPropellant", "XenonGas")


@dataclass
class Event:
    kind: str
    parts: list[Placed]
    note: str = ""


@dataclass
class Section:
    head: Placed | None  # first part of the separated subtree (None for the root section)
    decoupler: Placed | None  # the part whose firing separates this section
    parts: list[Placed] = field(default_factory=list)
    children: list[Section] = field(default_factory=list)
    parent: Section | None = None

    @property
    def radial(self) -> bool:
        d = self.decoupler
        return d is not None and (bool(d.info.decoupler and d.info.decoupler.get("radial")) or d.attach == "surface")

    def engines(self) -> list[Placed]:
        return [p for p in self.parts if p.info.engines]

    def deep_parts(self) -> list[Placed]:
        out = list(self.parts)
        for c in self.children:
            out += c.deep_parts()
        return out

    def has_engines_deep(self) -> bool:
        return any(p.info.engines for p in self.deep_parts())


def separated_by(dec: Placed, placement: Placement) -> list[Placed]:
    """Parts that leave the root's vessel when ``dec`` fires (a subtree; may be empty)."""
    d = dec.info.decoupler or {}
    if d.get("is_omni"):
        return placement.subtree(dec)
    explosive = d.get("explosive_node", "top")
    if explosive == "srf":
        return placement.subtree(dec) if dec.attach == "surface" else []
    if dec.attach == "stack" and dec.child_node == explosive:
        return placement.subtree(dec)
    child = next((c for c in dec.children if c.attach == "stack" and c.parent_node == explosive), None)
    return placement.subtree(child) if child is not None else []


def build_sections(placement: Placement) -> Section:
    heads: dict[int, tuple[Placed, Placed]] = {}  # id(head) -> (head, decoupler)
    for p in placement.parts:
        if p.info.decoupler:
            sub = separated_by(p, placement)
            if sub:
                heads[id(sub[0])] = (sub[0], p)
    root = Section(None, None)
    sections: dict[int, Section] = {}

    def owner(p: Placed) -> Section:
        cur: Placed | None = p
        while cur is not None:
            if id(cur) in heads:
                return sections[id(cur)]
            cur = cur.parent
        return root

    for p in placement.parts:  # parents precede children in placement order
        if id(p) in heads:
            head, dec = heads[id(p)]
            sec = Section(head, dec)
            sec.parent = owner(p.parent) if p.parent is not None else root
            sec.parent.children.append(sec)
            sections[id(p)] = sec
        owner(p).parts.append(p)
    return root


def _burn_time_s(sec: Section, placement: Placement) -> float:
    flow = sum(e.mass_flow_tps for p in sec.deep_parts() for e in p.info.engines[:1])
    prop = sum(amount * p.info.resources[name]["density_t"]
               for p in sec.deep_parts() for name, (amount, _) in p.resource_amounts().items()
               if name in PROPELLANTS)
    return prop / flow if flow > 0 else float("inf")


def _plan(sec: Section, placement: Placement) -> list[Event]:
    """Events from launch until ``sec`` has burned and shed everything hanging on it."""
    stack_kids = [c for c in sec.children if not c.radial]
    main = next((c for c in stack_kids if c.has_engines_deep()), None)
    side = [c for c in sec.children if c is not main]
    boosters = [c for c in side if c.has_engines_deep() or _holds_propellant(c)]
    passive = [c for c in side if c not in boosters]

    ignite = list(sec.engines())
    for b in boosters:
        ignite += [p for p in b.deep_parts() if p.info.engines]
    if main is not None:
        events = _plan(main, placement)
        if main.decoupler.info.role == "heat_shield":
            # KSP ships heat-shield decouplers with staging disabled (they sit at stage -1), so their
            # release is a control_part action, never a stage; staging one would renumber the rest.
            if ignite:
                events.append(Event("stage", ignite, "light the next engines"))
        else:
            events.append(Event("stage", [main.decoupler] + ignite,
                                "separate the spent stage below" + (" and light the next engines" if ignite else "")))
    else:
        clamps = [p for p in placement.parts if p.info.role == "launch_clamp"]
        events = [Event("launch", ignite + clamps, "ignition at launch")]
    for group in _grouped(sorted(boosters, key=lambda b: _burn_time_s(b, placement))):
        for inner in _grouped([c for b in group for c in b.children]):  # boosters on boosters drop first
            events.append(Event("drop", [c.decoupler for c in inner], "separate inner boosters"))
        events.append(Event("drop", [b.decoupler for b in group], "separate spent boosters / drop tanks"))
    fairings = [p for p in sec.parts if p.info.role == "fairing"]
    if fairings:
        events.append(Event("fairing", fairings, "jettison the fairing"))
    for group in _grouped(passive):
        if all(b.decoupler.info.role == "heat_shield" for b in group):
            continue  # not staged in KSP (see above): released with control_part when wanted
        events.append(Event("separate", [b.decoupler for b in group], "release a payload/part"))
    return events


def _holds_propellant(sec: Section) -> bool:
    return any(amount > 0 and name in ("LiquidFuel", "Oxidizer", "SolidFuel")
               for p in sec.deep_parts() for name, (amount, _) in p.resource_amounts().items())


def _grouped(sections: list[Section]) -> list[list[Section]]:
    """Symmetry copies (same spec decoupler) separate together."""
    groups: dict[str, list[Section]] = {}
    for s in sections:
        groups.setdefault(s.decoupler.spec.id, []).append(s)
    return list(groups.values())


def assign_stages(placement: Placement) -> list[dict[str, Any]]:
    """Set ``istg``/``dstg`` on every placed part and return the stage table (launch first)."""
    spec = placement.spec
    for p in placement.parts:
        p.istg = -1
    if spec.stages == "auto":
        root = build_sections(placement)
        events = _plan(root, placement)
        chutes = [p for p in placement.parts if p.info.role == "parachute"]
        if chutes:
            events.append(Event("parachutes", chutes, "deploy parachutes"))
        events = [Event(e.kind, [p for p in e.parts if p is not None and p.spec.stage is None], e.note)
                  for e in events]
        events = [e for e in events if e.parts]
        claimed: set[int] = set()
        notes = {}
        n = len(events)
        for i, e in enumerate(events):
            for p in e.parts:
                if id(p) not in claimed and p.info.stageable:
                    p.istg = n - 1 - i
                    claimed.add(id(p))
                    notes[p.istg] = (e.kind, e.note)  # label only stages that hold auto-staged parts
    else:
        notes = {}
    for p in placement.parts:
        if p.spec.stage is not None and p.info.stageable:
            p.istg = p.spec.stage
    _decouple_stages(placement)
    return stage_table(placement, notes)


def _decouple_stages(placement: Placement) -> None:
    for p in placement.parts:
        p.dstg = -1
    for dec in placement.parts:
        if dec.info.decoupler and dec.istg >= 0:
            for q in separated_by(dec, placement):
                q.dstg = max(q.dstg, dec.istg)


def stage_table(placement: Placement, notes: dict[int, tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    notes = notes or {}
    stages = sorted({p.istg for p in placement.parts if p.istg >= 0} | {p.dstg for p in placement.parts if p.dstg >= 0},
                    reverse=True)
    rows = []
    for s in stages:
        act = [p for p in placement.parts if p.istg == s]
        drop = [p for p in placement.parts if p.dstg == s]
        row: dict[str, Any] = {"stage": s}
        if s in notes:
            row["event"], row["note"] = notes[s]
        row["activates"] = _names(act)
        if drop:
            row["drops"] = _names(drop)
        separators = {p.spec.id for p in act if p.info.decoupler or p.info.role == "fairing"}
        chutes = {p.spec.id for p in act if p.info.role == "parachute"}
        if len(separators) > 1:
            row["warning"] = f"{len(separators)} different separators fire together: {sorted(separators)}"
        elif separators and chutes:
            row["warning"] = f"parachutes {sorted(chutes)} deploy in the same stage as separator {sorted(separators)}"
        rows.append(row)
    return rows


def _names(parts: list[Placed]) -> list[str]:
    counts = Counter((p.spec.id, p.info.name) for p in parts)
    return [f"{pid} ({name})" + (f" x{c}" if c > 1 else "") for (pid, name), c in counts.items()]
