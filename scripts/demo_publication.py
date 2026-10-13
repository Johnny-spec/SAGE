"""Create and verify publication demos in an empty, local development control plane."""

import argparse
import asyncio
import hashlib
import io
import json
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import httpx
from temporalio.client import Client

ADMIN = "/v1/admin"
OWNER = "publication-demo"


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def local_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Demo URLs must be credential-free local HTTP URLs")
    return value.rstrip("/")


class Demo:
    def __init__(self, client: httpx.Client, manifest: Path, url: str, mcp_url: str):
        self.client = client
        self.manifest = manifest
        self.url = local_url(url)
        self.mcp_url = local_url(mcp_url)
        self.data = (
            json.loads(manifest.read_text(encoding="utf-8"))
            if manifest.exists()
            else {
                "url": self.url,
                "mcp_url": self.mcp_url,
                "steps": {},
            }
        )
        if (self.data["url"], self.data["mcp_url"]) != (self.url, self.mcp_url):
            raise ValueError("Manifest belongs to a different demo environment")

    def get(self, path: str):
        response = self.client.get(path)
        response.raise_for_status()
        return response.json()

    def save(self) -> None:
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        temporary.replace(self.manifest)

    def preflight(self) -> None:
        if self.get("/v1/control/bootstrap")["environment"] != "development":
            raise ValueError("Refusing to seed a production control plane")
        status = self.get(ADMIN + "/status")
        generations = self.get(ADMIN + "/generations")
        if not self.manifest.exists():
            collections = (
                "solutions",
                "solution-releases",
                "skills",
                "mcp/servers",
                "tools",
                "candidates",
                "approvals",
            )
            if len(generations) != 1 or any(self.get(f"{ADMIN}/{name}") for name in collections):
                raise ValueError("Refusing non-empty state; start a fresh isolated DataRoot")
            agents = self.get(ADMIN + "/agents")
            if any(item["definition"]["agent_id"] != "diagnostic-agent" for item in agents):
                raise ValueError("Refusing an environment containing user Agents")
            self.data["initial_generation"] = status["active_generation_id"]
            self.save()
        known = {self.data["initial_generation"]}
        known.update(
            item["result"]["generation_id"]
            for item in self.data["steps"].values()
            if isinstance(item["result"], dict) and item["result"].get("generation_id")
        )
        if status["active_generation_id"] not in known:
            raise ValueError(
                "Active Generation changed outside this manifest; inspect before resuming"
            )
        if {item["generation_id"] for item in generations} - known:
            raise ValueError("Untracked Generations found; refusing to overwrite external changes")

    def step(self, key: str, path: str, body=None, *, method="POST", headers=None, files=None):
        signature = digest(
            {
                "path": path,
                "method": method,
                "body": body,
                "headers": headers,
                "files": {name: digest(list(value[1])) for name, value in (files or {}).items()},
            }
        )
        previous = self.data["steps"].get(key)
        if previous:
            if previous["signature"] != signature:
                raise ValueError(f"Demo step changed: {key}; use a new isolated environment")
            return previous["result"]
        response = self.client.request(method, path, json=body, headers=headers, files=files)
        response.raise_for_status()
        result = response.json()
        self.data["steps"][key] = {"signature": signature, "result": result}
        self.save()
        print(f"Completed: {key}", flush=True)
        return result

    def publish(self, key: str, candidate: dict, *, approval=False):
        path = f"{ADMIN}/candidates/{candidate['candidate_id']}"
        if approval:
            submitted = self.step(key + "-submit", path + "/submit")
            self.step(
                key + "-approve",
                f"{ADMIN}/approvals/{submitted['approval_id']}/approve",
                {"reason": "Reviewed deterministic local demo inputs and dependency locks"},
            )
        return self.step(key + "-activate", path + ("/activate" if approval else "/publish"))


def disabled_package() -> bytes:
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        files = {
            "draft/SKILL.md": "---\nname: draft\ndescription: Inert upload validation demo.\n---\n",
            "draft/skill.yaml": """apiVersion: sage.dev/v1alpha1
kind: SkillBinding
metadata: {name: draft, version: 1.0.0}
spec:
  execution: {type: python-subprocess, entrypoint: main.py}
  inputSchema: {type: object}
  outputSchema: {type: object}
""",
            "draft/main.py": "print('{}')\n",
        }
        for name, content in files.items():
            info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            archive.writestr(info, content)
    return buffer.getvalue()


