"""Subprocess execution for administrator-trusted Python Skills."""

import asyncio
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from sage.domain import RuntimeContext
from sage.errors import ExecutionError
from sage.models import ArgumentBinding, SkillDefinition


class SubprocessAdapter:
    async def execute(
        self,
        definition: SkillDefinition,
        arguments: Mapping[str, Any],
        *,
        context: RuntimeContext,
    ) -> dict[str, Any]:
        del context
        with TemporaryDirectory(prefix="sage-json-input-") as temporary:
            return await self._execute(definition, arguments, Path(temporary))

    async def _execute(
        self,
        definition: SkillDefinition,
        arguments: Mapping[str, Any],
        input_directory: Path,
    ) -> dict[str, Any]:
        binding = definition.manifest.spec.execution
        if binding.entrypoint is None:
            raise ExecutionError(f"Missing subprocess entrypoint: {definition.name}")
        entrypoint = (definition.package_root / binding.entrypoint).resolve()
        if (
            not entrypoint.is_relative_to(definition.package_root.resolve())
            or not entrypoint.is_file()
        ):
            raise ExecutionError(f"Invalid subprocess entrypoint: {definition.name}")
        if entrypoint.suffix.casefold() != ".py":
            raise ExecutionError("Only Python subprocess Skills are enabled")

        argv = build_subprocess_argv(binding.arguments, arguments, input_directory=input_directory)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(entrypoint),
            *argv,
            cwd=str(definition.package_root),
            env=safe_subprocess_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await process.communicate()
        except BaseException:
            process.kill()
            await process.wait()
            raise

        stderr_text = stderr.decode("utf-8", errors="replace")[:16_384]
        if process.returncode != 0:
            raise ExecutionError(
                f"Skill process failed with exit code {process.returncode}: {stderr_text}"
            )
        try:
            result = json.loads(stdout.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExecutionError(f"Skill returned invalid JSON: {definition.name}") from exc
        if not isinstance(result, dict):
            raise ExecutionError(f"Skill result must be an object: {definition.name}")
        return result


def build_subprocess_argv(
    bindings: list[ArgumentBinding],
    arguments: Mapping[str, Any],
    *,
    input_directory: Path | None = None,
) -> list[str]:
    def value(binding: ArgumentBinding) -> str:
        argument = arguments[binding.name]
        if binding.encoding != "json-file":
            return _argument_value(argument)
        if input_directory is None:
            raise ExecutionError("JSON file arguments require a private execution directory")
        target = input_directory / f"{uuid4().hex}.json"
        target.write_text(json.dumps(argument, ensure_ascii=False), encoding="utf-8")
        return str(target)

    argv: list[str] = []
    for item in sorted(
        (binding for binding in bindings if binding.position is not None),
        key=lambda binding: binding.position or 0,
    ):
        if item.name not in arguments:
            raise ExecutionError(f"Missing positional argument: {item.name}")
        argv.append(value(item))
    for item in (binding for binding in bindings if binding.option is not None):
        if item.name in arguments and arguments[item.name] is not None:
            argv.extend((str(item.option), value(item)))
    return argv


def _argument_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def safe_subprocess_environment() -> dict[str, str]:
    allowed = ("SystemRoot", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP")
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    return environment
