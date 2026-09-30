"""Offline tests for launcher helpers (no game, no files outside tmp_path)."""

from pathlib import Path

import pytest

from astra import launcher

SFS = """GAME
{
\tversion = 1.12.5
\tFLIGHTSTATE
\t{
\t\tversion = 1.12.5
\t\tUT = 12345.6
\t\tactiveVessel = 3
\t\tVESSEL
\t\t{
\t\t\tname = Stray 1
\t\t\tPART
\t\t\t{
\t\t\t\tname = mk1pod.v2
\t\t\t}
\t\t}
\t\tVESSEL
\t\t{
\t\t\tname = Stray 2
\t\t}
\t}
\tROSTER
\t{
\t\tKERBAL
\t\t{
\t\t\tname = Jebediah Kerman
\t\t\tstate = Assigned
\t\t}
\t}
}
"""


def test_strip_vessels_keeps_settings_and_frees_crew():
    text, removed = launcher._strip_vessels(SFS)
    assert removed == 2
    assert "VESSEL" not in text and "Stray" not in text
    assert "UT = 12345.6" in text and "ROSTER" in text
    assert "state = Available" in text and "state = Assigned" not in text
    assert "activeVessel = -1" in text
    assert text.count("{") == text.count("}")


def test_new_save_refuses_bad_names_and_existing(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    (saves / "src").mkdir(parents=True)
    (saves / "src" / "persistent.sfs").write_text(SFS, encoding="utf-8")
    monkeypatch.setattr(type(launcher.CONFIG), "saves_dir", property(lambda self: saves))
    assert launcher.new_save("../evil", "src") == 2
    assert launcher.new_save("clean", "missing") == 2
    assert launcher.new_save("clean", "src") == 0
    assert (saves / "clean" / "Ships" / "VAB").is_dir()
    assert "VESSEL" not in (saves / "clean" / "persistent.sfs").read_text(encoding="utf-8")
    assert launcher.new_save("clean", "src") == 2  # already exists


@pytest.mark.skipif(not (launcher.CONFIG.saves_dir / "默认" / "persistent.sfs").exists(),
                    reason="needs the local default save (read-only check)")
def test_strip_real_save_read_only():
    text = (launcher.CONFIG.saves_dir / "默认" / "persistent.sfs").read_text(encoding="utf-8")
    out, removed = launcher._strip_vessels(text)
    assert removed > 0
    assert "\n\t\tVESSEL\n" not in out
    assert out.count("{") == out.count("}")
