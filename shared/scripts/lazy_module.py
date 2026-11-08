from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any


class _LazyModule:
    """Import a heavy module the first time one of its attributes is used."""

    def __init__(self, module_name: str) -> None:
        self._module_name = module_name
        self._module: ModuleType | None = None

    def _load(self) -> ModuleType:
        module = self._module
        if module is None:
            module = importlib.import_module(self._module_name)
            self._module = module
        return module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)
