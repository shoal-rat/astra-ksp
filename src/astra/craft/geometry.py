"""Part placement: turn a craft spec into positioned, oriented parts, the way the VAB would.

Coordinates are KSP/Unity (+Y up the stack), quaternions are ``[x, y, z, w]`` exactly as written in
.craft files, and rotation math is the ordinary Hamilton product (Unity's left-handedness only
affects how the axes are drawn, not the algebra).

Stack attachment uses real node offsets: ``child = parent + R_p·parent_node - R_c·child_node`` with the
child turned so the two node directions face each other. KSP re-snaps stack parts to exactly this
on load. Surface attachment puts the child's srfAttach node on the parent's surface at
(azimuth, height) and yaws the child to face it. The stock convention, measured on every stock
craft: an attach node along the part's X or Y axis points *toward* the parent, one along Z points
*away* from it; for X-axis nodes this is ``yaw = 180° - azimuth``. KSP keeps surface parts where
the file puts them, so the surface radius matters: it comes from live bounds, the part's own
radial attach offset, its node sizes, or radial faces learned from stock craft.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from astra.craft.catalog import AttachNode, Catalog, PartInfo
from astra.craft.spec import CraftSpec, PartSpec, SpecError, check_spec, default_child_node

Vec = list[float]
Quat = list[float]
IDENTITY: Quat = [0.0, 0.0, 0.0, 1.0]
UP: Vec = [0.0, 1.0, 0.0]


# ---------------------------------------------------------------------------------------------
# Vector / quaternion helpers


def v_add(a: Vec, b: Vec) -> Vec:
    return [a[0] + b[0], a[1] + b[1], a[2] + b[2]]


def v_sub(a: Vec, b: Vec) -> Vec:
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def v_scale(a: Vec, k: float) -> Vec:
    return [a[0] * k, a[1] * k, a[2] * k]


def v_dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def v_cross(a: Vec, b: Vec) -> Vec:
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def v_len(a: Vec) -> float:
    return math.sqrt(v_dot(a, a))


def v_norm(a: Vec) -> Vec:
    n = v_len(a)
    return v_scale(a, 1.0 / n) if n > 1e-12 else [0.0, 0.0, 0.0]


def q_mul(a: Quat, b: Quat) -> Quat:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz]


def q_inv(q: Quat) -> Quat:
    n = sum(c * c for c in q) or 1.0
    return [-q[0] / n, -q[1] / n, -q[2] / n, q[3] / n]


def q_rot(q: Quat, v: Vec) -> Vec:
    x, y, z, w = q
    # v + 2w(q×v) + 2 q×(q×v), for a unit quaternion
    tx, ty, tz = 2 * (y * v[2] - z * v[1]), 2 * (z * v[0] - x * v[2]), 2 * (x * v[1] - y * v[0])
    return [v[0] + w * tx + y * tz - z * ty, v[1] + w * ty + z * tx - x * tz, v[2] + w * tz + x * ty - y * tx]


def q_axis_angle(axis: Vec, deg: float) -> Quat:
    ax = v_norm(axis)
    h = math.radians(deg) / 2
    s = math.sin(h)
    return [ax[0] * s, ax[1] * s, ax[2] * s, math.cos(h)]


def q_yaw(deg: float) -> Quat:
    """Rotation about +Y; maps +X to (cos θ, 0, -sin θ) (the stock surface-attach yaw)."""
    return q_axis_angle(UP, deg)


def q_from_to(a: Vec, b: Vec, fallback_axis: Vec = UP) -> Quat:
    """Shortest rotation taking direction ``a`` to ``b`` (``fallback_axis`` when they are opposite)."""
    a, b = v_norm(a), v_norm(b)
    d = v_dot(a, b)
    if d > 1 - 1e-9:
        return list(IDENTITY)
    if d < -1 + 1e-9:
        axis = fallback_axis if abs(v_dot(v_norm(fallback_axis), a)) < 0.99 else v_cross(a, [1.0, 0.0, 0.0])
        if v_len(axis) < 1e-6:
            axis = v_cross(a, [0.0, 0.0, 1.0])
        return q_axis_angle(axis, 180.0)
    c = v_cross(a, b)
    q = [c[0], c[1], c[2], 1 + d]
    n = math.sqrt(sum(x * x for x in q))
    return [x / n for x in q]


def q_angle_deg(a: Quat, b: Quat) -> float:
    """Angle of the rotation between two orientations."""
    d = abs(sum(x * y for x, y in zip(q_normalize(a), q_normalize(b))))
    return math.degrees(2 * math.acos(min(1.0, d)))


def q_normalize(q: Quat) -> Quat:
    n = math.sqrt(sum(c * c for c in q)) or 1.0
    q = [c / n for c in q]
    return [-c for c in q] if q[3] < 0 else q


def yaw_phase(v: Vec) -> float:
    """Heading of a horizontal direction in the sense of :func:`q_yaw` (+X = 0°, -Z = 90°)."""
    return math.degrees(math.atan2(-v[2], v[0]))


# ---------------------------------------------------------------------------------------------
# Part geometry


def local_extent_y(info: PartInfo) -> tuple[float, float]:
    """(bottom, top) of the part along its own +Y axis."""
    if info.bounds:
        c, s = info.bounds["center"][1], info.bounds["size"][1]
        if s > 0:
            return c - s / 2, c + s / 2
    ys = [n.pos[1] for n in info.nodes]
    if ys:
        return min(ys + [0.0]), max(ys + [0.0])
    r = min(info.radius_m, 0.3)  # small radial part without geometry data
    return -r, r


def surface_radius(info: PartInfo, height_m: float) -> tuple[float, str]:
    """Distance from the part's +Y axis to its skin at ``height_m`` (part-local), and the source."""
    if info.srf_node is not None:
        x, _, z = info.srf_node.pos
        if abs(info.srf_node.dir[1]) < 0.3 and math.hypot(x, z) > 0.05:
            return math.hypot(x, z), "radial attach offset"
    top, bottom = info.node("top"), info.node("bottom")
    if top is not None and bottom is not None and top.pos[1] > bottom.pos[1]:
        r_top, r_bot = _node_radius(info, top, "top"), _node_radius(info, bottom, "bottom")
        t = min(1.0, max(0.0, (height_m - bottom.pos[1]) / (top.pos[1] - bottom.pos[1])))
        return r_bot + t * (r_top - r_bot), "node sizes"
    if info.bounds and info.bounds["size"][0] > 0:
        s = info.bounds["size"]
        return max(s[0], s[2]) / 2, "bounds"
    return info.radius_m, "bulkhead"


