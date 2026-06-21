"""ASTRA — Autonomous Spaceflight Trial & Research Agent.

One line of natural language in, a flown mission out. ASTRA interprets a plain-English goal, designs
the rocket, flies it live in Kerbal Space Program 1 (via the ``ksp_lab`` kRPC flight core), diagnoses
failures against a growing experience ledger, and retries until the mission succeeds.

Public surface:
    from ksp_lab.astra import AstraAgent, ExperienceLedger, KnowledgeBase, Interpreter
    AstraAgent(config_path).run("land a relay in high Mun orbit")
"""
from __future__ import annotations

from .agent import AstraAgent, AstraResult
from .interpreter import Interpreter, MissionPlan
from .knowledge import KnowledgeBase
from .ledger import ExperienceLedger, LedgerEntry

__all__ = [
    "AstraAgent",
    "AstraResult",
    "Interpreter",
    "MissionPlan",
    "KnowledgeBase",
    "ExperienceLedger",
    "LedgerEntry",
]
