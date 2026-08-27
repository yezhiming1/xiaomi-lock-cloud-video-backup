"""Load integration modules without importing Home Assistant itself."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "custom_components" / "xiaomi_lock_cloud_backup"
PACKAGE = "xiaomi_lock_cloud_backup_under_test"


if PACKAGE not in sys.modules:
    package = ModuleType(PACKAGE)
    package.__path__ = [str(SOURCE)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE] = package


def load(name: str) -> ModuleType:
    qualified_name = f"{PACKAGE}.{name}"
    if qualified_name in sys.modules:
        return sys.modules[qualified_name]
    path = SOURCE / f"{name}.py"
    spec = importlib.util.spec_from_file_location(qualified_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load test module: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified_name] = module
    spec.loader.exec_module(module)
    return module
