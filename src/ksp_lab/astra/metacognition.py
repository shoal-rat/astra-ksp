"""ASTRA's METACOGNITION framework — safe, bounded self-correction.

The USER DIRECTIVE: *while executing, the agent may adjust its own calculations/rules based on guidelines
+ memory, iterating to complete the objective — if data isn't available, generate it; if a calculation is
flawed versus reality, investigate, run an experiment, and fix the component.*

This module makes that possible WITHOUT letting the agent rewrite its own source code blindly. The whole
design is "self-adjustment with a seatbelt":

  * **Discrepancy ledger** (``runs/metacognition.jsonl``): every time a prediction misses reality
    (predicted Δv vs the Δv the flight actually needed, predicted drag loss vs observed, ...) the gap is
    appended as one JSON line. This is the agent's episodic memory of where its model is wrong.

  * **Bounded calibration** (``propose_calibration``): from accumulated discrepancies for a quantity, the
    agent proposes a single correction (a multiplier or offset) on a *named tunable constant*. It is
    GUARDED — it needs ``MIN_SAMPLES`` consistent samples and CLAMPS the adjustment to a small band — and
    it only PROPOSES; it never edits anything by itself.

  * **File-based application** (``apply_calibration`` -> ``src/ksp_lab/data/calibrations.json``): an
    approved calibration is written to a dedicated data file that the rest of the lab can read as an
    *override*. The agent thus "adjusts calculations in files" — but the file is data, bounded, logged,
    and reversible (delete the key to revert). It NEVER rewrites ``design.py`` / a ``.cs`` / formula code.

  * **Missing-data hooks** (``needs_data`` / ``generate_missing_data``): the "if data isn't available,
    generate it" path. The agent asks whether a named dataset exists; if not, it runs a supplied generator
    and records that it did so.

  * **Experiment log** (``record_experiment``): the "run an experiment, compare to prediction, correct"
    loop. The live experiment is a FLIGHT: launch the rocket, read telemetry, feed the gap into
    ``record_discrepancy``, and let the next ``propose_calibration`` tighten the constant.

WHAT IS SAFE TO SELF-ADJUST vs WHAT STAYS HUMAN-REVIEWED (be honest):
  * SAFE (auto): numeric overrides on whitelisted, bounded tunable constants in ``calibrations.json``
    (e.g. an ascent drag-loss multiplier, a capture-Δv margin). Bounded, logged, reversible, off by one
    key delete.
  * NOT SAFE (proposal only / human or higher-authority review): anything not on the whitelist, any
    adjustment outside the clamp band, structural/code changes, mission-graph or staging logic. The
    framework refuses to apply those — it records the proposal and stops.
"""
from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ----------------------------------------------------------------------------------------------------
# Calibration policy: what may be auto-tuned, by how much, and with how much evidence.
# ----------------------------------------------------------------------------------------------------
# Minimum number of CONSISTENT discrepancy samples before a calibration may be proposed at all.
MIN_SAMPLES = 3

# Whitelisted tunable constants the agent is allowed to override, each with a kind and a clamp band.
#   kind="multiplier" -> override = clamp(mean(observed/predicted), lo, hi)   (centred on 1.0)
#   kind="offset"     -> override = clamp(mean(observed-predicted), lo, hi)   (centred on 0.0)
# Anything NOT in this table is proposal-only and will NOT be written to calibrations.json.
TUNABLE_CONSTANTS: dict[str, dict] = {
    "ascent_drag_loss_mult": {"kind": "multiplier", "lo": 0.70, "hi": 1.40,
                              "doc": "scales the modeled ascent aerodynamic Δv loss"},
    "capture_dv_margin_mult": {"kind": "multiplier", "lo": 0.90, "hi": 1.30,
                               "doc": "safety margin multiplier on planned capture Δv"},
    "insertion_dv_margin_mult": {"kind": "multiplier", "lo": 0.90, "hi": 1.30,
                                 "doc": "safety margin multiplier on orbital insertion Δv"},
}

# A calibration is only proposed when the samples AGREE: their coefficient of variation must be below this
# (a noisy, contradictory set of gaps should not move a constant).
MAX_REL_SPREAD = 0.35


@dataclass(slots=True)
class Discrepancy:
    quantity: str
    predicted: float
    observed: float
    source: str
    ts: float = field(default_factory=time.time)

    @property
    def ratio(self) -> float:
        return self.observed / self.predicted if self.predicted else float("nan")

    @property
    def delta(self) -> float:
        return self.observed - self.predicted

    def to_dict(self) -> dict:
        return {"kind": "discrepancy", "quantity": self.quantity, "predicted": self.predicted,
                "observed": self.observed, "source": self.source, "ts": self.ts}


