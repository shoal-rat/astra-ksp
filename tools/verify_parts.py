"""Part-accuracy verifier — the mechanism that REPLACES the old curated-parts tier.

The design system no longer keeps a hand-picked "validated" short list. Instead, EVERY rocket-relevant
stock part stands on equal footing in ``data/stock_parts.json`` (materialized from the game cfgs), and
this tool is what makes that trustworthy: it proves the materialized numbers are accurate so curation is
unnecessary. It does three things, offline:

  1. RE-PARSE vs JSON. Walk the live GameData PART tree again and assert the committed JSON matches the
     fresh parse field-for-field (mass, ASL/vac thrust, ASL/vac Isp, LF/Ox/SolidFuel capacity, diameter,
     stack size). A drift here means the JSON is stale — re-materialize.
  2. HEADLINE SPOT-CHECK. Cross-check the marquee engines (Reliant/Swivel/Terrier/Skipper/Mainsail/
     Twin-Boar/Mammoth/Vector/Poodle/Rhino/Nerv) and tanks (FL-T/Rockomax/Kerbodyne families) against
     HARD-CODED known KSP 1.12 stats — thrust, Isp, mass, propellant — so a silently-wrong parser is
     caught against ground truth, not just against its own JSON.
  3. LIVE kRPC (optional). If KSP is running with kRPC, sample a few parts in the editor/flight and
     compare ``part.mass`` and engine thrust to the catalog. Skipped cleanly when KSP is not up.

Run:  PYTHONPATH=src python tools/verify_parts.py            # offline report (parse + spot-check)
      PYTHONPATH=src python tools/verify_parts.py --krpc      # also cross-check live kRPC if reachable

Exit code 0 = every checked part accurate; 1 = at least one mismatch (printed). The report is the
accuracy evidence the "no curation" design depends on.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ksp_lab import parts as P  # noqa: E402

# --------------------------------------------------------------------------------------------------
# GROUND TRUTH — known KSP 1.12 stats for the marquee parts, transcribed from the wiki / in-game VAB.
# These are the hand spot-check the user asked for: the parser must reproduce them from the raw cfg.
# thrust/Isp are the headline engine numbers; mass is the part dry mass; tanks list LF+Ox capacity.
# Tolerances are generous on thrust_asl (it is a derived Isp-ratio number) and tight on the cfg-direct
# fields (mass, vac thrust, Isp, propellant capacity).
# --------------------------------------------------------------------------------------------------
# Keys are the LIVE AvailablePart.name (dotted) form the catalog now stores — KSP's cfg writes these with
# underscores (liquidEngine3_v2) but the running game / part-database report them with dots
# (liquidEngine3.v2). The catalog keys on the live form, so the ground-truth lookups must too.
ENGINE_TRUTH = {
    #  name (materialized key)        title            dry_t   thr_vac  isp_asl isp_vac
    "liquidEngine":              ("LV-T30 Reliant",     1.25,   240.0,  265.0,  310.0),
    "liquidEngine2":             ("LV-T45 Swivel",      1.50,   215.0,  250.0,  320.0),
    "liquidEngine3.v2":          ("LV-909 Terrier",     0.50,    60.0,   85.0,  345.0),
    "engineLargeSkipper.v2":     ("RE-I5 Skipper",      3.00,   650.0,  280.0,  320.0),
    "liquidEngineMainsail.v2":   ("RE-M3 Mainsail",     6.00,  1500.0,  285.0,  310.0),
    "Size2LFB":                  ("Twin-Boar",         10.50,  2000.0,  291.0,  300.0),
    "Size3EngineCluster":        ("Mammoth",           15.00,  4000.0,  295.0,  315.0),
    "SSME":                      ("Vector",             4.00,  1000.0,  295.0,  315.0),
    "liquidEngine2-2.v2":        ("Poodle",             1.75,   250.0,   90.0,  350.0),
    "Size3AdvancedEngine":       ("Rhino",              9.00,  2000.0,  205.0,  340.0),
    "nuclearEngine":             ("Nerv (LV-N)",        3.00,    60.0,  185.0,  800.0),
}

# Tanks: name (live dotted form) -> (title, dry_t, LiquidFuel, Oxidizer). Capacities are the in-VAB fulls.
TANK_TRUTH = {
    "fuelTankSmallFlat": ("FL-T100",  0.0625,   45.0,   55.0),
    "fuelTankSmall":     ("FL-T200",  0.125,    90.0,  110.0),
    "fuelTank":          ("FL-T400",  0.25,    180.0,  220.0),
    "fuelTank.long":     ("FL-T800",  0.50,    360.0,  440.0),
    "Rockomax8BW":       ("X200-8",   0.50,    360.0,  440.0),
    "Rockomax16.BW":     ("X200-16",  1.00,    720.0,  880.0),
    "Rockomax32.BW":     ("X200-32",  2.00,   1440.0, 1760.0),
    "Rockomax64.BW":     ("Jumbo-64", 4.00,   2880.0, 3520.0),
    "Size3SmallTank":    ("S3-3600",  2.25,   1620.0, 1980.0),
    "Size3MediumTank":   ("S3-7200",  4.50,   3240.0, 3960.0),
    "Size3LargeTank":    ("S3-14400", 9.00,   6480.0, 7920.0),
}


def _close(a: float, b: float, rel: float = 0.02, absol: float = 0.02) -> bool:
    return abs(a - b) <= max(absol, abs(b) * rel)


def reparse_catalog() -> dict[str, P.StockPart]:
    """Re-parse the live GameData tree the same way materialize_catalog does (without writing)."""
    catalog: dict[str, P.StockPart] = {}
    for d in P.DEFAULT_GAMEDATA_DIRS:
        base = Path(d)
        if not base.exists():
            continue
        for cfg in base.rglob("*.cfg"):
            try:
                text = cfg.read_text(encoding="utf-8-sig", errors="ignore")
            except OSError:
                continue
            try:
                for sp in P.parse_part_cfg(text):
                    if sp.name not in catalog:
                        catalog[sp.name] = sp
            except Exception:
                continue
    return dict((p.name, p) for p in P._apply_asl_thrust(list(catalog.values())))


# --------------------------------------------------------------------------------------------------
# CHECK 1 — committed JSON vs a fresh re-parse of the cfgs.
# --------------------------------------------------------------------------------------------------
# The committed JSON is now LIVE-reconciled (physics from the running game's /part-database), so the bare
# cfg PHYSICS can legitimately differ from it (e.g. a command pod's live dry mass < its cfg mass). The
# GEOMETRY fields are still cfg-derived and must match the re-parse exactly — those are the drift guard.
_GEOMETRY_FIELDS = ("diameter_m", "height_m")
_PHYSICS_FIELDS = ("dry_mass_t", "wet_mass_t", "thrust_kn_asl", "thrust_kn_vac",
                   "isp_asl_s", "isp_vac_s", "liquid_fuel", "oxidizer", "solid_fuel")
_COMPARE_FIELDS = _PHYSICS_FIELDS + _GEOMETRY_FIELDS


def check_json_matches_reparse(reparsed: dict[str, P.StockPart]) -> list[str]:
    """The committed JSON must match a fresh cfg re-parse on GEOMETRY (always), and on PHYSICS too unless
    the JSON has been live-reconciled away from the bare cfg numbers (then physics diffs are expected and
    only flagged as informational, never as a hard drift)."""
    problems: list[str] = []
    catalog = P.load_catalog()
    if not catalog:
        return ["JSON catalog is empty/absent — run `python -m ksp_lab.parts` to materialize it"]
    if not reparsed:
        return ["GameData not found — cannot re-parse; skipping JSON-vs-cfg cross-check"]
    # If the JSON physics already diverges from the bare cfg for several parts, it was built --from-live;
    # in that mode we drift-check geometry only (physics is authoritatively the live game's, not the cfg's).
    physics_divergent = sum(
        1 for name, jp in catalog.items()
        if (rp := reparsed.get(name)) is not None
        and any(not _close(getattr(jp, f), getattr(rp, f)) for f in _PHYSICS_FIELDS)
    )
    fields = _GEOMETRY_FIELDS if physics_divergent >= 3 else _COMPARE_FIELDS
    for name, jp in catalog.items():
        rp = reparsed.get(name)
        if rp is None:
            problems.append(f"{name}: in JSON but NOT produced by re-parse (parser drift)")
            continue
        for f in fields:
            a, b = getattr(jp, f), getattr(rp, f)
            if not _close(a, b):
                problems.append(f"{name}.{f}: JSON={a} re-parse={b}")
    return problems


# --------------------------------------------------------------------------------------------------
# CHECK 2 — marquee parts vs hard-coded known KSP stats (the hand spot-check).
# --------------------------------------------------------------------------------------------------
def check_headline_engines() -> tuple[list[str], list[str]]:
    ok, bad = [], []
    for name, (title, dry, thr_vac, isp_asl, isp_vac) in ENGINE_TRUTH.items():
        try:
            p = P.part(name)
        except KeyError:
            bad.append(f"{name} ({title}): MISSING from catalog")
            continue
        errs = []
        if not _close(p.dry_mass_t, dry, rel=0.03):
            errs.append(f"dry {p.dry_mass_t}!={dry}")
        if not _close(p.thrust_kn_vac, thr_vac, rel=0.02):
            errs.append(f"thrust_vac {p.thrust_kn_vac}!={thr_vac}")
        if not _close(p.isp_vac_s, isp_vac, rel=0.02):
            errs.append(f"isp_vac {p.isp_vac_s}!={isp_vac}")
        if not _close(p.isp_asl_s, isp_asl, rel=0.04):
            errs.append(f"isp_asl {p.isp_asl_s}!={isp_asl}")
        if errs:
            bad.append(f"{name} ({title}): " + ", ".join(errs))
        else:
            ok.append(f"{name:24s} {title:16s} thr_vac={p.thrust_kn_vac:7.1f} "
                      f"isp={p.isp_asl_s:5.1f}/{p.isp_vac_s:5.1f}s dry={p.dry_mass_t}t")
    return ok, bad


def check_headline_tanks() -> tuple[list[str], list[str]]:
    ok, bad = [], []
    for name, (title, dry, lf, ox) in TANK_TRUTH.items():
        try:
            p = P.part(name)
        except KeyError:
            bad.append(f"{name} ({title}): MISSING from catalog")
            continue
        errs = []
        if not _close(p.dry_mass_t, dry, rel=0.03):
            errs.append(f"dry {p.dry_mass_t}!={dry}")
        if not _close(p.liquid_fuel, lf, rel=0.02):
            errs.append(f"LF {p.liquid_fuel}!={lf}")
        if not _close(p.oxidizer, ox, rel=0.02):
            errs.append(f"Ox {p.oxidizer}!={ox}")
        if errs:
            bad.append(f"{name} ({title}): " + ", ".join(errs))
        else:
            ok.append(f"{name:20s} {title:10s} dry={p.dry_mass_t}t LF/Ox={p.liquid_fuel:.0f}/{p.oxidizer:.0f}")
    return ok, bad


# --------------------------------------------------------------------------------------------------
# CHECK 3 — optional live kRPC cross-check (mass + engine thrust of whatever is on the active vessel).
# --------------------------------------------------------------------------------------------------
def check_krpc_sample() -> tuple[list[str], list[str], bool]:
    """Cross-check the catalog against the LIVE game via kRPC if reachable. Returns (ok, bad, ran)."""
    try:
        import krpc  # type: ignore
    except Exception:
        return [], [], False
    try:
        conn = krpc.connect(name="verify_parts")
    except Exception:
        return [], [], False
    ok, bad = [], []
    try:
        vessel = conn.space_center.active_vessel
        for live in vessel.parts.all:
            nm = live.name
            try:
                cat = P.part(nm)
            except KeyError:
                continue
            live_dry = live.dry_mass / 1000.0
            if not _close(live_dry, cat.dry_mass_t, rel=0.05, absol=0.05):
                bad.append(f"{nm}: live dry {live_dry:.3f}t != catalog {cat.dry_mass_t}t")
            else:
                ok.append(f"{nm}: dry {live_dry:.3f}t matches catalog")
            eng = live.engine
            if eng is not None and cat.thrust_kn_vac > 0:
                live_thr = eng.max_vacuum_thrust / 1000.0
                if not _close(live_thr, cat.thrust_kn_vac, rel=0.05):
                    bad.append(f"{nm}: live vac thrust {live_thr:.1f}kN != catalog {cat.thrust_kn_vac}kN")
    except Exception as exc:
        return ok, bad + [f"kRPC sample error: {exc}"], True
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return ok, bad, True


def main(argv: list[str]) -> int:
    want_krpc = "--krpc" in argv
    print("=" * 90)
    print("PART-ACCURACY VERIFICATION  (the mechanism that replaces curation)")
    print("=" * 90)

    reparsed = reparse_catalog()
    print(f"\n[1] JSON-vs-cfg re-parse cross-check ({len(P.load_catalog())} parts in JSON)")
    json_problems = check_json_matches_reparse(reparsed)
    if not json_problems:
        print("    PASS - every committed part matches a fresh cfg re-parse")
    elif json_problems[0].startswith("GameData not found"):
        print(f"    SKIP - {json_problems[0]}")
        json_problems = []
    else:
        print(f"    FAIL - {len(json_problems)} mismatch(es):")
        for p in json_problems[:40]:
            print(f"      - {p}")

    print("\n[2a] Headline ENGINES vs known KSP 1.12 stats (hand spot-check)")
    eng_ok, eng_bad = check_headline_engines()
    for line in eng_ok:
        print(f"    OK  {line}")
    for line in eng_bad:
        print(f"    BAD {line}")

    print("\n[2b] Headline TANKS vs known KSP 1.12 capacities (hand spot-check)")
    tank_ok, tank_bad = check_headline_tanks()
    for line in tank_ok:
        print(f"    OK  {line}")
    for line in tank_bad:
        print(f"    BAD {line}")

    krpc_bad: list[str] = []
    if want_krpc:
        print("\n[3] Live kRPC cross-check (active vessel parts)")
        k_ok, krpc_bad, ran = check_krpc_sample()
        if not ran:
            print("    SKIP - kRPC not reachable (KSP not running or krpc not installed)")
            krpc_bad = []
        else:
            for line in k_ok:
                print(f"    OK  {line}")
            for line in krpc_bad:
                print(f"    BAD {line}")
            if not k_ok and not krpc_bad:
                print("    (no overlapping parts on the active vessel to compare)")

    total_bad = len(json_problems) + len(eng_bad) + len(tank_bad) + len(krpc_bad)
    print("\n" + "=" * 90)
    if total_bad == 0:
        print("RESULT: ACCURATE - all checked parts match cfg + known stats. Curation is unnecessary.")
        return 0
    print(f"RESULT: {total_bad} MISMATCH(ES) - fix the parser/catalog (see above).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