def workflow_definition(tool_id: str, discount: int) -> dict:
    arguments = {"unit_price": 12, "quantity": 3, "discount_percent": discount}
    return {
        "apiVersion": "sage.dev/v1beta1",
        "kind": "WorkflowTemplate",
        "workflowId": "flow",
        "version": "1.0.0",
        "steps": [
            {
                "id": "echo",
                "kind": "skill",
                "target": "echo",
                "inheritInput": False,
                "input": {"text": "Published Flow"},
            },
            {
                "id": "native",
                "kind": "tool",
                "target": tool_id,
                "dependsOn": ["echo"],
                "inheritInput": False,
                "input": arguments,
            },
            {
                "id": "skill",
                "kind": "skill",
                "target": "calc",
                "dependsOn": ["native"],
                "inheritInput": False,
                "input": arguments,
            },
        ],
    }


def total(output: dict) -> float:
    if "total" in output:
        return output["total"]
    for key in ("structuredContent", "structured_content"):
        if isinstance(output.get(key), dict) and "total" in output[key]:
            return output[key]["total"]
    raise AssertionError(f"Missing structured calculation result: {output}")


async def workflow_result(target: str, workflow_id: str) -> dict:
    client = await Client.connect(target)
    return await asyncio.wait_for(client.get_workflow_handle(workflow_id).result(), timeout=120)


