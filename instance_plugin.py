"""Generic instance-plugin contract and filesystem loader."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class TurnContext:
    text: str
    source: str
    user_id: str
    request_id: str | None

    @property
    def is_internal(self) -> bool:
        return self.source == "reflection"


class InstancePlugin(Protocol):
    def system_prompt_context(self) -> str: ...

    def on_turn_received(self, turn: TurnContext) -> None: ...

    def context_for_turn(self, turn: TurnContext) -> str: ...

    def transform_response(self, turn: TurnContext, response_text: str) -> str: ...

    def on_turn_completed(self, turn: TurnContext, clean_response_text: str) -> None: ...


_REQUIRED_METHODS = (
    "system_prompt_context",
    "on_turn_received",
    "context_for_turn",
    "transform_response",
    "on_turn_completed",
)


def _load_instance_plugin(path: str) -> InstancePlugin | None:
    """Load one configured plugin entry point or return None when unset.

    A configured plugin is mandatory: path, import, factory, and interface
    errors deliberately propagate instead of silently disabling the plugin.
    """
    if not path or not path.strip():
        return None

    entry_path = Path(os.path.abspath(os.path.expanduser(path)))
    if entry_path.suffix != ".py":
        raise ValueError(f"Instance plugin must be a .py file: {entry_path}")
    if not entry_path.exists():
        raise FileNotFoundError(f"Instance plugin does not exist: {entry_path}")
    if not entry_path.is_file():
        raise ValueError(f"Instance plugin is not a regular file: {entry_path}")

    namespace_hash = hashlib.sha256(str(entry_path).encode()).hexdigest()[:16]
    package_name = f"_axon_instance_extension_{namespace_hash}"
    module_name = f"{package_name}.entrypoint"

    package = types.ModuleType(package_name)
    package.__path__ = [str(entry_path.parent)]
    package.__package__ = package_name
    sys.modules[package_name] = package

    try:
        spec = importlib.util.spec_from_file_location(module_name, entry_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not create import spec for instance plugin: {entry_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        factory = getattr(module, "create_plugin", None)
        if not callable(factory):
            raise TypeError(f"Instance plugin must export callable create_plugin(): {entry_path}")
        plugin = factory()

        missing = [name for name in _REQUIRED_METHODS if not callable(getattr(plugin, name, None))]
        if missing:
            raise TypeError(
                "Instance plugin is missing callable method(s): " + ", ".join(missing)
            )
        return plugin
    except Exception:
        for loaded_name in tuple(sys.modules):
            if loaded_name == package_name or loaded_name.startswith(f"{package_name}."):
                sys.modules.pop(loaded_name, None)
        raise
