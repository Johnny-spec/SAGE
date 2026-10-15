"""Publish the offline SZYD Solution into an existing local development catalog."""

import argparse
import io
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

import httpx

from sage.digests import canonical_digest, directory_digest
from sage.reference_solutions.szyd_compliance import szyd_agent_payload
from sage.solution_packages import load_solution_bundle

ROOT = Path(__file__).resolve().parents[1]
ADMIN = "/v1/admin"


def archive(root: Path, *, skill: bool = False) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as package:
        for path in sorted(root.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                relative = path.relative_to(root)
                package.writestr(
                    (Path(root.name) / relative if skill else relative).as_posix(),
                    path.read_bytes(),
                )
    return output.getvalue()


def publish(client, *, activate: bool = False) -> dict:
    def request(method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    if request("GET", "/v1/control/bootstrap").get("environment") != "development":
        raise ValueError("This helper only publishes to a development control plane")
    if request("GET", "/health/ready").get("runner") != "ready":
        raise ValueError("Configure an isolated Runner before publishing uploaded Python Skills")
    manifest = load_solution_bundle(ROOT / "solutions/szyd-compliance")
    locks = {
        component.component_id: component.content_digest for component in manifest.spec.components
    }
    definition = szyd_agent_payload()
    skill_digest = directory_digest(ROOT / "skills/szyd-law")
    if locks != {"szyd-law": skill_digest, "szyd-agent": canonical_digest(definition)}:
        raise ValueError(
            "Repository Solution locks differ from current content; update the release"
        )
    skills = [
        item
        for item in request("GET", ADMIN + "/skills")
        if item["name"] == "szyd-law" and item["version"] == "1.0.0"
    ]
    if any(item["content_digest"] != skill_digest for item in skills):
        raise ValueError("Published/imported Skill version has different content")
    skill = (
        skills[0]
        if skills
        else request(
            "POST",
            ADMIN + "/skills/uploads",
            files={
                "file": (
                    "szyd-law.zip",
                    archive(ROOT / "skills/szyd-law", skill=True),
                    "application/zip",
                ),
            },
        )
    )
    if skill["status"] != "published":
        skill = request("POST", f"{ADMIN}/skills/{skill['package_id']}/validate")
    if skill["status"] not in {"valid", "published"} or not skill["executable"]:
        raise ValueError("SZYD Skill is not valid and executable")
    agents = [
        item
        for item in request("GET", ADMIN + "/agents")
        if item["definition"]["agent_id"] == "szyd-agent"
        and item["definition"]["version"] == "1.0.0"
    ]
    if any(canonical_digest(item["definition"]) != locks["szyd-agent"] for item in agents):
        raise ValueError("Published/imported Agent version has different content")
    agent = agents[0] if agents else request("POST", ADMIN + "/agents", json=definition)
    if agent["status"] != "published":
        agent = request("POST", f"{ADMIN}/agents/{agent['draft_id']}/validate")
    if agent["status"] not in {"valid", "published"}:
        raise ValueError("SZYD Agent validation failed")
    release_id = "szyd-compliance@1.0.0"
    releases = [
        item
        for item in request("GET", ADMIN + "/solution-releases")
        if item["release_id"] == release_id
    ]
    if releases:
        release = releases[0]
        actual = {item["component_id"]: item["content_digest"] for item in release["components"]}
        if (
            actual != locks
            or release["entry_agent_id"] != "szyd-agent"
            or release["supported_profiles"] != ["interactive"]
            or release["allowed_roles"] != ["user"]
        ):
            raise ValueError("Existing immutable Solution release differs from repository content")
    else:
        request(
            "POST",
            ADMIN + "/solutions/imports",
            files={
                "file": (
                    "szyd-compliance.zip",
                    archive(ROOT / "solutions/szyd-compliance"),
                    "application/zip",
                ),
            },
        )
    if any(item["release_id"] == release_id for item in request("GET", "/v1/solutions")):
        return {"status": "already-active", "release_id": release_id}
    candidate = request(
        "POST",
        ADMIN + "/candidates",
        json={
            "title": "Publish SZYD compliance Solution",
            "skill_package_ids": [skill["package_id"]],
            "agent_draft_ids": [] if agent["status"] == "published" else [agent["draft_id"]],
            "solution_release_ids": [release_id],
        },
    )
    path = f"{ADMIN}/candidates/{candidate['candidate_id']}"
    validated = request("POST", path + "/validate")
    if validated["status"] != "valid":
        raise ValueError(f"Candidate validation failed: {validated['validation']['issues']}")
    if not activate:
        return {"status": "awaiting-approval", "candidate_id": candidate["candidate_id"]}
    approval = request("POST", path + "/submit")
    request(
        "POST",
        f"{ADMIN}/approvals/{approval['approval_id']}/approve",
        json={
            "reason": "Reviewed offline SZYD package and component locks for local publication",
        },
    )
    generation = request("POST", path + "/activate")
    return {
        "status": "active",
        "release_id": release_id,
        "generation_id": generation["generation_id"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8014")
    parser.add_argument(
        "--activate",
        action="store_true",
        help="Explicitly approve and activate in local development",
    )
    arguments = parser.parse_args()
    parsed = urlsplit(arguments.url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        parser.error("Use a credential-free local HTTP origin")
    token = os.environ.get("SAGE_ADMIN_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=arguments.url, headers=headers, timeout=60) as client:
        print(json.dumps(publish(client, activate=arguments.activate), indent=2))


if __name__ == "__main__":
    main()