def run_demo(demo: Demo, temporal_target: str) -> dict:
    demo.preflight()
    server = demo.step(
        "mcp-create",
        ADMIN + "/mcp/servers",
        {
            "alias": "calc",
            "transport": "streamable-http",
            "url": demo.mcp_url,
        },
    )
    server_path = f"{ADMIN}/mcp/servers/{server['server_id']}"
    live_server = next(
        item
        for item in demo.get(ADMIN + "/mcp/servers")
        if item["server_id"] == server["server_id"]
    )
    if live_server["config_digest"] != server["config_digest"]:
        raise ValueError("Demo MCP configuration changed; inspect before resuming")
    demo.step("mcp-test", server_path + "/test")
    demo.step("mcp-discover", server_path + "/discover")
    tool = demo.step(
        "native-register",
        server_path + "/tool-registrations",
        {
            "tool_name": "calculate_total",
            "version": "1.0.0",
            "description": "Calculate an order total",
            "risk": "read-only",
        },
    )
    skill = demo.step(
        "skill-import",
        server_path + "/tools/calculate_total/import",
        {
            "skill_name": "calc",
            "version": "1.0.0",
            "description": "Calculate an order total",
            "instructions": "Calculate a total using the MCP tool.",
            "risk": "read-only",
        },
    )
    agent = demo.step(
        "agent-create",
        ADMIN + "/agents",
        {
            "agent_id": "echo",
            "name": "Echo",
            "version": "1.0.0",
            "instructions": "Use the echo Skill to repeat the user's message.",
            "capability_ids": ["skill:echo"],
            "memory_policy": "none",
        },
    )
    agent = demo.step("agent-validate", f"{ADMIN}/agents/{agent['draft_id']}/validate")
    assert agent["status"] == "valid", agent
    candidate = demo.step(
        "capabilities-candidate",
        ADMIN + "/candidates",
        {
            "title": "Demo capabilities",
            "agent_draft_ids": [agent["draft_id"]],
            "skill_package_ids": [skill["package_id"]],
            "tool_registration_ids": [tool["registration_id"]],
        },
    )
    generation = demo.publish("capabilities", candidate)
    releases = []
    locks = {
        "calc": [
            {
                "kind": "mcp-tool",
                "component_id": tool["tool_id"],
                "version": "1.0.0",
                "content_digest": tool["content_digest"],
            },
            {
                "kind": "skill",
                "component_id": "calc",
                "version": "1.0.0",
                "content_digest": generation["skill_digests"]["calc"],
            },
        ],
        "echo": [
            {
                "kind": "agent",
                "component_id": "echo",
                "version": "1.0.0",
                "content_digest": agent["validation"]["content_digest"],
            },
            {
                "kind": "skill",
                "component_id": "echo",
                "version": generation["skill_versions"]["echo"],
                "content_digest": generation["skill_digests"]["echo"],
            },
        ],
    }
    for name in ("calc", "echo"):
        demo.step(
            name + "-solution",
            ADMIN + "/solutions",
            {
                "solution_id": name,
                "title": name.title(),
                "owner": OWNER,
                "entry_agent_id": "echo" if name == "echo" else None,
                "supported_profiles": ["direct", "interactive"],
            },
        )
        release = demo.step(
            name + "-release",
            f"{ADMIN}/solutions/{name}/releases",
            {
                "version": "1.0.0",
                "components": locks[name],
                "source_generation_id": generation["generation_id"],
            },
        )
        releases.append(release["release_id"])
    candidate = demo.step(
        "scopes-candidate",
        ADMIN + "/candidates",
        {
            "title": "Demo Calc and Echo",
            "solution_release_ids": releases,
        },
    )
    demo.publish("scopes", candidate)
    for name, capability in (("native", tool["tool_id"]), ("skill", "skill:calc")):
        result = demo.step(
            name + "-invoke",
            "/v1/invocations",
            {
                "solution_id": "calc",
                "capability_id": capability,
                "input": {"unit_price": 12, "quantity": 3, "discount_percent": 10},
                "idempotency_key": "publication-demo-" + name,
            },
        )
        assert result["status"] == "completed" and total(result["output"]) == 32.4, result
    echoed = demo.step(
        "agent-invoke",
        "/v1/messages",
        {
            "solution_id": "echo",
            "mode": "agent",
            "agent_id": "echo",
            "message": "echo publication demo",
        },
    )
    assert echoed["status"] == "completed" and echoed["usage"]["tool_calls"] > 0, echoed
    assert "echo publication demo" in echoed["message"], echoed
    demo.step(
        "flow-solution",
        ADMIN + "/solutions",
        {
            "solution_id": "flow",
            "title": "Flow",
            "owner": OWNER,
            "supported_profiles": ["durable"],
        },
    )
    path = ""
    draft = None
    results = {}
    for version, discount, expected in (("1.0.0", 10, 32.4), ("1.1.0", 20, 28.8)):
        body = {
            "title": "Flow",
            "solution_id": "flow",
            "definition": workflow_definition(tool["tool_id"], discount),
        }
        if draft is None:
            draft = demo.step("workflow-create", ADMIN + "/workflow-drafts", body)
            path = f"{ADMIN}/workflow-drafts/{draft['draft_id']}"
        else:
            draft = demo.step(
                "workflow-update",
                path,
                body,
                method="PUT",
                headers={"If-Match": f'"{draft["revision"]}"'},
            )
        headers = {"If-Match": f'"{draft["revision"]}"'}
        preview = demo.step(
            version + "-resolve", path + "/resolve", {"version": version}, headers=headers
        )
        built = demo.step(
            version + "-build",
            path + "/build",
            {
                "version": version,
                "build_digest": preview["build_digest"],
            },
            headers=headers,
        )
        demo.publish(version, built["candidate"], approval=version == "1.1.0")
        run = demo.step(
            version + "-run",
            "/v1/durable-runs",
            {
                "solution_id": "flow",
                "workflow_type": "flow",
                "payload": {},
                "idempotency_key": "publication-demo-flow-" + version,
                "execution_timeout_seconds": 120,
            },
        )
        result = asyncio.run(workflow_result(temporal_target, run["workflow_id"]))
        assert result["status"] == "completed", result
        for name in ("native", "skill"):
            assert total(result["results"][name]["output"]) == expected, result
        assert run["solution_release_id"] == "flow@" + version, run
        demo.data.setdefault("workflow_results", {})[version] = result
        demo.save()
        results[version] = {"run_id": run["run_id"], "total": expected}
    package = demo.step(
        "disabled-upload",
        ADMIN + "/skills/uploads",
        files={"file": ("draft.zip", disabled_package(), "application/zip")},
    )
    checked = demo.step("disabled-validate", f"{ADMIN}/skills/{package['package_id']}/validate")
    assert checked["status"] == "valid" and not checked["executable"], checked
    assert any(
        issue["code"] == "uploaded_code_execution_disabled"
        for issue in checked["validation"]["issues"]
    ), checked
    status = demo.get(ADMIN + "/status")
    active = next(
        item
        for item in demo.get(ADMIN + "/generations")
        if item["generation_id"] == status["active_generation_id"]
    )
    assert active["solution_releases"] == {
        "calc": "calc@1.0.0",
        "echo": "echo@1.0.0",
        "flow": "flow@1.1.0",
    }, active["solution_releases"]
    summary = {
        "url": demo.url + "/admin",
        "generation_id": active["generation_id"],
        "solutions": active["solution_releases"],
        "workflow_runs": results,
        "tool_id": tool["tool_id"],
        "workflow_draft_id": draft["draft_id"],
    }
    demo.data["summary"] = summary
    demo.save()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--mcp-url", required=True)
    parser.add_argument("--temporal-target", default="127.0.0.1:7234")
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    with httpx.Client(base_url=local_url(args.url), timeout=150, trust_env=False) as client:
        demo = Demo(client, args.manifest, args.url, args.mcp_url)
        print(json.dumps(run_demo(demo, args.temporal_target), indent=2))


if __name__ == "__main__":
    main()
