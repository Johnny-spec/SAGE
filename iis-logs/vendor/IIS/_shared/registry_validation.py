"""Compatibility wrapper for the cross-product registry closure validator."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMMON_ROOT = ROOT.parent / "Common"
if str(COMMON_ROOT) not in sys.path:
    sys.path.insert(0, str(COMMON_ROOT))

from registry_validation import validate_registry as _validate_registry  # noqa: E402


def validate_registry(
    registry_path: Path | None = None,
    playbook_path: Path | None = None,
) -> list[str]:
    return _validate_registry(
        (registry_path or ROOT / "_shared" / "registry.json").resolve(),
        (playbook_path or ROOT / "_shared" / "playbook.json").resolve(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate IIS skill registry closure")
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--playbook", type=Path)
    args = parser.parse_args(argv)
    errors = validate_registry(args.registry, args.playbook)
    print(json.dumps({"ok": not errors, "errors": errors}, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
