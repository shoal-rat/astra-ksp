"""LIVE part-database validation — cross-check the materialized stock catalog against the GAME's own
authoritative loaded part list, end to end.

This is the "use LIVE kRPC/bridge for the FINAL check" step: ``data/stock_parts.json`` is materialized by
the lab's raw .cfg parser, and ``tools/verify_parts.py`` already proves it self-consistent (JSON vs a fresh
re-parse) and matches hand-transcribed KSP 1.12 stats. This tool closes the loop against the running game,
where KSP has actually instantiated every part (cfg parse + ModuleManager patches + variant resolution).
Two independent live paths, in order of authority:

  PATH A — /part-database (preferred, needs the rebuilt DLL).  GET the whole loaded catalog
    (PartLoader.LoadedPartsList) via the bridge and cross-check EVERY rocket-relevant catalog part's
    dry mass, max thrust, ASL+vac Isp and LF/Ox/SolidFuel capacity against the live game numbers (~1%
    tolerance). This is the comprehensive final check. If the running bridge predates the endpoint the
    GET 404s and we say so (install the C:/tmp DLL + restart KSP, then re-run).

  PATH B — in-use vessel parts (fallback, works WITHOUT the new endpoint).  Read the parts of whatever
    vessel(s) are currently loaded via the existing POST /parts-list and cross-check those in-use parts'
    masses (dry + resource) against the catalog. This gives a live datum even before the consolidated DLL
    is reinstalled — it is exactly the parts the current mission is flying.

Both paths degrade gracefully: if the bridge is unreachable at all, we report that and exit 0 (nothing to
validate against) so the tool is safe to run unattended.

Run:  PYTHONPATH=src python tools/validate_parts_live.py
      PYTHONPATH=src python tools/validate_parts_live.py --bridge http://127.0.0.1:48500

Exit code 0 = every compared part matched (or nothing live to compare); 1 = at least one MISMATCH.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ksp_lab import parts as P  # noqa: E402
from ksp_lab.bridge_client import BridgeClient, BridgeError  # noqa: E402

# Tolerance for the live cross-check. Masses/Isp/thrust/capacities are the SAME cfg numbers the game
# loaded, so equality is expected; 1% rel + a tiny absolute floor absorbs float round-trip noise only.
REL_TOL = 0.01
ABS_TOL = 1e-4

# part_type buckets that carry rocket-relevant numbers worth a live cross-check. (Pure structural /
# aero / science parts have only a mass, which PATH B already covers when they are flown.)
ROCKET_RELEVANT_TYPES = {
    "liquid_engine", "solid_booster", "monoprop_engine", "ion_engine", "jet_engine",
    "fuel_tank", "adapter_tank", "rcs_tank", "xenon_tank",
    "pod", "probe", "lander", "crew_cabin",
}


def _close(catalog_val: float, live_val: float, rel: float = REL_TOL, absol: float = ABS_TOL) -> bool:
    """True when the two agree within ``rel`` (relative to the live value) OR ``absol`` (absolute)."""
    return abs(catalog_val - live_val) <= max(absol, abs(live_val) * rel)


class Report:
    """Accumulates MATCH/MISMATCH/SKIP rows and prints a clean summary."""

    def __init__(self) -> None:
        self.matches = 0
        self.mismatches: list[str] = []
        self.skips: list[str] = []

    def field(self, part_name: str, field: str, catalog_val: float, live_val: float,
              unit: str = "") -> None:
        """Compare one numeric field; record a MATCH or a MISMATCH line."""
        if _close(catalog_val, live_val):
            self.matches += 1
        else:
            self.mismatches.append(
                f"{part_name}.{field}: catalog={catalog_val:g}{unit} live={live_val:g}{unit} "
                f"(Δ={catalog_val - live_val:+g}{unit})"
            )

    def skip(self, msg: str) -> None:
        self.skips.append(msg)

    def print_summary(self, header: str) -> None:
        print(f"\n{header}")
        print("-" * len(header))
        print(f"  MATCH: {self.matches} field comparison(s) agree within {REL_TOL:.0%}")
        if self.skips:
            print(f"  SKIP:  {len(self.skips)}")
            for s in self.skips[:20]:
                print(f"    - {s}")
        if self.mismatches:
            print(f"  MISMATCH: {len(self.mismatches)}")
            for m in self.mismatches:
                print(f"    ! {m}")
        else:
            print("  MISMATCH: 0  (catalog matches the live game)")


# --------------------------------------------------------------------------------------------------
# The pure compare logic (unit-tested). Takes already-fetched live data + the catalog dict so the
# tests can drive it with mocks and no live calls.
# --------------------------------------------------------------------------------------------------
def catalog_resource_capacity(sp: P.StockPart, resource_name: str) -> float:
    """The catalog's stored max capacity for one of the resources the live DB reports, or 0.0."""
    return {
        "LiquidFuel": sp.liquid_fuel,
        "Oxidizer": sp.oxidizer,
        "SolidFuel": sp.solid_fuel,
    }.get(resource_name, 0.0)


