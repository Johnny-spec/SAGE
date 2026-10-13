import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_native_tools import context
from test_tool_registration import discover, manager_and_store

from sage.errors import PolicyDeniedError
from sage.execution_approvals import ExecutionApprovals
from sage.human_tasks import SqliteHumanTaskStore
from sage.invocations import SqliteInvocationStore


def test_exact_native_grant_and_dispatch_replay(tmp_path: Path):
    manager, _ = manager_and_store(tmp_path)
    server_id = discover(manager)
    definition = manager.register_tool(
        server_id,
        "clauses/search",
        version="1.0.0",
        description="Write",
        risk="write-high",
    ).definition
    approval = ExecutionApprovals(
        SqliteHumanTaskStore(str(tmp_path / "tasks.sqlite3")),
        SqliteInvocationStore(str(tmp_path / "calls.sqlite3")),
    )
    runtime = replace(context(), lifecycle="durable")
    with pytest.raises(PolicyDeniedError):
        approval.prepare(runtime, definition, {"value": 1}, "call-one")
    runtime = replace(
        runtime,
        principal=replace(
            runtime.principal,
            roles=frozenset({"platform_admin"}),
        ),
    )
    task = approval.prepare(runtime, definition, {"value": 1}, "call-one")
    assert task is not None
    assert task.subject["arguments"] == {"value": 1}
    assert approval.prepare(runtime, definition, {"value": 1}, "call-one") == task
    executor = AsyncMock()
    executor.execute.return_value = {"ok": True}

    async def scenario():
        with pytest.raises(PolicyDeniedError):
            await approval.dispatch(executor, runtime, definition, {"value": 1}, "call-one", task)
        approval.tasks.decide(
            task.task_id,
            actor_id=runtime.principal.principal_id,
            actor_roles=runtime.principal.roles,
            decision={},
            approve=True,
            reason="Verified",
        )
        for _ in range(2):
            assert await approval.dispatch(
                executor,
                runtime,
                definition,
                {"value": 1},
                "call-one",
                task,
            ) == {"ok": True}
        executor.execute.assert_awaited_once()
        with pytest.raises(PolicyDeniedError):
            await approval.dispatch(executor, runtime, definition, {"value": 2}, "call-one", task)
        with pytest.raises(PolicyDeniedError):
            await approval.dispatch(executor, runtime, definition, {"value": 1}, "call-two", task)

    asyncio.run(scenario())


def test_approval_argument_view_is_redacted_and_bounded(tmp_path: Path):
    manager, _ = manager_and_store(tmp_path)
    definition = manager.register_tool(
        discover(manager), "clauses/search", version="1.0.0", description="Write",
        risk="write-high",
    ).definition.model_copy(update={"input_schema": {
        "type": "object", "properties": {"personal": {"x-sensitive": True}},
    }})
    subject = ExecutionApprovals.subject(
        context(), definition, {"personal": "private", "password": "hidden", "value": 1}, "call",
    )
    assert subject["arguments"] == {
        "personal": "[redacted]", "password": "[redacted]", "value": 1,
    }
    changed = ExecutionApprovals.subject(
        context(), definition, {"personal": "changed", "password": "hidden", "value": 1}, "call",
    )
    assert subject["input_digest"] != changed["input_digest"]
    large = ExecutionApprovals.subject(context(), definition, {"text": "x" * 20000}, "call")
    assert "omitted" in large["arguments"]


