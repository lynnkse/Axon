#!/usr/bin/env python3
"""Dependency-free runner for the actor tests (also pytest-compatible)."""
import importlib
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

failed = 0
total = 0
for module_name in ("tests.test_actor_math", "tests.test_actor_runtime", "tests.test_actor_worker",
                    "tests.test_actor_shadow"):
    module = importlib.import_module(module_name)
    for name, fn in inspect.getmembers(module, inspect.isfunction):
        if not name.startswith("test_"):
            continue
        try:
            total += 1
            fn()
            print(f"PASS {module_name}.{name}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {module_name}.{name}: {exc}")
print(f"{total-failed}/{total} passed")
sys.exit(bool(failed))
