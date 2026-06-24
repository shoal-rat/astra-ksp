"""Strip junk vessels (debris + failed interplanetary transfers) from a KSP .sfs save to
cut game lag. Keeps working relays + completed-mission craft. Dry-run by default; pass
--apply to write the cleaned save (a .bak backup is made first)."""
import glob, re, sys

SAVE_GLOB = r"C:/Program Files (x86)/Steam/steamapps/common/Kerbal Space Program/saves/*/ai_cleanup.sfs"

# Vessels to ALWAYS KEEP (working constellation + the owner's completed-mission craft).
KEEP_NAME_SUBSTR = (
    "AI-Keo-2", "AI-Keo-8", "AI-Mun-Relay", "AI-Duna-Comsat", "AI-Duna-Ring-Y",
    "AI-HLS", "AI-Orion", "AI-Starship", "AI-Route-Depot", "AI-Duna-Depot",
)


def find_save():
    cands = sorted(glob.glob(SAVE_GLOB))
    if not cands:
        sys.exit("ai_cleanup.sfs not found")
    return cands[0]


def parse_vessels(text):
    out = []
    for m in re.finditer(r'\n([ \t]*)VESSEL[ \t]*\r?\n[ \t]*\{', text):
        start = m.start() + 1
        bo = text.index('{', m.end() - 1)
        depth, j = 0, bo
        while j < len(text):
            ch = text[j]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        block = text[start:j + 1]
        nm = re.search(r'\n[ \t]*name = (.*)', block)
        ty = re.search(r'\n[ \t]*type = (.*)', block)
        rf = re.search(r'\n[ \t]*REF = (.*)', block)
        out.append({"start": start, "end": j + 1,
                    "name": (nm.group(1).strip() if nm else "?"),
                    "type": (ty.group(1).strip() if ty else "?"),
                    "ref": (rf.group(1).strip() if rf else "?")})
    return out


def keep(v, sun_ref):
    # Safety whitelist for the live constellation (exact names) — never removed.
    CRITICAL = {"AI-Keo-2", "AI-Keo-8", "AI-Mun-Relay-A2",
                "AI-Duna-Comsat-6", "AI-Duna-Comsat-7", "AI-Duna-Comsat-8", "AI-Duna-Ring-Y"}
    if v["name"] in CRITICAL:
        return True
    if v["type"] == "Debris":                          # spent boosters/fairings -> junk
        return False
    if v["type"] == "SpaceObject":                     # asteroids/comets -> natural, keep
        return True
    if sun_ref is not None and v["ref"] == sun_ref:    # heliocentric -> failed transfer, junk
        return False
    return True                                        # working craft around Kerbin/Mun/Duna


def main():
    apply = "--apply" in sys.argv
    sun_ref = None
    for a in sys.argv:
        if a.startswith("--sun="):
            sun_ref = a.split("=", 1)[1]
    f = find_save()
    text = open(f, encoding="utf-8").read()
    vs = parse_vessels(text)
    from collections import Counter
    by_type = Counter(v["type"] for v in vs)
    by_ref = Counter(v["ref"] for v in vs)
    print(f"save: {f}\nvessels: {len(vs)}  by_type: {dict(by_type)}\n  by_ref(orbit body index): {dict(by_ref)}")
    # show a couple of sample names per REF so we can identify the Sun index
    seen = {}
    for v in vs:
        seen.setdefault(v["ref"], []).append(f"{v['name']}[{v['type']}]")
    for r, names in sorted(seen.items()):
        print(f"  REF {r}: {len(names)} e.g. {names[:3]}")
    if sun_ref is None:
        print("\n(no --sun=IDX given; pass the Sun's orbit-body index to enable heliocentric removal)")
    rm = [v for v in vs if not keep(v, sun_ref)]
    kp = [v for v in vs if keep(v, sun_ref)]
    print(f"\nWOULD REMOVE {len(rm)}, KEEP {len(kp)}")
    print("KEEP names:", sorted({v['name'] for v in kp}))
    if apply and sun_ref is not None:
        open(f + ".bak", "w", encoding="utf-8").write(text)
        # remove from the end so offsets stay valid
        new = text
        for v in sorted(rm, key=lambda x: -x["start"]):
            new = new[:v["start"]] + new[v["end"]:]
        # activeVessel is a 0-based INDEX into the (now shorter) vessel list -> repoint it to
        # a kept relay's new index so KSP loads cleanly (else it points past the end / wrong craft)
        try:
            new_idx = next(i for i, v in enumerate(kp) if v["name"] == "AI-Keo-8")
        except StopIteration:
            new_idx = 0
        new = re.sub(r'activeVessel = \d+', f'activeVessel = {new_idx}', new, count=1)
        open(f, "w", encoding="utf-8").write(new)
        print(f"\nAPPLIED: removed {len(rm)} vessels, activeVessel->{new_idx} (AI-Keo-8). "
              f"backup at {f}.bak. New size {len(new)} (was {len(text)}).")


if __name__ == "__main__":
    main()