def _node_radius(info: PartInfo, node: AttachNode, end: str) -> float:
    """Radius at a stack end: bulkhead profiles refine the node size (e.g. size1p5 = 1.875 m)."""
    diameters = info.diameters_m
    if len(diameters) == 1:
        return diameters[0] / 2
    if len(diameters) >= 2:
        other = info.node("bottom" if end == "top" else "top")
        if other is not None and other.size != node.size:
            return (diameters[-1] if node.size > other.size else diameters[0]) / 2
    return node.diameter_m / 2


def outer_extent(info: PartInfo, direction: Vec) -> tuple[float, str]:
    """How far the part reaches from its origin along a part-local unit ``direction``: the face
    KSP actually attached to in stock craft when known (render bounds overshoot trusses), else the
    part's bounding box, else its radius."""
    if info.outer_face_m is not None and info.srf_node is not None:
        if v_dot(direction, outward_dir(info.srf_node)) > 0.9:
            return info.outer_face_m, "learned from stock craft"
    if info.bounds and info.bounds["size"][0] > 0:
        c, s = info.bounds["center"], info.bounds["size"]
        return v_dot(c, direction) + sum(s[i] / 2 * abs(direction[i]) for i in range(3)), "bounds"
    return info.radius_m, "estimate"


def srf_target_sign(node: AttachNode) -> float:
    """-1: the node direction points toward the parent (X/Y-axis nodes); +1: away (Z-axis nodes)."""
    ax = max(range(3), key=lambda i: abs(node.dir[i]))
    return 1.0 if ax == 2 else -1.0


def outward_dir(node: AttachNode) -> Vec:
    """Part-local direction pointing away from the part it is surface-attached to."""
    return v_scale(v_norm(node.dir), srf_target_sign(node))


def surface_rotation(node: AttachNode, normal: Vec) -> Quat:
    """Part-local rotation that makes a surface part face a parent surface with outward ``normal``."""
    d = v_norm(node.dir)
    target = v_scale(normal, srf_target_sign(node))
    flat = [d[0], 0.0, d[2]]
    if v_len(flat) < 1e-6:
        flat = [1.0, 0.0, 0.0]
    tilt = q_from_to(d, v_norm(flat), fallback_axis=[0.0, 0.0, 1.0])
    horiz_target = [target[0], 0.0, target[2]]
    if v_len(horiz_target) < 1e-6:  # vertical normal (top face): align directly
        return q_mul(q_from_to(v_norm(flat), target, fallback_axis=[1.0, 0.0, 0.0]), tilt)
    yaw = yaw_phase(v_norm(horiz_target)) - yaw_phase(v_norm(flat))
    q = q_mul(q_yaw(yaw), tilt)
    if abs(target[1]) > 1e-6:  # a tilted normal (cone skin): pitch the result onto it
        q = q_mul(q_from_to(v_norm(horiz_target), target), q)
    return q


