from pathlib import Path

import pytest

from sage.registry import SkillPackageError, SkillRegistry

FIXTURES = Path(__file__).parent / "fixtures" / "skills"


def test_loads_valid_skill_package() -> None:
    registry = SkillRegistry.load(FIXTURES)

    definition = registry.get("echo")
    assert definition is not None
    assert definition.version == "1.0.0"
    assert definition.route_keywords == ("echo", "repeat", "回显", "重复")
    assert definition.manifest.spec.execution.handler == "echo"


def test_rejects_mismatched_directory_name(tmp_path: Path) -> None:
    package = tmp_path / "wrong-directory"
    package.mkdir()
    (package / "SKILL.md").write_text(
        "---\nname: echo\ndescription: Echo text.\n---\n",
        encoding="utf-8",
    )
    (package / "skill.yaml").write_text(
        (FIXTURES / "echo" / "skill.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    with pytest.raises(SkillPackageError, match="name must match parent directory"):
        SkillRegistry.load(tmp_path)