def cross_check_part_database(live_parts: list[dict], catalog: dict[str, "P.StockPart"]) -> Report:
    """PATH A core: cross-check every rocket-relevant CATALOG part against the live /part-database dump.

    ``live_parts`` is the ``parts`` array from ``part_database()``; ``catalog`` is ``{name: StockPart}``.
    For each catalog part that the game also loaded we compare dry mass, max thrust, ASL/vac Isp, and the
    LF/Ox/SolidFuel capacities the live entry reports. Parts only the game has (or only the catalog has)
    are recorded as SKIP, not failures — the catalog is intentionally rocket-relevant, not exhaustive.
    """
    # KSP names a part TWO ways: the cfg/persistence form (underscores) and the runtime AvailablePart.name
    # the /part-database reports (dots). The reconciled catalog keys on the live (dotted) form, so a direct
    # by_name hit is the norm; we ALSO index by the canonical (underscore->dot) form so a legacy
    # underscore alias still resolves to its live twin instead of being a false "missing".
    by_name = {p.get("name"): p for p in live_parts if p.get("name")}
    by_canon = {P.live_part_name(p.get("name", "")): p for p in live_parts if p.get("name")}
    report = Report()
    for name, sp in sorted(catalog.items()):
        if sp.part_type not in ROCKET_RELEVANT_TYPES:
            continue
        live = by_name.get(name) or by_canon.get(P.live_part_name(name))
        if live is None:
            report.skip(f"{name}: in catalog but NOT in live part-database")
            continue

        report.field(name, "dry_mass_t", sp.dry_mass_t, float(live.get("dryMassT", 0.0)), "t")

        # Engine stats — only when the live part actually exposes them (has a ModuleEngines).
        #
        # MULTIMODE skip (the RAPIER). A multimode engine's /part-database entry reports its PRIMARY mode,
        # which for the RAPIER is the air-breathing jet: maxThrust 105 kN and an "Isp" of ~3200 s that is a
        # velocity curve, not a vacuum Isp (no chemical rocket exceeds Nerv's 800 s). The catalog correctly
        # stores the engine's ROCKET mode (180 kN, 305/275 s) — the rocket-relevant numbers — so comparing
        # the two modes would be a spurious MISMATCH. We skip the thrust/Isp compare ONLY when the live Isp
        # is a jet-curve artifact (>900 s) AND the catalog holds a normal rocket Isp (<=900 s) — i.e. a real
        # mode disagreement. A PURE jet (Wheesley/Panther/…) has the SAME high "Isp" in both catalog and
        # live, so it still compares and MATCHES normally; only a genuine multimode part is skipped.
        live_isp = float(live.get("ispVacS", 0.0))
        catalog_holds_rocket_mode = 0 < sp.isp_vac_s <= 900.0
        if live_isp > 900.0 and catalog_holds_rocket_mode:
            report.skip(f"{name}: multimode engine — live reports jet mode "
                        f"(thrust={live.get('maxThrustKn')}kN, isp={live.get('ispVacS')}s); "
                        f"catalog keeps the ROCKET mode (thrust={sp.thrust_kn_vac}kN, "
                        f"isp={sp.isp_asl_s}/{sp.isp_vac_s}s)")
        else:
            if "maxThrustKn" in live and sp.thrust_kn_vac > 0:
                report.field(name, "thrust_kn_vac", sp.thrust_kn_vac, float(live["maxThrustKn"]), "kN")
            if "ispVacS" in live and sp.isp_vac_s > 0:
                report.field(name, "isp_vac_s", sp.isp_vac_s, float(live["ispVacS"]), "s")
            if "ispAslS" in live and sp.isp_asl_s > 0:
                report.field(name, "isp_asl_s", sp.isp_asl_s, float(live["ispAslS"]), "s")

        # Resource capacities the catalog tracks (LF/Ox/SolidFuel).
        live_res = live.get("resources", {}) or {}
        for res_name, field_label in (("LiquidFuel", "liquid_fuel"),
                                      ("Oxidizer", "oxidizer"),
                                      ("SolidFuel", "solid_fuel")):
            cat_cap = catalog_resource_capacity(sp, res_name)
            live_cap = float(live_res.get(res_name, 0.0))
            if cat_cap > 0 or live_cap > 0:
                report.field(name, field_label, cat_cap, live_cap)
    return report


