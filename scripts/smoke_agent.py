"""Exercise the live Deep Agent and governed Skills through HTTP/SSE."""

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx

DEFAULT_LOG = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "iis" / "sample.log"


def parse_sse(lines: Iterator[str]) -> Iterator[tuple[str, dict[str, object]]]:
    event_name = "message"
    data_lines: list[str] = []
    for line in lines:
        if not line:
            if data_lines:
                payload = json.loads("\n".join(data_lines))
                if isinstance(payload, dict):
                    yield event_name, payload
            event_name = "message"
            data_lines = []
        elif line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if data_lines:
        payload = json.loads("\n".join(data_lines))
        if isinstance(payload, dict):
            yield event_name, payload


def run(base_url: str, log_path: Path, require_provider: bool) -> int:
    with httpx.Client(base_url=base_url, timeout=180) as client:
        status_response = client.get("/v1/agent")
        status_response.raise_for_status()
        status = status_response.json()
        model = str(status["model"])
        print(f"runtime={status['runtime']} model={model}")
        print(f"configuration={json.dumps(status['configuration'], ensure_ascii=False)}")
        if require_provider and model == "sage-demo-model":
            print(
                "Expected a Provider model, but the service is using sage-demo-model.",
                file=sys.stderr,
            )
            return 2

        with log_path.open("rb") as stream:
            upload = client.post(
                "/v1/attachments",
                files={"file": (log_path.name, stream, "text/plain")},
            )
        upload.raise_for_status()
        attachment_id = upload.json()["attachment_id"]

        request = {
            "mode": "agent",
            "message": "Use a subagent to analyze this IIS log and create a concise report",
            "thread_id": f"smoke-{uuid4().hex}",
            "attachment_ids": [attachment_id],
        }
        events: list[str] = []
        completed: dict[str, object] | None = None
        with client.stream(
            "POST",
            "/v1/messages/stream",
            json=request,
            headers={"Accept": "text/event-stream"},
        ) as response:
            response.raise_for_status()
            for event_name, data in parse_sse(response.iter_lines()):
                events.append(event_name)
                detail = data.get("resource") or data.get("tool") or data.get("agent") or ""
                print(f"{event_name}: {detail}")
                if event_name == "run.failed":
                    print(str(data.get("message", "Agent run failed")), file=sys.stderr)
                    return 1
                if event_name == "run.completed":
                    response_data = data.get("response")
                    if isinstance(response_data, dict):
                        completed = response_data

    required_events = {
        "plan.updated",
        "subagent.started",
        "skill.loading",
        "skill.loaded",
        "skill.resource.loaded",
        "tool.started",
        "tool.completed",
        "run.completed",
    }
    missing = sorted(required_events - set(events))
    if missing or completed is None:
        print(f"Missing required events: {', '.join(missing)}", file=sys.stderr)
        return 1

    result = completed.get("result")
    usage = completed.get("usage")
    skills_used = result.get("skills_used", []) if isinstance(result, dict) else []
    print(f"skills_used={skills_used}")
    print(f"usage={json.dumps(usage, ensure_ascii=False)}")
    if skills_used != ["iis-logs", "report-summary"]:
        print("The expected two-Skill workflow did not complete.", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument(
        "--require-provider",
        action="store_true",
        help="Fail when the service is still using the offline demo model.",
    )
    arguments = parser.parse_args()
    if not arguments.log.is_file():
        parser.error(f"Log file does not exist: {arguments.log}")
    return run(arguments.base_url, arguments.log.resolve(), arguments.require_provider)


if __name__ == "__main__":
    raise SystemExit(main())
