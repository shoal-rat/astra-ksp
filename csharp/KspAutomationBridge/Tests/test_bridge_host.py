"""Offline tests for the bridge plugin's transport layer.

Json.cs, Http.cs, MainThread.cs and Capcom.cs contain no Unity or KSP types, so they compile with the
same in-box C# 5 compiler into a small console host (BridgeTestHost.cs) that serves real HTTP on an
ephemeral port. The tests exercise it over sockets: byte-exact UTF-8 bodies, query strings, native
and stringified JSON values, strict JSON output, error envelopes, the abandon-on-timeout job queue
and the CAPCOM message store. The pure decision helpers behind game routes (PausePlanner, EvaMath's
jetpack hop controller, the recorder's FrameQueue) are exposed as /test/* routes. The game-facing
routes themselves need KSP and are verified live (see docs/BRIDGE_API.md).
"""

import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
REPO = SRC.parents[1]
OUT = REPO / ".scratch" / "bridge_host"
PURE_SOURCES = ["Json.cs", "Http.cs", "MainThread.cs", "Capcom.cs", "HarmonyReflect.cs", "PausePlanner.cs", "EvaMath.cs",
                "FrameQueue.cs"]


def _csc() -> Path:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for cand in (windir / r"Microsoft.NET\Framework64\v4.0.30319\csc.exe",
                 windir / r"Microsoft.NET\Framework\v4.0.30319\csc.exe"):
        if cand.exists():
            return cand
    pytest.skip("no .NET Framework csc.exe on this machine")


def _reject_constant(name):
    raise ValueError(f"non-strict JSON constant {name}")


def _loads(text: str):
    return json.loads(text, parse_constant=_reject_constant)


@pytest.fixture(scope="session")
def host_exe() -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    exe = OUT / "BridgeTestHost.exe"
    cmd = [str(_csc()), "-nologo", "-target:exe", "-warnaserror+", f"-out:{exe}"]
    cmd += [str(SRC / f) for f in PURE_SOURCES] + [str(HERE / "BridgeTestHost.cs")]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return exe