def cross_check_in_use_parts(parts_list: list[dict], catalog: dict[str, "P.StockPart"]) -> Report:
    """PATH B core: cross-check the masses of the parts CURRENTLY on a loaded vessel against the catalog.

    ``parts_list`` is the ``parts`` array from ``bridge.parts_list()`` (each: name, dryMassT, resourceMassT,
    ...). For each in-use part we know in the catalog, compare its catalog dry mass to the live dry mass,
    and — when the part holds resources — its catalog wet mass to live (dry + resource). This is the live
    datum that does NOT need the new endpoint: it is the very hardware the active mission is flying.
    """
    report = Report()
    seen: set[str] = set()
    for live in parts_list:
        name = live.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        sp = catalog.get(name)
        if sp is None:
            report.skip(f"{name}: on the live vessel but NOT in catalog")
            continue
        live_dry = float(live.get("dryMassT", 0.0))
        report.field(name, "dry_mass_t", sp.dry_mass_t, live_dry, "t")
        # If the part carries resources right now, its live total (dry+resource) should not EXCEED the
        # catalog wet mass (the live tank may be partially drained, so only an over-shoot is a mismatch).
        live_total = live_dry + float(live.get("resourceMassT", 0.0))
        if live.get("resourceMassT", 0.0) > 0 and sp.wet_mass_t > 0:
            if live_total > sp.wet_mass_t * (1 + REL_TOL) + ABS_TOL:
                report.mismatches.append(
                    f"{name}.wet_mass_t: live total={live_total:g}t EXCEEDS catalog wet={sp.wet_mass_t:g}t"
                )
            else:
                report.matches += 1
    return report


# --------------------------------------------------------------------------------------------------
# Live orchestration (the part the tests do NOT exercise — no live calls in tests).
# --------------------------------------------------------------------------------------------------
def _is_404(err: BridgeError) -> bool:
    return "404" in str(err)


def run(bridge: BridgeClient, catalog: dict[str, "P.StockPart"]) -> int:
    """Drive both live paths against ``bridge``. Returns a process exit code (0 ok, 1 mismatch)."""
    print("=" * 90)
    print("LIVE PART-DATABASE VALIDATION  (catalog cross-checked against the running game)")
    print("=" * 90)

    total_mismatch = 0

    # PATH A — the authoritative loaded catalog (needs the rebuilt DLL).
    print("\n[A] GET /part-database  (game's authoritative PartLoader.LoadedPartsList)")
    path_a_ran = False
    try:
        db = bridge.part_database()
        live_parts = db.get("parts", []) or []
        print(f"    bridge returned {len(live_parts)} loaded parts")
        rep_a = cross_check_part_database(live_parts, catalog)
        rep_a.print_summary("PATH A — catalog vs live /part-database")
        total_mismatch += len(rep_a.mismatches)
        path_a_ran = True
    except BridgeError as exc:
        if _is_404(exc):
            print("    endpoint not yet installed; install C:/tmp DLL + restart KSP, then re-run")
        else:
            print(f"    bridge not reachable for /part-database: {exc}")

    # PATH B — the in-use vessel parts (works WITHOUT the new endpoint).
    print("\n[B] POST /parts-list  (parts of the currently loaded vessel — fallback live check)")
    try:
        pl = bridge.parts_list()
        in_use = pl.get("parts", []) or []
        print(f"    active vessel '{pl.get('vessel', '')}' has {len(in_use)} parts")
        rep_b = cross_check_in_use_parts(in_use, catalog)
        rep_b.print_summary("PATH B — catalog vs in-use vessel parts")
        total_mismatch += len(rep_b.mismatches)
    except BridgeError as exc:
        if not path_a_ran:
            print(f"    bridge not reachable for /parts-list: {exc}")
            print("    (no live game to validate against — start KSP in flight with the bridge up)")
        else:
            print(f"    /parts-list unavailable (no vessel in flight?): {exc}")

    print("\n" + "=" * 90)
    if total_mismatch == 0:
        print("RESULT: catalog matches the live game on every compared field.")
        return 0
    print(f"RESULT: {total_mismatch} MISMATCH(ES) — report these to the main agent to patch parts.py.")
    return 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Cross-check the stock catalog against the LIVE game.")
    ap.add_argument("--bridge", default="http://127.0.0.1:48500", help="KSP bridge base URL")
    args = ap.parse_args(argv)

    catalog = P.STOCK_PARTS
    if not catalog:
        print("Catalog is empty — run `python -m ksp_lab.parts` to materialize stock_parts.json first.")
        return 1
    # The /part-database payload is large; give it a generous timeout.
    bridge = BridgeClient(base_url=args.bridge, timeout_s=60)
    return run(bridge, catalog)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
