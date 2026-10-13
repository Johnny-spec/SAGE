from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_routes_python_execution_to_separate_runner() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    api = services["sage"]
    runner = services["sage-runner"]

    assert api["environment"]["SAGE_RUNNER_URL"] == "http://sage-runner:8080"
    assert api["environment"]["SAGE_ALLOW_LOCAL_SUBPROCESS"] == "false"
    assert runner["read_only"] is True
    assert runner["cap_drop"] == ["ALL"]
    assert runner["security_opt"] == ["no-new-privileges:true"]
    assert "volumes" not in runner
    assert runner["tmpfs"] == ["/tmp:size=512m,mode=1777"]


def test_kubernetes_runner_has_no_state_volume_identity_or_egress() -> None:
    documents = list(
        yaml.safe_load_all(
            (ROOT / "deploy" / "kubernetes" / "sage.yaml").read_text(encoding="utf-8")
        )
    )
    config = next(document for document in documents if document["kind"] == "ConfigMap")
    runner = next(
        document
        for document in documents
        if document["kind"] == "Deployment"
        and document["metadata"]["name"] == "sage-runner"
    )
    service = next(
        document
        for document in documents
        if document["kind"] == "Service" and document["metadata"]["name"] == "sage-runner"
    )
    network = next(
        document
        for document in documents
        if document["kind"] == "NetworkPolicy"
        and document["metadata"]["name"] == "sage-runner-isolation"
    )
    pod = runner["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert config["data"]["SAGE_ADMIN_AUTH_ENABLED"] == "true"
    assert config["data"]["SAGE_ALLOW_LOCAL_SUBPROCESS"] == "false"
    assert config["data"]["SAGE_RUNNER_URL"] == "http://sage-runner:8080"
    assert pod["automountServiceAccountToken"] is False
    assert not any(volume.get("persistentVolumeClaim") for volume in pod["volumes"])
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }
    assert container["resources"]["limits"]["ephemeral-storage"] == "512Mi"
    assert service["spec"]["ports"][0]["port"] == 8080
    assert network["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert network["spec"]["egress"] == []
    assert network["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"] == {
        "app": "sage"
    }
