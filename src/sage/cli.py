"""SAGE authoring and local-runtime command line interface."""

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path

import httpx
import uvicorn

from sage.solution_packages import (
    SolutionPackageError,
    validate_solution_bundle_directory,
    write_solution_bundle_zip,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sage")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-solution", help="Validate a Solution Bundle")
    validate.add_argument("path", type=Path)
    package = commands.add_parser("package-solution", help="Create a verified Solution ZIP")
    package.add_argument("path", type=Path)
    package.add_argument("--output", type=Path, required=True)
    publish = commands.add_parser("publish-solution", help="Upload a Solution Bundle")
    publish.add_argument("package", type=Path)
    publish.add_argument("--server", default="http://127.0.0.1:8000")
    promote = commands.add_parser("promote", help="Validate and submit Release Candidates")
    promote.add_argument("--release", action="append", required=True)
    promote.add_argument("--title", required=True)
    promote.add_argument("--server", default="http://127.0.0.1:8000")
    rollback = commands.add_parser("rollback", help="Request a Generation rollback")
    rollback.add_argument("generation_id")
    rollback.add_argument("--server", default="http://127.0.0.1:8000")
    replay = commands.add_parser("replay", help="Read persisted Run events")
    replay.add_argument("run_id")
    replay.add_argument("--after", type=int, default=0)
    replay.add_argument("--server", default="http://127.0.0.1:8000")
    serve = commands.add_parser("serve", help="Start the self-hosted SAGE API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-solution":
        try:
            manifest = validate_solution_bundle_directory(args.path)
        except SolutionPackageError as exc:
            print(json.dumps({"valid": False, "error": str(exc)}))
            return 1
        print(
            json.dumps(
                {
                    "valid": True,
                    "solution_id": manifest.metadata.name,
                    "version": manifest.metadata.version,
                    "components": len(manifest.spec.components),
                }
            )
        )
        return 0
    if args.command == "package-solution":
        try:
            output = write_solution_bundle_zip(args.path, args.output)
        except SolutionPackageError as exc:
            print(json.dumps({"packaged": False, "error": str(exc)}))
            return 1
        print(json.dumps({"packaged": True, "path": str(output)}))
        return 0
    if args.command == "publish-solution":
        with args.package.open("rb") as stream:
            response = httpx.post(
                f"{args.server.rstrip('/')}/v1/admin/solutions/imports",
                headers=_admin_headers(),
                files={"file": (args.package.name, stream, "application/zip")},
                timeout=120,
            )
        return _print_response(response)
    if args.command == "promote":
        headers = _admin_headers()
        server = args.server.rstrip("/")
        candidate = httpx.post(
            f"{server}/v1/admin/candidates",
            headers=headers,
            json={"title": args.title, "solution_release_ids": args.release},
            timeout=30,
        )
        if candidate.is_error:
            return _print_response(candidate)
        candidate_id = candidate.json()["candidate_id"]
        validated = httpx.post(
            f"{server}/v1/admin/candidates/{candidate_id}/validate",
            headers=headers,
            timeout=120,
        )
        if validated.is_error or validated.json().get("status") != "valid":
            return _print_response(validated, success=False)
        submitted = httpx.post(
            f"{server}/v1/admin/candidates/{candidate_id}/submit",
            headers=headers,
            timeout=30,
        )
        return _print_response(submitted)
    if args.command == "rollback":
        response = httpx.post(
            f"{args.server.rstrip('/')}/v1/admin/generations/"
            f"{args.generation_id}/rollback-request",
            headers=_admin_headers(),
            timeout=30,
        )
        return _print_response(response)
    if args.command == "replay":
        token = os.getenv("SAGE_ACCESS_TOKEN")
        response = httpx.get(
            f"{args.server.rstrip('/')}/v1/runs/{args.run_id}/events",
            headers={"Authorization": f"Bearer {token}"} if token else {},
            params={"after": args.after},
            timeout=30,
        )
        return _print_response(response)
    if args.command == "serve":
        uvicorn.run(
            "sage.app:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
        return 0
    return 2


def _admin_headers() -> dict[str, str]:
    token = os.getenv("SAGE_ADMIN_TOKEN") or os.getenv("SAGE_ACCESS_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _print_response(response: httpx.Response, *, success: bool | None = None) -> int:
    try:
        value = response.json()
    except ValueError:
        value = {"status_code": response.status_code, "message": response.text[:500]}
    print(json.dumps(value, ensure_ascii=False))
    accepted = not response.is_error if success is None else success and not response.is_error
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())