import json
from pathlib import Path
from zipfile import ZipFile

from sage.cli import main


def test_validate_solution_cli_reports_manifest_summary(
    tmp_path: Path,
    capsys,
) -> None:
    digest = "sha256:" + "a" * 64
    manifest = tmp_path / "solution.yaml"
    manifest.write_text(
        f"""apiVersion: sage.dev/v1alpha1
kind: SolutionBundle
metadata: {{name: echo-solution, version: 1.0.0, title: Echo}}
spec:
  owner: platform-team
  supportedProfiles: [direct]
  components:
    - kind: skill
      id: echo
      version: 1.0.0
      contentDigest: {digest}
""",
        encoding="utf-8",
    )

    result = main(["validate-solution", str(tmp_path)])

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output == {
        "valid": True,
        "solution_id": "echo-solution",
        "version": "1.0.0",
        "components": 1,
    }


def test_package_solution_cli_creates_verified_zip(tmp_path: Path, capsys) -> None:
    digest = "sha256:" + "b" * 64
    (tmp_path / "solution.yaml").write_text(
        f"""apiVersion: sage.dev/v1alpha1
kind: SolutionBundle
metadata: {{name: echo-solution, version: 1.0.0, title: Echo}}
spec:
  owner: platform-team
  supportedProfiles: [direct]
  components:
    - kind: skill
      id: echo
      version: 1.0.0
      contentDigest: {digest}
""",
        encoding="utf-8",
    )
    output = tmp_path.parent / "echo-solution.zip"

    result = main(["package-solution", str(tmp_path), "--output", str(output)])

    payload = json.loads(capsys.readouterr().out)
    assert result == 0, payload
    assert payload["packaged"] is True
    with ZipFile(output) as archive:
        assert archive.namelist() == ["solution.yaml"]