@dataclass(slots=True)
class Calibration:
    """A PROPOSED (or applied) override of a tunable constant. ``applicable`` is False when the constant
    is not whitelisted or the adjustment fell outside the clamp band / sample agreement test — in that
    case it stays a proposal for human review and is never written to the data file."""

    quantity: str
    constant: str
    kind: str            # "multiplier" | "offset"
    value: float         # the clamped override value actually proposed
    raw_value: float     # the unclamped central estimate (for the audit trail)
    n_samples: int
    applicable: bool
    reason: str = ""

    def to_dict(self) -> dict:
        return {"kind": "calibration", "quantity": self.quantity, "constant": self.constant,
                "calib_kind": self.kind, "value": self.value, "raw_value": self.raw_value,
                "n_samples": self.n_samples, "applicable": self.applicable, "reason": self.reason}


@dataclass(slots=True)
class Experiment:
    name: str
    predicted: dict
    observed: dict
    note: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"kind": "experiment", "name": self.name, "predicted": self.predicted,
                "observed": self.observed, "note": self.note, "ts": self.ts}


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class Metacognition:
    """The self-correction engine. Construct with a memory dir (where the JSONL ledger lives) and the path
    to the calibrations data file the lab reads as overrides.

    Defaults follow the project layout: ``runs/metacognition.jsonl`` and
    ``src/ksp_lab/data/calibrations.json``."""

    def __init__(
        self,
        memory_dir: str | Path = "runs",
        rules_path: str | Path | None = None,
        *,
        calibrations_path: str | Path | None = None,
    ):
        self.memory_dir = Path(memory_dir)
        self.ledger_path = self.memory_dir / "metacognition.jsonl"
        # ``rules_path`` is the (optional) operational-rules file the agent reasons against; kept as a
        # reference so a future rule tweak is logged the same bounded way. Not rewritten by this module.
        self.rules_path = Path(rules_path) if rules_path else None
        if calibrations_path is not None:
            self.calibrations_path = Path(calibrations_path)
        else:
            # src/ksp_lab/data/calibrations.json — sibling of stock_parts.json (parts.py DATA_DIR).
            self.calibrations_path = Path(__file__).resolve().parents[1] / "data" / "calibrations.json"
        # In-memory experiment log (also persisted to the ledger).
        self.experiment_log: list[Experiment] = []

    # ----------------------------------------------------------------------------------------------
    # Ledger I/O
    # ----------------------------------------------------------------------------------------------
    def _append(self, record: dict) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _read_records(self) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        out: list[dict] = []
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def record_discrepancy(self, quantity: str, predicted: float, observed: float, source: str) -> Discrepancy:
        """Record one prediction-vs-reality gap and append it to the ledger. ``quantity`` names what was
        predicted (e.g. ``ascent_drag_loss``), ``source`` is where the observation came from (a flight id,
        a telemetry stream)."""
        d = Discrepancy(quantity=str(quantity), predicted=float(predicted),
                        observed=float(observed), source=str(source))
        self._append(d.to_dict())
        return d

    def discrepancies(self, quantity: str | None = None) -> list[Discrepancy]:
        """All recorded discrepancies (optionally filtered to one quantity), oldest first."""
        out: list[Discrepancy] = []
        for r in self._read_records():
            if r.get("kind") != "discrepancy":
                continue
            if quantity is not None and r.get("quantity") != quantity:
                continue
            out.append(Discrepancy(quantity=r["quantity"], predicted=r["predicted"],
                                   observed=r["observed"], source=r.get("source", ""),
                                   ts=r.get("ts", 0.0)))
        return out

    # ----------------------------------------------------------------------------------------------
    # Bounded calibration
    # ----------------------------------------------------------------------------------------------
    def propose_calibration(self, quantity: str) -> Calibration | None:
        """From the accumulated discrepancies for ``quantity``, propose a bounded correction to its mapped
        tunable constant. GUARDED:

          * needs >= ``MIN_SAMPLES`` samples (else returns None),
          * the samples must AGREE (relative spread < ``MAX_REL_SPREAD``) else ``applicable=False``,
          * the central estimate is CLAMPED to the constant's band; a clamp means the true correction is
            larger than we will auto-apply, so it is flagged ``applicable=False`` for human review,
          * a quantity with no whitelisted constant returns a proposal-only Calibration (``applicable``
            False) — never silently applied.

        Returns the :class:`Calibration` (proposal) or None when there is not enough evidence to say
        anything at all."""
        samples = self.discrepancies(quantity)
        if len(samples) < MIN_SAMPLES:
            return None

        spec = TUNABLE_CONSTANTS.get(quantity)
        # Determine the constant + kind. If the quantity is not whitelisted, we still compute a multiplier
        # estimate but mark the proposal non-applicable (human review only).
        constant = quantity if spec else f"{quantity} (unmapped)"
        kind = spec["kind"] if spec else "multiplier"

        if kind == "offset":
            estimates = [s.delta for s in samples]
            centre = 0.0
        else:
            estimates = [s.ratio for s in samples if s.predicted]
            centre = 1.0
        estimates = [e for e in estimates if e == e]  # drop NaN
        if not estimates:
            return None

        raw = statistics.fmean(estimates)
        # Sample-agreement test: relative spread around the centre-anchored mean.
        spread = (statistics.pstdev(estimates) / abs(raw)) if len(estimates) > 1 and raw else 0.0

        if not spec:
            return Calibration(quantity=quantity, constant=constant, kind=kind, value=raw, raw_value=raw,
                               n_samples=len(samples), applicable=False,
                               reason="quantity has no whitelisted tunable constant; proposal only")

        clamped = _clamp(raw, spec["lo"], spec["hi"])
        was_clamped = abs(clamped - raw) > 1e-9
        agrees = spread < MAX_REL_SPREAD

        applicable = agrees and not was_clamped
        reasons = []
        if not agrees:
            reasons.append(f"samples disagree (rel spread {spread:.2f} >= {MAX_REL_SPREAD})")
        if was_clamped:
            reasons.append(f"estimate {raw:.3f} fell outside band [{spec['lo']}, {spec['hi']}]; "
                           "clamped, needs human review")
        reason = "; ".join(reasons) if reasons else "within band and samples agree"

        return Calibration(quantity=quantity, constant=constant, kind=kind, value=clamped, raw_value=raw,
                           n_samples=len(samples), applicable=applicable, reason=reason)

    def apply_calibration(self, calibration: Calibration) -> bool:
        """Write an APPLICABLE calibration to ``calibrations.json`` (the data-file override the lab reads).

        Refuses to write a non-applicable proposal (returns False) — that path stays human-reviewed. On
        success the override is stored under the constant name with its metadata, the action is logged to
        the ledger, and the change is fully reversible (delete the key / call ``revert_calibration``).

        Never rewrites code: the only side effect is one JSON data file."""
        if not calibration.applicable:
            self._append({"kind": "calibration_refused", **calibration.to_dict(), "ts": time.time()})
            return False

        data = self.read_calibrations()
        data[calibration.constant] = {
            "kind": calibration.kind,
            "value": calibration.value,
            "raw_value": calibration.raw_value,
            "n_samples": calibration.n_samples,
            "quantity": calibration.quantity,
            "applied_ts": time.time(),
            "reason": calibration.reason,
        }
        self._write_calibrations(data)
        self._append({"kind": "calibration_applied", **calibration.to_dict(), "ts": time.time()})
        return True

    def read_calibrations(self) -> dict:
        """The current override table (empty dict if the file does not exist / is unreadable)."""
        if not self.calibrations_path.exists():
            return {}
        try:
            return json.loads(self.calibrations_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_calibrations(self, data: dict) -> None:
        self.calibrations_path.parent.mkdir(parents=True, exist_ok=True)
        self.calibrations_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def get_override(self, constant: str, default: float | None = None) -> float | None:
        """Read a single calibrated override value the rest of the lab can apply to its calculation."""
        entry = self.read_calibrations().get(constant)
        if isinstance(entry, dict) and "value" in entry:
            try:
                return float(entry["value"])
            except (TypeError, ValueError):
                return default
        return default

    def revert_calibration(self, constant: str) -> bool:
        """Remove an override (the reversibility guarantee). Returns True if a key was removed."""
        data = self.read_calibrations()
        if constant in data:
            del data[constant]
            self._write_calibrations(data)
            self._append({"kind": "calibration_reverted", "constant": constant, "ts": time.time()})
            return True
        return False

    # ----------------------------------------------------------------------------------------------
    # Missing-data generation
    # ----------------------------------------------------------------------------------------------
    def needs_data(self, name: str, exists_fn: Callable[[], bool]) -> bool:
        """Return True when a named dataset is NOT available yet (``exists_fn()`` is falsy). The
        "if data isn't available..." predicate."""
        try:
            return not bool(exists_fn())
        except Exception:
            # If the existence check itself blows up, treat the data as missing so we try to generate it.
            return True

    def generate_missing_data(self, name: str, gen_fn: Callable[[], object]) -> object:
        """"...generate it." Run ``gen_fn`` to produce the dataset, log the generation to the ledger, and
        return whatever the generator produced. The generator owns HOW the data is made (a sweep, a query,
        a synthetic fill); this just records that the agent had to make it."""
        result = gen_fn()
        self._append({"kind": "data_generated", "name": str(name), "ts": time.time()})
        return result

    # ----------------------------------------------------------------------------------------------
    # Experiments (the live "fly it and compare" loop)
    # ----------------------------------------------------------------------------------------------
    def record_experiment(self, name: str, predicted: dict, observed: dict, note: str = "") -> Experiment:
        """Record one experiment outcome (predicted vs observed dicts) and persist it.

        The canonical experiment is a FLIGHT: ``predicted`` is what the design model expected (Δv budget,
        drag loss, apoapsis), ``observed`` is the flight telemetry. Pair this with ``record_discrepancy``
        per quantity so the next ``propose_calibration`` can tighten the constant from real data."""
        exp = Experiment(name=str(name), predicted=dict(predicted), observed=dict(observed), note=str(note))
        self.experiment_log.append(exp)
        self._append(exp.to_dict())
        return exp