# ---------------------------------------------------------------------------------------------
# Placement


@dataclass
class Placed:
    key: str  # spec id, with '#k' suffixes for symmetry copies
    spec: PartSpec
    info: PartInfo
    parent: Placed | None
    attach: str  # root | stack | surface
    pos: Vec
    rot: Quat
    parent_node: str | None = None  # stack: the parent's node id
    child_node: str | None = None  # stack: this part's node id toward the parent
    azimuth_deg: float | None = None  # surface: in the parent's frame
    children: list[Placed] = field(default_factory=list)
    sym: list[Placed] = field(default_factory=list)  # symmetry counterparts (excluding self)
    uid: int = 0
    istg: int = -1  # inverse stage in which the part is activated (-1 = not staged)
    dstg: int = -1  # inverse stage in which the part is decoupled from the root (-1 = never)

    @property
    def craft_id(self) -> str:
        return f"{self.info.name}_{self.uid}"

    @property
    def rel_pos(self) -> Vec:
        if self.parent is None:
            return list(self.pos)
        return q_rot(q_inv(self.parent.rot), v_sub(self.pos, self.parent.pos))

    @property
    def rel_rot(self) -> Quat:
        if self.parent is None:
            return list(self.rot)
        return q_normalize(q_mul(q_inv(self.parent.rot), self.rot))

    def world(self, local: Vec) -> Vec:
        return v_add(self.pos, q_rot(self.rot, local))

    def node_world(self, node_id: str) -> Vec | None:
        n = self.info.node(node_id)
        return self.world(n.pos) if n else None

    @property
    def wet_mass_t(self) -> float:
        total = self.info.mass_t
        for name, r in self.info.resources.items():
            frac = self.spec.resources.get(name)
            amount = r["max"] * frac if frac is not None else r["amount"]
            total += amount * r["density_t"]
        return total

    def resource_amounts(self) -> dict[str, tuple[float, float]]:
        """{resource: (amount, max)} after the spec's fill fractions."""
        out = {}
        for name, r in self.info.resources.items():
            frac = self.spec.resources.get(name)
            out[name] = (r["max"] * frac if frac is not None else r["amount"], r["max"])
        return out


@dataclass
class Placement:
    spec: CraftSpec
    parts: list[Placed]
    warnings: list[str] = field(default_factory=list)

    @property
    def root(self) -> Placed:
        return self.parts[0]

    def by_key(self, key: str) -> Placed:
        return next(p for p in self.parts if p.key == key)

    def copies(self, spec_id: str) -> list[Placed]:
        return [p for p in self.parts if p.spec.id == spec_id]

    def subtree(self, part: Placed) -> list[Placed]:
        out, todo = [], [part]
        while todo:
            p = todo.pop()
            out.append(p)
            todo.extend(p.children)
        return out

    def extents(self) -> dict[str, float]:
        """Vertical span, max radial reach from the root axis, and bounding size."""
        lo, hi, reach = math.inf, -math.inf, 0.0
        xs, zs = [], []
        for p in self.parts:
            bottom, top = local_extent_y(p.info)
            for y in (bottom, top):
                w = p.world([0.0, y, 0.0])
                lo, hi = min(lo, w[1]), max(hi, w[1])
            r = max(p.info.radius_m, 0.05)
            dist = math.hypot(p.pos[0] - self.root.pos[0], p.pos[2] - self.root.pos[2])
            reach = max(reach, dist + r)
            xs += [p.pos[0] - r, p.pos[0] + r]
            zs += [p.pos[2] - r, p.pos[2] + r]
        return {"bottom_y": lo, "top_y": hi, "height_m": hi - lo, "max_diameter_m": 2 * reach,
                "size": [max(xs) - min(xs), hi - lo, max(zs) - min(zs)]}

    def com(self) -> Vec:
        total = 0.0
        acc = [0.0, 0.0, 0.0]
        for p in self.parts:
            m = p.wet_mass_t
            acc = v_add(acc, v_scale(p.world(p.info.com_offset), m))
            total += m
        return v_scale(acc, 1.0 / total) if total else list(self.root.pos)


