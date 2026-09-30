"""ConfigNode parser/serializer: KSP syntax edge cases and byte-faithful round trips."""

from pathlib import Path

import pytest

from astra.craft.confignode import ConfigNode, fmt_float, parse_bool, parse_floats, read_text

FIXTURES = Path(__file__).parent / "fixtures"
KSP = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Kerbal Space Program")


def test_values_nodes_and_duplicate_keys_keep_their_order():
    root = ConfigNode.parse("a = 1\nNODE\n{\n\tk = x\n\tk = y\n\tINNER\n\t{\n\t}\n\tk = z\n}\nb = 2\n")
    assert [v.key for v in root.values()] == ["a", "b"]
    node = root.node("NODE")
    assert node.get_all("k") == ["x", "y", "z"]
    assert [type(i).__name__ for i in node.items] == ["Value", "Value", "ConfigNode", "Value"]
    assert root.to_text() == "a = 1\nNODE\n{\n\tk = x\n\tk = y\n\tINNER\n\t{\n\t}\n\tk = z\n}\nb = 2\n"


def test_braces_on_one_line_and_name_before_brace():
    root = ConfigNode.parse("PART {\n name = x\n MODULE { name = ModuleEngines  maxThrust = 60 }\n}\n")
    part = root.node("PART")
    assert part.get("name") == "x"
    mod = part.node("MODULE")
    assert mod.get("name") == "ModuleEngines  maxThrust = 60"  # KSP splits on '=' only once per line
    root = ConfigNode.parse("A\n{\nB\n{\nc = 1\n}\n}\n")
    assert root.node("A").node("B").get("c") == "1"


def test_comments_are_kept_on_their_value_and_dropped_elsewhere():
    text = ("// header comment\nPART\n{\n\tname = radialDecoupler2 // trailing\n"
            "\ttitle = #autoLOC_500820 //#autoLOC_500820 = TT-70 Radial Decoupler\n\tmaxTemp = 2000 // = 3200\n}\n")
    part = ConfigNode.parse(text).node("PART")
    assert part.get("name") == "radialDecoupler2"
    title = part.get_value("title")
    assert title.value == "#autoLOC_500820"
    assert title.comment == "#autoLOC_500820 = TT-70 Radial Decoupler"
    assert part.get("maxTemp") == "2000"
    again = ConfigNode.parse(part.to_text()).node("PART")
    assert again.get_value("title").comment == title.comment


def test_bom_empty_values_and_equals_in_value():
    root = ConfigNode.parse("\ufeffkey = \nurl = a=b=c\n  spaced   =   value with  spaces  \n")
    assert root.get("key") == ""
    assert root.get("url") == "a=b=c"
    assert root.get("spaced") == "value with  spaces"
    assert root.to_text().splitlines()[0] == "key = "


def test_unbalanced_braces_are_tolerated():
    root = ConfigNode.parse("A\n{\nx = 1\n}\n}\nB\n{\ny = 2\n")
    assert root.node("A").get("x") == "1"
    assert root.node("B").get("y") == "2"


def test_editing_api():
    n = ConfigNode("PART")
    n.add("pos", [0, 1, 2]).add("flag", True).add("x", 0.1 + 0.2)
    n.set("pos", "0,1,2")
    n.set("new", 5)
    assert n.get("flag") == "True"
    assert n.get("x") == "0.3"
    assert [v.key for v in n.values()] == ["pos", "flag", "x", "new"]
    assert n.remove("flag") == 1 and not n.has("flag")
    child = n.add_node("MODULE")
    child.add("name", "ModuleEngines")
    assert n.find_nodes("MODULE", name="ModuleEngines") == [child]
    assert n.remove_nodes("MODULE") == 1 and n.nodes() == []


def test_number_helpers():
    assert fmt_float(1e-9) == "0"
    assert fmt_float(-0.0000001) == "-0.0000001"
    assert fmt_float(0.6423756) == "0.6423756"
    assert fmt_float(15.0) == "15"
    assert "e" not in fmt_float(1.23e-5).lower()
    assert parse_floats("0.0, 1.5,2 x 3e-1") == [0.0, 1.5, 2.0, 0.3]
    assert parse_bool("True") and not parse_bool("false") and parse_bool(None, True)


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("*.craft")))
def test_fixture_craft_round_trip_is_byte_faithful(name):
    raw = read_text(FIXTURES / name)
    assert ConfigNode.parse(raw).to_text() == raw.replace("\r\n", "\n")


def test_stock_file_in_windows_1252_is_readable(tmp_path):
    f = tmp_path / "old.craft"
    f.write_bytes("ship = Kerbal 2\ndescription = trainer \xa8\xa8 rocket\n".encode("cp1252"))
    root = ConfigNode.load(f)
    assert root.get("ship") == "Kerbal 2"
    assert "\u00a8" in root.get("description")


@pytest.mark.skipif(not (KSP / "Ships").exists(), reason="KSP install not present")
def test_every_stock_craft_round_trips():
    files = sorted((KSP / "Ships").glob("*/*.craft"))
    assert len(files) > 40
    for f in files:
        raw = read_text(f)
        assert ConfigNode.parse(raw).to_text() == raw.replace("\r\n", "\n"), f.name


@pytest.mark.skipif(not (KSP / "GameData" / "Squad").exists(), reason="KSP install not present")
def test_stock_part_configs_round_trip_semantically():
    def canon(n):
        return n.name, [(i.key, i.value) if not isinstance(i, ConfigNode) else canon(i) for i in n.items]

    files = sorted((KSP / "GameData" / "Squad" / "Parts").rglob("*.cfg"))
    assert len(files) > 300
    for f in files:
        first = ConfigNode.load(f)
        assert canon(ConfigNode.parse(first.to_text())) == canon(first), f.name