@pytest.mark.parametrize("decision", ["approve", "reject", "cancel"])
def test_delegated_graph_resumes_child_turns(tmp_path: Path, decision: str):
    from types import MethodType, SimpleNamespace

    from fastapi import HTTPException
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langgraph.checkpoint.memory import InMemorySaver
    from test_agent_runtime import ScriptedProviderModel

    from sage.agent_catalog import AgentCatalog
    from sage.agent_factory import AgentRuntimeFactory
    from sage.capabilities import CapabilityViewResolver
    from sage.domain import AgentDefinition, DelegationDefinition
    from sage.executor import SkillExecutor
    from sage.policy import PolicyEnforcer
    from sage.registry import SkillRegistry
    from sage.runs import InMemoryRunStore
    from sage.runtime_manager import RuntimeManager
    from sage.skill_orchestration import SkillOrchestrator

    class DelegatedWriteModel(ScriptedProviderModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            completed = [item for item in messages if isinstance(item, ToolMessage)]
            if "delegate_writer" in self.bound_tool_names:
                message = AIMessage(content="done") if completed else AIMessage(
                    content="", tool_calls=[{
                        "name": "delegate_writer", "args": {
                            "goal": "Perform two writes", "input": {}, "artifact_ids": [],
                        }, "id": "delegate-call", "type": "tool_call",
                    }],
                )
            elif len(completed) < 2:
                name = next(name for name in self.bound_tool_names if name.startswith("mcp_"))
                message = AIMessage(content="", tool_calls=[{
                    "name": name, "args": {"arguments": {"value": len(completed)}},
                    "id": f"write-{len(completed)}", "type": "tool_call",
                }])
            else:
                message = AIMessage(content='{"ok":true}')
            return ChatResult(generations=[ChatGeneration(message=message)])

    manager, _ = manager_and_store(tmp_path)
    tool = manager.register_tool(
        discover(manager), "clauses/search", version="1.0.0",
        description="Write", risk="write-high",
    ).definition
    coordinator = AgentDefinition(
        "coordinator", "Coordinator", "1.0.0", "Delegate writes", (),
        delegations=(DelegationDefinition("writer", "writer-agent", "Write twice"),),
    )
    writer = AgentDefinition("writer-agent", "Writer", "1.0.0", "Write twice", (tool.tool_id,))
    catalog = AgentCatalog((coordinator, writer), default_agent_id="coordinator")
    registry = SkillRegistry({})
    capabilities = CapabilityViewResolver(registry, PolicyEnforcer(), [tool])
    executor = AsyncMock()
    executor.execute.return_value = {"ok": True}
    factory = AgentRuntimeFactory(
        registry, SkillExecutor(), catalog, capabilities, model=DelegatedWriteModel(),
        checkpointer=InMemorySaver(), tool_executor=executor,
    )
    snapshot = SimpleNamespace(
        agent_runtimes=factory, agent_catalog=catalog, solution_release=None,
    )
    runs = InMemoryRunStore()
    runtimes = SimpleNamespace(_runs=runs, get=lambda generation, solution: snapshot)
    runtimes.resolve_child = MethodType(RuntimeManager.resolve_child, runtimes)
    orchestrator = SkillOrchestrator(
        None, runtimes, runs, SqliteInvocationStore(str(tmp_path / "calls.sqlite3")),
        AsyncMock(), "tests", SqliteHumanTaskStore(str(tmp_path / "tasks.sqlite3")),
    )
    root = replace(context(), lifecycle="durable", agent_id="coordinator", request_id="root-wf")
    root = replace(root, principal=replace(root.principal, roles=frozenset({"platform_admin"})))

    async def scenario():
        await runs.begin(root)
        await runs.append(root.run_id, "skill.orchestration.prepared", {
            "execution_type": "model", "input": {},
        })
        pending = await orchestrator.model(root.run_id)
        assert pending["status"] == "awaiting-approval"
        executor.execute.assert_not_awaited()
        child_id = orchestrator.approvals.tasks.get(pending["tasks"][0]["task_id"]).run_id
        child = await runs.get(child_id)
        assert child.context.parent_run_id == root.run_id
        assert child.context.request_id == root.request_id
        if decision == "cancel":
            await runs.append(root.run_id, "run.cancelled", {})
            task = orchestrator.approvals.tasks.get(pending["tasks"][0]["task_id"])
            with pytest.raises(PolicyDeniedError, match="no longer running"):
                await orchestrator.approvals.dispatch(
                    executor, child.context, tool, {"value": 0}, "write-0", task,
                )
            executor.execute.assert_not_awaited()
            return
        for expected_calls in range(2):
            task_id = pending["tasks"][0]["task_id"]
            orchestrator.approvals.tasks.decide(
                task_id, actor_id=root.principal.principal_id, actor_roles=root.principal.roles,
                decision={}, approve=decision == "approve", reason="Reviewed exact child call",
            )
            orchestrator._model_runtimes.clear()
            if decision == "reject":
                with pytest.raises(HTTPException):
                    await orchestrator.model(root.run_id)
                executor.execute.assert_not_awaited()
                assert (await runs.get(root.run_id)).status == "failed"
                return
            pending = await orchestrator.model(root.run_id)
            assert executor.execute.await_count == expected_calls + 1
        assert pending["status"] == "completed"
        assert pending["output"] == {"answer": "done"}
        assert (await runs.get(child_id)).status == "completed"
        assert await orchestrator.model(root.run_id) == pending
        assert executor.execute.await_count == 2
        root_run = await runs.get(root.run_id)
        last_turn = next(event.data for event in reversed(root_run.events)
                         if event.name == "skill.model.turn")
        assert last_turn["counters"]["delegation_calls"] == {"writer": 1}

    asyncio.run(scenario())


def test_cancelled_run_cannot_dispatch(tmp_path: Path):
    from sage.runs import InMemoryRunStore

    manager, _ = manager_and_store(tmp_path)
    tool = manager.register_tool(discover(manager), "clauses/search", version="1.0.0",
                                 description="Read").definition
    runs = InMemoryRunStore()
    approvals = ExecutionApprovals(
        SqliteHumanTaskStore(str(tmp_path / "tasks.sqlite3")),
        SqliteInvocationStore(str(tmp_path / "calls.sqlite3")), runs,
    )

    async def scenario():
        runtime = replace(context(), lifecycle="durable")
        await runs.begin(runtime)
        await runs.append(runtime.run_id, "run.cancelled", {})
        executor = AsyncMock()
        with pytest.raises(PolicyDeniedError, match="no longer running"):
            await approvals.dispatch(executor, runtime, tool, {}, "call", None)
        executor.execute.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("scoped_skill", [False, True])
@pytest.mark.parametrize("ordinary_skill", [False, True])
@pytest.mark.parametrize("memory_policy", ["thread", "none"])
def test_native_graph_interrupt_resumes_same_call(
    tmp_path: Path, scoped_skill: bool, ordinary_skill: bool, memory_policy: str,
):
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langgraph.checkpoint.memory import InMemorySaver
    from test_agent_runtime import ScriptedProviderModel

    from sage.agent_runtime import DeepAgentRuntime
    from sage.capabilities import CapabilityViewResolver
    from sage.domain import AgentDefinition
    from sage.executor import SkillExecutor
    from sage.policy import PolicyEnforcer
    from sage.registry import SkillRegistry

    class ApprovalModel(ScriptedProviderModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            rejected = any(isinstance(item, ToolMessage) and
                           "skill_instructions_not_loaded" in str(item.content)
                           for item in messages)
            loaded = any(isinstance(item, ToolMessage) and item.tool_call_id == "read-own"
                         for item in messages)
            if (scoped_skill or ordinary_skill) and rejected and not loaded:
                message = AIMessage(content="", tool_calls=[{
                    "name": "read_file", "args": {
                        "file_path": "/flow/SKILL.md" if scoped_skill else "/echo/SKILL.md"
                    },
                    "id": "read-own", "type": "tool_call",
                }])
            elif ordinary_skill and not any(
                isinstance(item, ToolMessage) and item.tool_call_id == "read-dependency"
                for item in messages
            ):
                message = AIMessage(content="", tool_calls=[{
                    "name": "read_file", "args": {"file_path": "/echo/SKILL.md"},
                    "id": "read-dependency", "type": "tool_call",
                }])
            elif any(
                isinstance(message, ToolMessage) and
                message.name.startswith("skill_" if ordinary_skill else "mcp_")
                and "skill_instructions_not_loaded" not in str(message.content)
                for message in messages
            ):
                message = AIMessage(content='{"ok":true}')
            else:
                name = next(name for name in self.bound_tool_names if name.startswith(
                    "skill_" if ordinary_skill else "mcp_"
                ))
                message = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": name,
                            "args": {"arguments": {"value": 1}},
                            "id": "fixed-call",
                            "type": "tool_call",
                        }
                    ],
                )
            return ChatResult(generations=[ChatGeneration(message=message)])

    manager, _ = manager_and_store(tmp_path)
    definition = manager.register_tool(
        discover(manager),
        "clauses/search",
        version="1.0.0",
        description="Write",
        risk="write-high",
    ).definition
    approvals = ExecutionApprovals(
        SqliteHumanTaskStore(str(tmp_path / "tasks.sqlite3")),
        SqliteInvocationStore(str(tmp_path / "calls.sqlite3")),
    )
    runtime_context = replace(context(), lifecycle="durable")
    runtime_context = replace(
        runtime_context,
        principal=replace(
            runtime_context.principal,
            roles=frozenset({"platform_admin"}),
        ),
    )
    executor = AsyncMock()
    executor.execute.return_value = {"ok": True}
    registry = SkillRegistry({})
    capability_id = definition.tool_id
    if ordinary_skill:
        skill = SkillRegistry.load_definition(Path(__file__).parent / "fixtures/skills/echo")
        spec = skill.manifest.spec
        skill = skill.model_copy(update={"manifest": skill.manifest.model_copy(update={
            "spec": spec.model_copy(update={
                "input_schema": {"type": "object", "properties": {"value": {"type": "integer"}}},
                "policy": spec.policy.model_copy(update={"risk": "write-high"}),
            }),
        })})
        registry = SkillRegistry({skill.name: skill})
        capability_id = f"skill:{skill.name}"
    saver = InMemorySaver()
    skill_scope = None
    if scoped_skill:
        from test_orchestrated_skills import make_skill
        skill_root, _ = make_skill(tmp_path, "model")
        skill_scope = SkillRegistry.load_definition(skill_root)

    def runtime():
        return DeepAgentRuntime(
            registry,
            executor if ordinary_skill else SkillExecutor(),
            AgentDefinition(
                "test", "Test", "1.0.0", "Execute the tool", (capability_id,),
                memory_policy=memory_policy,
            ),
            model=ApprovalModel(),
            capabilities=CapabilityViewResolver(registry, PolicyEnforcer(), [definition]),
            tool_executor=executor,
            skill_scope=skill_scope,
            execution_approvals=approvals,
            checkpointer=saver,
        )

    async def scenario():
        pending = await runtime().durable_turn("write", runtime_context)
        assert pending["status"] == "awaiting-approval"
        executor.execute.assert_not_awaited()
        task_id = pending["tasks"][0]["task_id"]
        assert approvals.tasks.get(task_id).subject["scope"] == (
            "published-skill" if ordinary_skill else "published-tool"
        )
        approvals.tasks.decide(
            task_id,
            actor_id=runtime_context.principal.principal_id,
            actor_roles=runtime_context.principal.roles,
            decision={},
            approve=True,
            reason="Reviewed",
        )
        result = await runtime().durable_turn(
            "write",
            runtime_context,
            resume=True,
            counters=pending["counters"],
        )
        assert result["status"] == "completed"
        assert result["answer"] == '{"ok":true}'
        executor.execute.assert_awaited_once()
        assert executor.execute.await_args.args[1] == {"value": 1}

    asyncio.run(scenario())
