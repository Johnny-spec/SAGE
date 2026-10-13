from sage.app import DEFAULT_SKILLS_ROOT
from sage.capabilities import CapabilityViewResolver
from sage.domain import Principal, RuntimeContext
from sage.models import SkillPolicy
from sage.policy import PolicyEnforcer
from sage.registry import SkillRegistry


def _context(*roles: str) -> RuntimeContext:
    return RuntimeContext.create(
        mode="direct",
        principal=Principal(
            workspace_id="ws_default",
            principal_id="usr_test",
            roles=frozenset(roles),
            authentication_method="local",
        ),
        generation_id="gen_test",
    )


def _registry_with_write_echo() -> SkillRegistry:
    registry = SkillRegistry.load(DEFAULT_SKILLS_ROOT)
    echo = registry.get("echo")
    assert echo is not None
    policy = SkillPolicy(risk="write-low")
    spec = echo.manifest.spec.model_copy(update={"policy": policy})
    manifest = echo.manifest.model_copy(update={"spec": spec})
    write_echo = echo.model_copy(update={"manifest": manifest})
    return SkillRegistry({"echo": write_echo})


def test_capability_view_projects_registered_skills() -> None:
    registry = SkillRegistry.load(DEFAULT_SKILLS_ROOT)
    view = CapabilityViewResolver(registry, PolicyEnforcer()).resolve(_context("user"))

    assert len(view) == len(registry)
    assert view.generation_id == "gen_test"
    assert view.get_skill("echo") is not None
    assert view.contains("skill:echo") is True


def test_capability_view_applies_explicit_agent_bindings() -> None:
    registry = SkillRegistry.load(DEFAULT_SKILLS_ROOT)
    view = CapabilityViewResolver(registry, PolicyEnforcer()).resolve(
        _context("user"),
        allowed_capability_ids={"skill:echo", "skill:report-summary"},
    )

    assert {descriptor.capability_id for descriptor in view.descriptors} == {
        "skill:echo",
        "skill:report-summary",
    }


def test_capability_view_hides_write_skill_from_regular_user() -> None:
    view = CapabilityViewResolver(
        _registry_with_write_echo(),
        PolicyEnforcer(),
    ).resolve(_context("user"))

    assert len(view) == 0
    assert view.get_skill("echo") is None


def test_capability_view_exposes_write_skill_to_capability_admin() -> None:
    view = CapabilityViewResolver(
        _registry_with_write_echo(),
        PolicyEnforcer(),
    ).resolve(_context("capability_admin"))

    assert view.get_skill("echo") is not None


def test_capability_view_hides_disabled_skill_from_every_role() -> None:
    registry = SkillRegistry.load(DEFAULT_SKILLS_ROOT)
    echo = registry.get("echo")
    assert echo is not None
    policy = echo.manifest.spec.policy.model_copy(update={"execution_enabled": False})
    spec = echo.manifest.spec.model_copy(update={"policy": policy})
    manifest = echo.manifest.model_copy(update={"spec": spec})
    disabled = echo.model_copy(update={"manifest": manifest})
    view = CapabilityViewResolver(
        SkillRegistry.from_definitions(iter((disabled,))),
        PolicyEnforcer(),
    ).resolve(_context("platform_admin"))

    assert view.get_skill("echo") is None


def test_capability_view_hides_unavailable_execution_type() -> None:
    registry = SkillRegistry.load(DEFAULT_SKILLS_ROOT)
    view = CapabilityViewResolver(
        registry,
        PolicyEnforcer(),
        enabled_execution_types={"builtin", "workflow", "model"},
    ).resolve(_context("user"))

    assert view.get_skill("echo") is not None
    assert view.get_skill("iis-logs") is None
    assert view.get_skill("netlog") is None