def place(spec: CraftSpec, catalog: Catalog, *, uid_seed: int | None = None) -> Placement:
    """Position every part (symmetry copies included). Raises SpecError if the spec does not fit
    the catalog. The lowest point of the craft ends up 1 m above the origin, like a VAB build."""
    issues = check_spec(spec, catalog)
    if issues:
        raise SpecError(issues)
    placement = Placement(spec, [])
    root_spec = spec.root
    root = Placed(root_spec.id, root_spec, catalog.get(root_spec.part), None, "root", [0.0, 0.0, 0.0],
                  list(IDENTITY))
    placement.parts.append(root)
    _place_children(root, spec, catalog, placement)

    groups: dict[str, list[Placed]] = {}
    for p in placement.parts:
        groups.setdefault(p.spec.id, []).append(p)
    for members in groups.values():
        if len(members) > 1:
            for m in members:
                m.sym = [o for o in members if o is not m]

    lift = 1.0 - placement.extents()["bottom_y"]
    for p in placement.parts:
        p.pos[1] += lift
    _assign_uids(placement, uid_seed)
    return placement


def _place_children(parent: Placed, spec: CraftSpec, catalog: Catalog, placement: Placement) -> None:
    for cs in spec.children(parent.spec.id):
        info = catalog.get(cs.part)
        if cs.surface is None:
            child = _stack_attach(parent, cs, info)
            copies = [child]
        else:
            copies = [_surface_attach(parent, cs, info, k, placement) for k in range(cs.symmetry)]
        for i, child in enumerate(copies):
            if len(copies) > 1:
                child.key = f"{child.key}#{i + 1}"
            parent.children.append(child)
            placement.parts.append(child)
            _place_children(child, spec, catalog, placement)


def _suffix(parent: Placed) -> str:
    return parent.key[len(parent.spec.id):]  # symmetry path of the parent ('', '#2', '#2#1', ...)


def _stack_attach(parent: Placed, cs: PartSpec, info: PartInfo) -> Placed:
    pn = parent.info.node(cs.node)
    cn_id = cs.child_node or default_child_node(pn, info)
    cn = info.node(cn_id)
    local_rot = q_from_to(cn.dir, v_scale(pn.dir, -1.0), fallback_axis=[0.0, 0.0, 1.0])
    rot = q_normalize(q_mul(parent.rot, local_rot))
    pos = v_sub(parent.world(pn.pos), q_rot(rot, cn.pos))
    return Placed(cs.id + _suffix(parent), cs, info, parent, "stack", pos, rot,
                  parent_node=pn.id, child_node=cn.id)


def _surface_attach(parent: Placed, cs: PartSpec, info: PartInfo, k: int, placement: Placement) -> Placed:
    s = cs.surface
    step = 360.0 * k / cs.symmetry
    if s.azimuth_deg is None:  # outer face of a radially mounted parent
        outward = outward_dir(parent.info.srf_node)
        normal = q_rot(q_yaw(-step), outward) if step else outward  # same sense as azimuth steps
        az = math.degrees(math.atan2(normal[2], normal[0]))
        if s.radius_m is not None:
            r, src = s.radius_m, "spec"
        else:
            r, src = outer_extent(parent.info, outward)
        point = v_add(v_scale(normal, r), [0.0, s.height_m, 0.0])
    else:
        az = s.azimuth_deg + step
        a, t = math.radians(az), math.radians(s.tilt_deg)
        normal = [math.cos(t) * math.cos(a), math.sin(t), math.cos(t) * math.sin(a)]
        if s.radius_m is not None:
            r, src = s.radius_m, "spec"
        elif abs(s.tilt_deg) >= 45.0:
            r, src = 0.0, "face centre"  # top/bottom face: on the axis unless radius_m says otherwise
        else:
            r, src = surface_radius(parent.info, s.height_m)
        point = [r * math.cos(a), s.height_m, r * math.sin(a)]
    if src == "estimate":
        note = (f"{cs.id}: the outer face of {parent.spec.id} ({parent.info.name}) is not known; placed at "
                f"{r:.2f} m from its origin. Pass surface.radius_m if the parts should touch exactly.")
        if note not in placement.warnings:
            placement.warnings.append(note)
    local_rot = surface_rotation(info.srf_node, normal)
    local_pos = v_sub(point, q_rot(local_rot, info.srf_node.pos))
    rot = q_normalize(q_mul(parent.rot, local_rot))
    return Placed(cs.id + _suffix(parent), cs, info, parent, "surface", parent.world(local_pos), rot,
                  azimuth_deg=az % 360.0)


def _assign_uids(placement: Placement, seed: int | None) -> None:
    rng = random.Random(seed)
    used: set[int] = set()
    for p in placement.parts:
        uid = rng.randint(4_000_000_000, 4_294_967_000)
        while uid in used:
            uid = rng.randint(4_000_000_000, 4_294_967_000)
        used.add(uid)
        p.uid = uid

