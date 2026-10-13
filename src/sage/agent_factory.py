"""Factory for versioned Agent runtimes."""

from typing import Any

from langchain_core.language_models import BaseChatModel

from sage.agent_catalog import AgentCatalog
from sage.agent_invocations import AgentInvoke
from sage.agent_model import load_agent_model
from sage.agent_runtime import DeepAgentRuntime
from sage.capabilities import CapabilityViewResolver
from sage.domain import AgentDefinition
from sage.execution_approvals import ExecutionApprovals
from sage.executor import SkillExecutor
from sage.memory import MemoryStore
from sage.models import SkillDefinition
from sage.registry import SkillRegistry
from sage.settings import AgentModelSettings, load_agent_settings
from sage.tools import ToolExecutor


class AgentRuntimeFactory:
    def __init__(
        self,
        registry: SkillRegistry,
        executor: SkillExecutor,
        catalog: AgentCatalog,
        capabilities: CapabilityViewResolver,
        *,
        model: BaseChatModel | None = None,
        model_name: str | None = None,
        settings: AgentModelSettings | None = None,
        memory_store: MemoryStore | None = None,
        checkpointer: Any | None = None,
        agent_invoker: AgentInvoke | None = None,
        tool_executor: ToolExecutor | None = None,
    ) -> None:
        resolved_settings = settings or load_agent_settings()
        if model is None:
            model, configured_name = load_agent_model(resolved_settings)
            model_name = model_name or configured_name
        self._registry = registry
        self._executor = executor
        self._catalog = catalog
        self._capabilities = capabilities
        self._model = model
        self._model_name = model_name or getattr(model, "model_name", "agent-model")
        self._settings = resolved_settings
        self._memory_store = memory_store
        self._checkpointer = checkpointer
        self._agent_invoker = agent_invoker
        self._tool_executor = tool_executor
        self._runtimes: dict[tuple[str, str], DeepAgentRuntime] = {}

    @property
    def catalog(self) -> AgentCatalog:
        return self._catalog

    @property
    def has_persistent_checkpointer(self) -> bool:
        return self._checkpointer is not None

    def for_skill(
        self,
        skill: SkillDefinition,
        approvals: ExecutionApprovals | None = None,
    ) -> DeepAgentRuntime:
        execution = skill.manifest.spec.execution
        definition = AgentDefinition(
            agent_id=f"skill-{skill.name}",
            name=f"skill-{skill.name}",
            version=skill.version,
            instructions=(
                (skill.package_root / "SKILL.md").read_text(encoding="utf-8")
                + f"\nRead /{skill.name}/SKILL.md before acting. "
                "Use only the declared capabilities. Do not delegate. "
                "Return only a JSON object matching the supplied output schema."
            ),
            capability_ids=execution.capability_ids,
            max_steps=execution.max_steps,
            max_model_calls=execution.max_model_calls,
            max_tool_calls=execution.max_tool_calls,
        )
        return DeepAgentRuntime(
            self._registry,
            self._executor,
            definition,
            model=self._model,
            model_name=self._model_name,
            settings=self._settings,
            capabilities=self._capabilities,
            checkpointer=self._checkpointer,
            tool_executor=self._tool_executor,
            skill_scope=skill,
            execution_approvals=approvals,
        )

    def get(
        self,
        agent_id: str | None = None,
        *,
        approvals: ExecutionApprovals | None = None,
        agent_invoker: AgentInvoke | None = None,
    ) -> DeepAgentRuntime:
        definition = self._catalog.get(agent_id)
        key = (definition.agent_id, definition.version)
        runtime = self._runtimes.get(key) if approvals is None and agent_invoker is None else None
        if runtime is None:
            runtime = DeepAgentRuntime(
                self._registry,
                self._executor,
                model=self._model,
                model_name=self._model_name,
                settings=self._settings,
                definition=definition,
                capabilities=self._capabilities,
                memory_store=self._memory_store,
                checkpointer=self._checkpointer,
                agent_invoker=agent_invoker or self._agent_invoker,
                tool_executor=self._tool_executor,
                execution_approvals=approvals,
            )
            if approvals is None and agent_invoker is None:
                self._runtimes[key] = runtime
        return runtime