@pytest.fixture(scope="module")
def server(host_exe):
    with open(OUT / "host_stderr.log", "wb") as log:
        proc = subprocess.Popen([str(host_exe), "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
        line = proc.stdout.readline().decode().strip()
        assert line.startswith("PORT "), line
        yield int(line.split()[1])
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def call(port, method, path, body=None, raw=None, headers=None, timeout=10.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    data = raw if raw is not None else (json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None)
    conn.request(method, path, body=data, headers=headers or {"Content-Type": "application/json; charset=utf-8"})
    resp = conn.getresponse()
    text = resp.read().decode("utf-8")
    conn.close()
    assert resp.getheader("Content-Type") == "application/json; charset=utf-8"
    return resp.status, _loads(text)


def json_roundtrip(host_exe, text: str):
    proc = subprocess.run([str(host_exe), "json"], input=text.encode("utf-8"), capture_output=True)
    return proc.returncode, proc.stdout.decode("utf-8")


# ---------------------------------------------------------------------------------------- build


def _plugin_sources():
    return sorted(SRC.glob("*.cs")) + sorted((SRC / "Properties").glob("*.cs"))


def test_plugin_sources_are_ascii():
    # csc reads BOM-less sources in the system code page (GBK here): keep them ASCII, use \u escapes.
    for path in _plugin_sources() + [HERE / "BridgeTestHost.cs"]:
        text = path.read_bytes()
        assert all(b < 128 for b in text), f"{path.name} contains non-ASCII bytes"


def test_plugin_compiles_without_warnings():
    sys.path.insert(0, str(REPO / "src"))
    from astra.config import CONFIG

    managed = CONFIG.ksp_dir / "KSP_x64_Data" / "Managed"
    mechjeb = CONFIG.gamedata_dir / "MechJeb2" / "Plugins"
    if not (managed / "Assembly-CSharp.dll").exists() or not (mechjeb / "MechJeb2.dll").exists():
        pytest.skip("KSP or MechJeb2 is not installed here")
    refs = [managed / "Assembly-CSharp.dll", managed / "Assembly-CSharp-firstpass.dll"]
    refs += sorted(managed.glob("UnityEngine*.dll")) + [mechjeb / "MechJeb2.dll", mechjeb / "MechJebLib.dll"]
    OUT.mkdir(parents=True, exist_ok=True)
    cmd = [str(_csc()), "-target:library", "-nologo", "-optimize+", "-warn:4", "-warnaserror+", "-nowarn:1701,1702",
           f"-out:{OUT / 'KspAutomationBridge.check.dll'}"] + [f"-reference:{r}" for r in refs]
    proc = subprocess.run(cmd + [str(p) for p in _plugin_sources()], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_every_route_is_documented():
    routes = set()
    for path in _plugin_sources():
        text = path.read_text(encoding="ascii")
        routes |= set(re.findall(r'(?:r\.Main|r\.Inline|Inline|Add\(r,)\s*\(?"(GET|POST)",\s*"(/[^"]*)"', text))
    assert len(routes) >= 40
    doc = (REPO / "docs" / "BRIDGE_API.md").read_text(encoding="utf-8")
    index = set()
    for methods, route in re.findall(r"^\| (GET|POST|GET/POST) \| `(/[^`?]*)", doc, flags=re.M):
        index |= {(method, route) for method in methods.split("/")}
    missing = sorted(r for r in routes if r not in index)
    assert not missing, f"routes missing from the docs/BRIDGE_API.md index: {missing}"
    stale = sorted(r for r in index if r not in routes)
    assert not stale, f"documented routes that do not exist: {stale}"


def _route_registrations():
    """(method, path) -> 'Main'|'Inline' for every route the plugin registers."""
    found = {}
    for path in _plugin_sources():
        text = path.read_text(encoding="ascii")
        for kind, method, route in re.findall(r'r\.(Main|Inline)\(\s*"(GET|POST)",\s*"(/[^"]*)"', text):
            found[(method, route)] = kind
    return found


def test_pause_routes_are_registered_on_the_main_thread():
    # FlightDriver.SetPause / PauseMenu.Close touch Unity state: they must run as main-thread jobs.
    regs = _route_registrations()
    assert regs.get(("GET", "/pause")) == "Main"
    assert regs.get(("POST", "/pause")) == "Main"
    source = (SRC / "GameRoutes.cs").read_text(encoding="ascii")
    # Never the kRPC path (PauseMenu.Display opens the ESC menu); postScreenMessage=false on both calls.
    assert "PauseMenu.Display" not in source
    assert "FlightDriver.SetPause(true, false)" in source and "FlightDriver.SetPause(false, false)" in source
    assert 'req.RequireBool("paused")' in source  # no default: pausing is always an explicit request
    doc = (REPO / "docs" / "BRIDGE_API.md").read_text(encoding="utf-8")
    assert "### GET or POST /pause" in doc and "pauseMenuOpen" in doc


def test_leaving_flight_never_discards_it_by_default():
    # /space-center from flight either saves or throws the flight away: the caller must say which.
    source = (SRC / "GameRoutes.cs").read_text(encoding="ascii")
    assert 'req.Bool("saveFirst", false)' not in source
    assert "Missing required parameter 'saveFirst' when leaving flight." in source


def test_stock_game_rules_are_checked_before_acting():
    # FlightEVA.spawnEVA, KerbalEVA.PlantFlag/BoardPart and the transfer calls do not check the game's
    # own rules themselves (the stock buttons do); skipping them would let career saves bypass
    # progression. Offline we can only pin that the checks stay in place.
    eva = (SRC / "EvaRoutes.cs").read_text(encoding="ascii")
    assert "EvaLockedReason(pcm, part, from)" in eva and "UnlockedEVA(" in eva and "EVAIsPossible(" in eva
    assert "KerbalType.Tourist" in eva and "Parameters.Flight.CanEVA" in eva and "Parameters.Flight.CanBoard" in eva
    flags = (SRC / "FlagPlanting.cs").read_text(encoding="ascii")
    assert "UnlockedEVAFlags(" in flags and "GroundContact" in flags and "isRagdoll" in flags
    crew = (SRC / "CrewRoutes.cs").read_text(encoding="ascii")
    assert "crewTransferAvailable" in crew
    game = (SRC / "GameRoutes.cs").read_text(encoding="ascii")
    assert "DiscoveryLevels.Owned" in game and "if (!IsOwned(v))" in game


def test_part_database_curve_keys_carry_tangents_and_engine_module():
    source = (SRC / "PartDatabase.cs").read_text(encoding="ascii")
    assert "new[] { k.time, k.value, k.inTangent, k.outTangent }" in source
    assert 'o["module"] =' in source and "e.moduleName" in source
    doc = (REPO / "docs" / "BRIDGE_API.md").read_text(encoding="utf-8")
    assert "[pressure_atm, isp_s, inTangent, outTangent]" in doc and '"module": "ModuleEngines"' in doc


# ---------------------------------------------------------------------------------------- Harmony


def test_harmony_postfix_through_reflection(host_exe):
    # The EVA walker patches KerbalEVA through HarmonyReflect; prove the reflection path works with
    # the installed 0Harmony.dll on a probe method.
    sys.path.insert(0, str(REPO / "src"))
    from astra.config import CONFIG

    harmony = CONFIG.gamedata_dir / "000_Harmony" / "0Harmony.dll"
    if not harmony.exists():
        pytest.skip("Harmony is not installed here")
    proc = subprocess.run([str(host_exe), "harmony", str(harmony)], capture_output=True, text=True, timeout=60)
    assert proc.stdout == "VALUE 101", proc.stdout + proc.stderr


def test_harmony_missing_is_reported(host_exe):
    proc = subprocess.run([str(host_exe), "harmony"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 3
    assert proc.stdout.startswith("ERROR Harmony (0Harmony.dll, HarmonyLib 2.x) is not loaded")


# ---------------------------------------------------------------------------------------- JSON


def test_json_parser_roundtrips_nested_values(host_exe):
    doc = {"a": [1, 2.5, -3e-7, True, False, None], "s": "tab\t quote\" back\\ 默认 😀", "o": {"k": {}}, "e": []}
    rc, out = json_roundtrip(host_exe, json.dumps(doc, ensure_ascii=True))
    assert rc == 0
    assert _loads(out) == doc


def test_json_parser_rejects_malformed_input(host_exe):
    for bad in ('{"a": }', '{"a": 1,}', '[1 2]', '"unterminated', '{"a": tru}', '{"a": "\\x"}', "{} extra"):
        rc, out = json_roundtrip(host_exe, bad)
        assert rc == 2, bad
        assert out.startswith("ERROR Invalid JSON at character"), out


def test_json_writer_is_strict_and_escapes(server):
    status, data = call(server, "GET", "/test/values")
    assert status == 200 and data["ok"] is True
    assert data["nan"] is None and data["inf"] is None and data["fnan"] is None
    assert data["big"] == 4294967295
    assert data["long"] == 9007199254740993
    assert data["text"] == "line1\nline2\t\"quoted\" \\ \u0001 \u2028 默认"
    assert data["nested"] == {"list": [1, 2.5, "x", None, True], "floats": [0.1, 1e-07]}
    assert data["empty"] == {}
    assert data["enum"] == "Friday"


# ---------------------------------------------------------------------------------------- HTTP


def test_non_ascii_body_is_read_by_bytes_not_chars(server):
    started = time.monotonic()
    status, data = call(server, "POST", "/echo", {"saveFolder": "默认", "needle": "液体燃料"})
    assert time.monotonic() - started < 2.0  # the old parser hung until the client gave up
    assert status == 200
    assert data["params"] == {"saveFolder": "默认", "needle": "液体燃料"}


def test_unicode_escapes_are_decoded(server):
    raw = json.dumps({"name": "默认 😀"}, ensure_ascii=True).encode("ascii")
    status, data = call(server, "POST", "/echo", raw=raw)
    assert status == 200 and data["params"]["name"] == "默认 😀"


def test_query_string_is_split_from_path_and_merged(server):
    status, data = call(server, "GET", "/echo?x=1&name=%E9%BB%98%E8%AE%A4&flag=true&plus=a+b")
    assert status == 200
    assert data["path"] == "/echo"
    assert data["params"] == {"x": "1", "name": "默认", "flag": "true", "plus": "a b"}


def test_body_overrides_query_and_keeps_native_types(server):
    status, data = call(server, "POST", "/echo?a=query", {"a": "body", "n": 80000, "b": False, "l": [1, "2"], "z": None})
    assert status == 200
    assert data["params"] == {"a": "body", "n": 80000, "b": False, "l": [1, "2"], "z": None}
    assert data["types"] == {"a": "String", "n": "Double", "b": "Boolean", "l": "List`1", "z": "null"}


def test_trailing_slash_is_ignored(server):
    status, data = call(server, "GET", "/echo/")
    assert status == 200 and data["path"] == "/echo"


@pytest.mark.parametrize("payload", [
    {"num": 80000, "flag": True, "int": 3, "id": 4294967295, "str": "x", "choice": "BETA", "list": ["a", "b"]},
    {"num": "80000", "flag": "true", "int": "3", "id": "4294967295", "str": "x", "choice": "beta", "list": "a, b"},
])
def test_typed_parameters_accept_strings_and_native_json(server, payload):
    status, data = call(server, "POST", "/test/typed", payload)
    assert status == 200, data
    assert data["num"] == 80000 and data["flag"] is True and data["int"] == 3
    assert data["id"] == 4294967295 and data["str"] == "x" and data["choice"] == "beta"
    assert data["list"] == ["a", "b"]
    assert isinstance(data["jobId"], int) and data["jobId"] > 0


def test_list_parameters_tolerate_stringified_python_lists(server):
    # astra.bridge stringifies every value; str(["ascent", "node"]) must still read as a list.
    status, data = call(server, "POST", "/test/typed", {"list": str(["ascent", "node"])})
    assert status == 200 and data["list"] == ["ascent", "node"]


def test_empty_strings_count_as_absent(server):
    status, data = call(server, "POST", "/test/typed", {"num": "", "flag": " ", "choice": ""})
    assert status == 200
    assert data["num"] is None and data["flag"] is None and data["choice"] == "alpha"


@pytest.mark.parametrize("payload, fragment", [
    ({"num": "abc"}, "Parameter 'num' must be a number (got 'abc')"),
    ({"flag": "maybe"}, "Parameter 'flag' must be a boolean"),
    ({"int": 2.5}, "Parameter 'int' must be an integer"),
    ({"id": -1}, "Parameter 'id' must be an unsigned 32-bit integer id"),
    ({"choice": "gamma"}, "Parameter 'choice' must be one of: alpha, beta"),
    ({"str": {"nested": 1}}, "Parameter 'str' must be a string"),
    ({"requireMissing": True}, "Missing required parameter 'absentParameter'"),
])
def test_bad_parameters_are_400_with_the_parameter_named(server, payload, fragment):
    status, data = call(server, "POST", "/test/typed", payload)
    assert status == 400
    assert data["ok"] is False and fragment in data["error"]


@pytest.mark.parametrize("value", ["NaN", "nan", "Infinity", "-Infinity", " NaN ", "1e999"])
def test_non_finite_numbers_are_refused(server, value):
    # double.TryParse accepts "NaN"/"Infinity"; a NaN target handed to MechJeb or the EVA walker would
    # corrupt the vessel instead of failing, so every numeric parameter must be finite.
    status, data = call(server, "POST", "/test/typed", {"num": value})
    assert status == 400, data
    assert "Parameter 'num' must be a" in data["error"]
    status, data = call(server, "POST", "/test/typed", {"num": 1.5, "need": value})
    assert status == 400 and "Parameter 'need'" in data["error"]


def test_native_json_overflow_number_is_refused(server):
    status, data = call(server, "POST", "/test/typed", raw=b'{"num": 1e999}')
    assert status == 400, data


def test_int_and_uint_parameters_refuse_non_finite_strings(server):
    for key in ("int", "id"):
        status, data = call(server, "POST", "/test/typed", {key: "Infinity"})
        assert status == 400 and f"Parameter '{key}'" in data["error"]


# ---------------------------------------------------------------------------------------- pause


def _pause_plan(port, want, fdp, scale, menu, in_flight=True, started=True, driver=True, load_requested=False):
    status, data = call(port, "POST", "/test/pause-plan",
                        {"want": want, "flightDriverPause": fdp, "timeScale": scale, "menuOpen": menu, "inFlight": in_flight,
                         "flightStarted": started, "flightDriverExists": driver, "sceneLoadRequested": load_requested})
    assert status == 200, data
    return data


@pytest.mark.parametrize("want, fdp, scale, menu, action", [
    # pause
    (True, False, 1.0, False, "Pause"),
    (True, False, 4.0, False, "Pause"),               # physics warp running
    (True, True, 0.0, False, "None"),                 # already paused without the menu (also mid vessel switch)
    (True, True, 0.0, True, "CloseMenuThenPause"),    # kRPC paused (menu): keep the pause, drop the menu
    (True, True, 1.0, False, "Pause"),                # inconsistent flag: re-apply so time really stops
    (True, False, 0.0, False, "Pause"),               # time stopped by someone else: take the pause over
    # resume
    (False, True, 0.0, True, "CloseMenu"),            # kRPC paused: closing the menu resumes
    (False, True, 0.0, False, "Resume"),              # paused by POST /pause
    (False, False, 0.0, False, "Resume"),             # time scale held at 0 without the flag
    (False, False, 1.0, False, "None"),               # already running
    (False, False, 1.0, True, "CloseMenu"),           # stale menu over a running game
])
def test_pause_planner(server, want, fdp, scale, menu, action):
    assert _pause_plan(server, want, fdp, scale, menu)["action"] == action


@pytest.mark.parametrize("started, driver, load_requested, loaded", [
    (True, True, False, True),
    (False, True, False, False),   # FlightDriver.Awake ran, Start has not finished
    (True, False, False, False),   # stale flightStarted after the old FlightDriver was destroyed
    (True, True, True, False),     # a load was requested; the old scene's teardown will reset the pause
])
def test_flight_loaded(server, started, driver, load_requested, loaded):
    data = _pause_plan(server, True, False, 1.0, False, started=started, driver=driver, load_requested=load_requested)
    assert data["flightLoaded"] is loaded
    assert data["action"] == ("Pause" if loaded else "Refuse")


@pytest.mark.parametrize("want, fdp, scale, menu, action", [
    (True, False, 1.0, False, "Refuse"),
    (True, True, 0.0, False, "Refuse"),   # looks paused, but the start-up will undo it: never report success
    (True, True, 0.0, True, "Refuse"),
    (False, True, 0.0, False, "Resume"),  # resuming is always allowed
    (False, True, 0.0, True, "CloseMenu"),
    (False, False, 1.0, False, "None"),
])
def test_pause_is_refused_only_while_the_scene_loads(server, want, fdp, scale, menu, action):
    assert _pause_plan(server, want, fdp, scale, menu, started=False)["action"] == action


def test_pause_is_not_gated_on_flightglobals_ready():
    # A vessel switch (EVA, boarding) while paused clears FlightGlobals.ready until physics runs again;
    # that used to 409 every later pause and push ASTRA onto kRPC's pause (the ESC menu).
    source = (SRC / "GameRoutes.cs").read_text(encoding="ascii")
    set_pause = source[source.index("private static JObj SetPause("):source.index("// ---", source.index("private static JObj SetPause("))]
    assert "FlightGlobals.ready" not in set_pause
    assert "case PauseAction.Refuse:" in set_pause and "409" in set_pause
    assert 'd["flightLoaded"]' in source and "SceneLoadWatch.Install()" in source


@pytest.mark.parametrize("in_flight, fdp, scale, paused", [
    (True, True, 0.0, True),
    (True, True, 1.0, True),     # FlightDriver.Pause counts in flight, whoever set it
    (True, False, 0.0, True),
    (True, False, 1.0, False),
    (False, True, 1.0, False),   # a stale FlightDriver flag outside flight is ignored
    (False, False, 0.0, True),   # space-center pause menu stops time
])
def test_true_paused_state(server, in_flight, fdp, scale, paused):
    assert _pause_plan(server, True, fdp, scale, False, in_flight=in_flight)["isPaused"] is paused


# ---------------------------------------------------------------------------------------- EVA hop


# KerbalEVA after StartEVA: linPower 8.63 kN x massMultiplier 0.03; a kerbal with pack is ~0.1 t.
PACK = {"linPower": 0.2589, "thrustPercentage": 100.0, "mass": 0.1}
UP = [0.0, 1.0, 0.0]
AT_REST = [0.0, 0.0, 0.0]


def _hop(port, horizontal, gravity_mps2, height_error=0.0, velocity=AT_REST, **pack):
    body = dict(PACK, **pack)
    body.update({"horizontal": horizontal, "up": UP, "heightError": height_error, "surfaceVelocity": velocity,
                 "gravity": [0.0, -gravity_mps2, 0.0]})
    status, data = call(port, "POST", "/test/hop-command", body)
    assert status == 200, data
    return data


def test_hop_hover_compensates_gravity(server):
    data = _hop(server, [0.0, 0.0, 0.0], 1.63)  # the Mun, over the target, at the cruise height
    full = PACK["linPower"] * 1.0 / PACK["mass"]
    assert data["jetpackAccel"] == pytest.approx(full)
    assert data["command"] == pytest.approx([0.0, 1.63 / full, 0.0])  # pushes UP, g/full of the pack
    assert data["canLift"] is True and data["released"] is True


def test_hop_saturates_where_the_pack_cannot_lift(server):
    data = _hop(server, [5.0, 0.0, 0.0], 9.81)  # Kerbin
    assert data["magnitude"] == pytest.approx(1.0)
    assert data["command"][1] > 0.95  # nearly all of it spent against gravity
    assert data["canLift"] is False
    assert _hop(server, [5.0, 0.0, 0.0], 2.94)["canLift"] is False  # Duna: above g, below the 1.3 margin
    assert _hop(server, [5.0, 0.0, 0.0], 0.491)["canLift"] is True  # Minmus


def test_hop_glides_from_a_hatch_where_the_pack_cannot_lift(server):
    full = PACK["linPower"] * 1.0 / PACK["mass"]
    duna = full / 0.94  # the live Duna case: the pack gives 94% of gravity (2.76 of 2.92 m/s^2)
    assert _hop(server, [5.0, 0.0, 0.0], duna, height=2.5)["canHop"] is True  # glides out from the hatch
    assert _hop(server, [5.0, 0.0, 0.0], duna, height=0.3)["canHop"] is False  # standing: cannot take off
    assert _hop(server, [5.0, 0.0, 0.0], full * 2.5, height=2.5)["canHop"] is False  # too weak even to glide
    assert _hop(server, [5.0, 0.0, 0.0], 0.491, height=0.0)["canHop"] is True  # Minmus lifts from the ground


def test_hop_thrust_percentage_scales_the_request(server):
    full = _hop(server, [0.0, 0.0, 0.0], 0.491)
    half = _hop(server, [0.0, 0.0, 0.0], 0.491, thrustPercentage=50.0)
    assert half["jetpackAccel"] == pytest.approx(full["jetpackAccel"] / 2)
    assert half["command"][1] == pytest.approx(2 * full["command"][1])


def test_hop_velocity_command(server):
    full = PACK["linPower"] / PACK["mass"]
    far = _hop(server, [20.0, 0.0, 0.0], 0.0)  # capped at 1.5 m/s, gain 1.5
    assert far["command"] == pytest.approx([1.5 * 1.5 / full, 0.0, 0.0])
    near = _hop(server, [1.0, 0.0, 0.0], 0.0)  # slows to 0.8 m/s per metre left
    assert near["command"] == pytest.approx([0.8 * 1.5 / full, 0.0, 0.0])
    assert _hop(server, AT_REST, 0.0, height_error=5.0)["command"][1] == pytest.approx(1.5 / full)  # climb capped at 1 m/s
    assert _hop(server, AT_REST, 0.0, height_error=-5.0)["command"][1] == pytest.approx(-1.5 / full)
    rising = _hop(server, AT_REST, 1.63, velocity=[0.0, 0.5, 0.0])  # damps a climb it did not ask for
    assert rising["command"][1] == pytest.approx((1.63 - 0.75) / full)


def test_hop_release_radius(server):
    assert _hop(server, [0.5, 0.0, 0.0], 1.63)["released"] is True
    assert _hop(server, [0.7, 0.0, 0.0], 1.63)["released"] is False


@pytest.mark.parametrize("last, ut, gap", [
    (None, 100.0, 0.0),     # first tick
    (100.0, 100.02, 0.0),   # one physics step
    (100.0, 100.08, 0.0),   # one step at 4x physics warp
    (100.0, 101.5, 1.5),    # packed or on rails in between
    (100.0, 400.0, 300.0),
])
def test_rails_gap_is_not_walking_time(server, last, ut, gap):
    body = {"ut": ut} if last is None else {"lastUt": last, "ut": ut}
    status, data = call(server, "POST", "/test/rails-gap", body)
    assert status == 200 and data["gap"] == pytest.approx(gap)


def test_walks_and_hops_cancel_each_other_and_outcomes_reset_on_load():
    walker = (SRC / "EvaWalker.cs").read_text(encoding="ascii")
    assert 'EndHop(eva, kerbal, hop, "replaced by walk")' in walker
    assert 'EndHop(eva, kerbal, hop, "stopped")' in walker
    assert 'Finish(kerbal.persistentId, order, "replaced by hop")' in walker
    assert "Outcomes.Clear(); HopOutcomes.Clear();" in walker
    assert "EvaMath.RailsGap(order.LastUt, ut)" in walker and "EvaMath.RailsGap(hop.LastUt, ut)" in walker
    assert "EvaMath.HopCommand(" in walker
    routes = (SRC / "EvaRoutes.cs").read_text(encoding="ascii")
    hop_to = routes[routes.index("private static JObj HopTo("):routes.index("internal static void Destination(")]
    assert "EvaMath.CanHop(" in hop_to and "maxS <= 0 || maxS > 120" in hop_to
    assert 'req.Bool("stop", false)' in hop_to


# ---------------------------------------------------------------------------------------- recorder queue


def _queue(port, scenario):
    status, data = call(port, "POST", "/test/frame-queue", {"scenario": scenario}, timeout=30.0)
    assert status == 200, data
    return data


def test_frame_queue_numbers_rows_and_frames_alike(server):
    data = _queue(server, "order")
    assert data["finished"] is True
    assert data["numbers"] == [1, 2, 3, 4, 5]
    assert data["index"] == [f"{i},row{i}" for i in range(1, 6)]
    assert data["sunk"] == [f"{i}:{10 + i}:4" for i in range(1, 6)]
    assert data["frames"] == 5 and data["written"] == 5 and data["bytes"] == 20


def test_frame_queue_drains_on_finish(server):
    data = _queue(server, "drain")
    assert data["finished"] is True and data["writerAlive"] is False
    assert data["written"] == 8 and data["sunk"] == list(range(1, 9))


def test_frame_queue_caps_buffers_and_backlog(server):
    data = _queue(server, "cap")
    assert data["threeRented"] and data["fourthRefused"] and data["returnedReused"]
    assert data["foreignDropped"] and data["closedRefuses"]
    assert data["fullWithNoneWaiting"] is False and data["fullAtTwoWaiting"] is True
    assert data["finished"] is True and data["fullAfter"] is False and data["written"] == 3


def test_frame_queue_refuses_late_frames(server):
    data = _queue(server, "stale")
    assert data["finished"] is True and data["closed"] is True
    assert data["lateNumber"] == 0 and data["frames"] == 1
    assert data["index"] == ["1,before"] and data["sunk"] == [1]


def test_frame_queue_failed_writes_are_counted_and_recycled(server):
    data = _queue(server, "sink-throws")
    assert data["capBefore"] and data["bothFailed"] and data["recycled"] and data["capAfter"]
    assert data["written"] == 0 and data["failed"] == 2 and "disk gone" in data["lastError"]
    assert data["index"] == ["1,r1", "2,r2"]  # the rows exist: failedWrites is what shows the gap
    assert data["finished"] is True


def test_recorder_tears_down_outside_readback_callbacks():
    source = (SRC / "Recorder.cs").read_text(encoding="ascii")

    def body(signature):
        start = source.index(signature)
        return source[start:source.index("\n        }\n", start)]

    assert "StopNow(" not in body("private static void OnReadback(") and "StopNow(" not in body("private static void Accepted(")
    assert "_stopRequest = " in body("private static void Accepted(")
    assert "StopNow(_stopRequest)" in body("private static void Tick()")
    mark = body("private static JObj Mark(")
    assert mark.index("AsyncGPUReadback.WaitAllRequests()") < mark.index("Frames()")
    start = body("private static JObj Start(")
    assert "catch (Exception)" in start and "AbortStart();" in start
    assert start.index("frames.csv") < start.index("Process.Start(") and "_queue.WriterAlive" in start


def test_malformed_or_non_object_bodies_are_400(server):
    status, data = call(server, "POST", "/echo", raw=b'{"a": ')
    assert status == 400 and "Invalid JSON" in data["error"]
    status, data = call(server, "POST", "/echo", raw=b"[1, 2]")
    assert status == 400 and "JSON object" in data["error"]


def test_unknown_route_and_wrong_method(server):
    status, data = call(server, "GET", "/nope")
    assert status == 404 and data["error"] == "Unknown route: GET /nope" and "GET /routes" in data["hint"]
    status, data = call(server, "DELETE", "/echo")
    assert status == 405 and "use GET or POST" in data["error"]


def test_handler_errors_become_envelopes(server):
    status, data = call(server, "GET", "/test/throw")
    assert status == 500 and data == {"ok": False, "error": "InvalidOperationException: boom",
                                      "hint": "Unexpected plugin error; KSP.log has the stack trace.",
                                      "jobId": data["jobId"]}
    status, data = call(server, "GET", "/test/bridge-error")
    assert status == 409 and data["error"] == "wrong scene" and data["hint"] == "go to flight"


def test_expect_100_continue_is_answered(server):
    body = json.dumps({"k": "v" * 2000}).encode()
    with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
        s.sendall(b"POST /echo HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                  b"Expect: 100-continue\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n")
        assert s.recv(64).startswith(b"HTTP/1.1 100 Continue")
        s.sendall(body)
        response = b""
        while chunk := s.recv(65536):
            response += chunk
    head, _, payload = response.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 OK") and b"Connection: close" in head
    assert _loads(payload.decode())["params"]["k"] == "v" * 2000


def test_truncated_body_does_not_wedge_the_server(server):
    with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
        s.sendall(b"POST /echo HTTP/1.1\r\nContent-Length: 100\r\n\r\n{\"a\":")
        s.shutdown(socket.SHUT_WR)
        response = s.recv(65536)
    assert response.startswith(b"HTTP/1.1 400")
    status, _ = call(server, "GET", "/echo")
    assert status == 200


def test_concurrent_requests(server):
    def one(i):
        return call(server, "POST", "/test/typed", {"num": i})

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(one, range(40)))
    assert sorted(d["num"] for _, d in results) == list(range(40))
    assert len({d["jobId"] for _, d in results}) == 40


@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.example"},  # any web page can POST to 127.0.0.1 without a CORS preflight
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Host": "evil.example:48500"},  # DNS rebinding: a page's own name resolving to 127.0.0.1
])
def test_browser_cross_site_requests_are_refused_before_running(server, headers):
    _, before = call(server, "GET", "/test/counter-value")
    status, data = call(server, "POST", "/test/counter", raw=b"{}", headers={"Content-Type": "text/plain", **headers})
    assert status == 403 and data["ok"] is False
    _, after = call(server, "GET", "/test/counter-value")
    assert after["counter"] == before["counter"]


def _raw_exchange(port, head: bytes, body: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        try:
            s.sendall(head + body)
        except ConnectionError:
            pass  # the server may answer and stop reading before the whole body is sent
        time.sleep(0.05)
        data = b""
        while chunk := s.recv(65536):
            data += chunk
        return data


@pytest.mark.parametrize("case", ["cross_site", "too_large", "chunked"])
def test_requests_refused_before_their_body_still_get_the_error(server, case):
    # A refusal sent before the body was read used to be followed by close() with unread bytes, which
    # makes Windows send a TCP RST: the client saw "connection reset" (ASTRA: "bridge unreachable")
    # instead of the 403/413/411. Deterministic with a body bigger than the socket buffers.
    body = b"{" + b" " * 200_000 + b"}"
    if case == "cross_site":
        head, expected = (b"POST /test/counter HTTP/1.1\r\nHost: evil.example\r\nContent-Length: "
                          + str(len(body)).encode() + b"\r\n\r\n"), b"HTTP/1.1 403"
    elif case == "too_large":
        head, expected = b"POST /echo HTTP/1.1\r\nContent-Length: 9000000\r\n\r\n", b"HTTP/1.1 413"
    else:
        head, expected = b"POST /echo HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n", b"HTTP/1.1 411"
    for _ in range(5):
        response = _raw_exchange(server, head, body)
        assert response.startswith(expected), response[:80]
        assert _loads(response.partition(b"\r\n\r\n")[2].decode())["ok"] is False


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "none"},  # a URL typed into the address bar
    {"Host": "localhost:48500"},
    {"Host": "LOCALHOST"},
    {"Host": "[::1]:48500"},
])
def test_local_clients_and_typed_urls_are_allowed(server, headers):
    status, data = call(server, "GET", "/echo?x=1", headers=headers)
    assert status == 200 and data["params"] == {"x": "1"}


def test_routes_are_self_described(server):
    status, data = call(server, "GET", "/routes")
    assert status == 200
    paths = {(r["method"], r["path"]) for r in data["routes"]}
    assert ("GET", "/job") in paths and ("POST", "/capcom") in paths
    counter = next(r for r in data["routes"] if r["path"] == "/test/counter")
    assert counter["mainThread"] is True and counter["timeout_s"] == 0.5


# ---------------------------------------------------------------------------------------- job queue


def test_timed_out_job_is_abandoned_and_never_runs(server):
    _, before = call(server, "GET", "/test/counter-value")
    call(server, "POST", "/test/freeze", {"ms": 1500})
    status, data = call(server, "POST", "/test/counter")
    assert status == 504
    assert "abandoned and will NOT run" in data["error"]
    job_id = data["jobId"]
    time.sleep(2.0)  # the simulated main thread is draining again
    _, after = call(server, "GET", "/test/counter-value")
    assert after["counter"] == before["counter"]  # no ghost execution
    status, job = call(server, "GET", f"/job?id={job_id}")
    assert status == 200 and job["state"] == "abandoned" and "result" not in job
    status, data = call(server, "POST", "/test/counter")
    assert status == 200 and data["counter"] == before["counter"] + 1


def test_client_that_hangs_up_abandons_its_queued_job(server):
    # A client whose own timeout is shorter than the route's must not leave a ghost job behind.
    _, before = call(server, "GET", "/test/counter-value")
    call(server, "POST", "/test/freeze", {"ms": 1500})
    with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
        s.sendall(b"POST /test/counter-patient HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 2\r\n\r\n{}")
        time.sleep(0.3)  # the client gives up while the main thread is still stalled
    time.sleep(2.0)  # the simulated main thread is draining again
    _, after = call(server, "GET", "/test/counter-value")
    assert after["counter"] == before["counter"]  # abandoned, never ran
    # Control: a client that waits through the same stall gets its job run.
    call(server, "POST", "/test/freeze", {"ms": 600})
    status, data = call(server, "POST", "/test/counter-patient")
    assert status == 200 and data["counter"] == before["counter"] + 1


def test_slow_job_reports_it_will_finish_and_is_queryable(server):
    status, data = call(server, "POST", "/test/slow", {"ms": 1200})
    assert status == 504 and "will still complete" in data["error"]
    job_id = data["jobId"]
    assert f"/job?id={job_id}" in data["hint"]
    deadline = time.monotonic() + 5
    job = {}
    while time.monotonic() < deadline:
        _, job = call(server, "GET", f"/job?id={job_id}")
        if job["state"] == "finished":
            break
        time.sleep(0.1)
    assert job["state"] == "finished"
    assert job["result"]["ok"] is True and job["result"]["data"] == {"slept": True}


def test_unknown_job_id_is_404(server):
    status, data = call(server, "GET", "/job?id=999999")
    assert status == 404


# ---------------------------------------------------------------------------------------- CAPCOM


def test_capcom_thread_and_inbox(server):
    status, first = call(server, "POST", "/capcom", {"text": "  Liftoff confirmed. 起飞！ ", "level": "warn"})
    assert status == 200 and first["level"] == "warn" and first["shownOnScreen"] is True
    status, inbox = call(server, "GET", f"/capcom/inbox?since={first['seq']}")
    assert status == 200 and inbox["messages"] == [] and inbox["count"] == 0

    _, typed = call(server, "POST", "/test/player", {"text": "Jeb, check the fuel"})
    assert typed["seq"] == first["seq"] + 1
    status, inbox = call(server, "POST", "/capcom/inbox", {"since": first["seq"]})
    assert status == 200 and inbox["count"] == 1
    msg = inbox["messages"][0]
    assert msg["from"] == "player" and msg["text"] == "Jeb, check the fuel" and msg["ut"] == 123.5
    assert inbox["lastSeq"] == typed["seq"]

    _, later = call(server, "GET", f"/capcom/inbox?since={typed['seq']}")
    assert later["messages"] == []

    status, tail = call(server, "GET", "/capcom?limit=2")
    assert [m["seq"] for m in tail["messages"]] == [first["seq"], typed["seq"]]
    assert tail["messages"][0]["text"] == "Liftoff confirmed. 起飞！" and tail["messages"][0]["from"] == "ai"
    assert tail["messages"][0]["ut"] is None  # AI posts have no game time (NaN -> null)


def test_capcom_validation(server):
    assert call(server, "POST", "/capcom", {"text": "x", "level": "panic"})[0] == 400
    assert call(server, "POST", "/capcom", {"text": "   "})[0] == 400
    assert call(server, "POST", "/capcom", {})[0] == 400
    status, data = call(server, "POST", "/capcom", {"text": "y" * 5000})
    assert status == 200
    _, tail = call(server, "GET", "/capcom?limit=1")
    assert len(tail["messages"][0]["text"]) == 4003  # capped at 4000 chars + "..."


# ---------------------------------------------------------------------------------------- Python client


def test_python_bridge_client_speaks_to_the_plugin(server):
    sys.path.insert(0, str(REPO / "src"))
    from astra.bridge import Bridge
    from astra.errors import BridgeError

    client = Bridge(f"http://127.0.0.1:{server}", timeout=10)
    data = client.post("/test/typed", {"num": 80000.0, "flag": True, "int": 3, "choice": "beta", "str": "默认"})
    assert data["num"] == 80000 and data["flag"] is True and data["str"] == "默认"
    with pytest.raises(BridgeError, match="Parameter 'num' must be a number"):
        client.post("/test/typed", {"num": "fast"})


def test_no_static_gameevents_delegates():
    """KSP's EventData.Add throws on static delegates (a non-capturing anonymous method); that exception
    in the addon's Start() once kept the whole server from starting. Subscribe via instance methods."""
    import re

    src_dir = Path(__file__).resolve().parents[1]
    bad = []
    for cs in src_dir.glob("*.cs"):
        text = cs.read_text(encoding="utf-8")
        for m in re.finditer(r"GameEvents\.\w+\.Add\(\s*delegate", text):
            bad.append(f"{cs.name}:{text[:m.start()].count(chr(10)) + 1}")
    assert not bad, f"static GameEvents delegates: {bad}"


def test_startup_steps_are_isolated():
    text = (Path(__file__).resolve().parents[1] / "Bridge.cs").read_text(encoding="utf-8")
    start = text[text.index("public void Start()"):]
    assert 'Safely("EVA walker", EvaWalker.Install)' in start
    assert start.index("Safely(") < start.index("_server.Start(")
