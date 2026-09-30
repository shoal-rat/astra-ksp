"""Tool modules. Importing this package registers every tool with astra.registry."""

import importlib
import warnings

MODULES = ("observe", "compute", "design", "game", "control", "fly", "autopilot", "crew", "logbook")

for _name in MODULES:
    try:
        importlib.import_module(f"astra.tools.{_name}")
    except ModuleNotFoundError as exc:
        if exc.name != f"astra.tools.{_name}":
            raise
        warnings.warn(f"astra tool module {_name!r} is missing", stacklevel=1)
