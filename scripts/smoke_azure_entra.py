"""Sign in with Azure CLI and verify the configured Azure OpenAI deployment."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import set_key

from sage.agent_model import load_agent_model
from sage.settings import DEFAULT_ENV_FILE, AgentModelSettings, SettingsError, load_agent_settings


def ensure_login(*, login: bool, tenant: str | None, subscription: str | None = None) -> None:
    executable = shutil.which("az")
    if executable is None:
        raise SettingsError("Azure CLI is required. Install Azure CLI, then restart this terminal.")
    account = subprocess.run(
        [executable, "account", "show", "--query", "tenantId", "--output", "tsv"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    needs_login = account.returncode != 0 or not account.stdout.strip()
    if tenant and account.stdout.strip().casefold() != tenant.casefold():
        needs_login = True
    if login or needs_login:
        command = [executable, "login", "--output", "none"]
        if tenant:
            command.extend(["--tenant", tenant])
        environment = os.environ.copy()
        if subscription or tenant:
            environment["AZURE_CORE_LOGIN_EXPERIENCE_V2"] = "off"
        completed = subprocess.run(command, check=False, env=environment)
        if completed.returncode:
            raise SettingsError(
                "Azure CLI login failed. Complete browser authentication and retry."
            )
    if subscription:
        selected = subprocess.run(
            [executable, "account", "set", "--subscription", subscription],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if selected.returncode:
            raise SettingsError(
                "Cannot select the requested subscription. Check account access and tenant."
            )
        verified = subprocess.run(
            [executable, "account", "show", "--query", "id", "--output", "tsv"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if verified.returncode or verified.stdout.strip().casefold() != subscription.casefold():
            raise SettingsError("Azure CLI did not select the requested subscription.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login", action="store_true", help="Force browser sign-in")
    parser.add_argument("--tenant", help="Azure tenant ID or domain")
    parser.add_argument("--subscription", help="Azure subscription ID to select after login")
    parser.add_argument("--configure", action="store_true", help="Save auth settings after success")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument(
        "--prompt",
        default="Reply with exactly: Entra ID connection succeeded",
    )
    arguments = parser.parse_args(argv)
    try:
        configured = load_agent_settings(arguments.env_file, azure_auth="entra-id")
        tenant = arguments.tenant or configured.azure_openai_tenant_id
        settings = AgentModelSettings.model_validate(
            {
                **configured.model_dump(),
                "azure_openai_credential": "azure-cli",
                "azure_openai_tenant_id": tenant,
                "max_tokens": min(configured.max_tokens or 256, 256),
                "max_retries": 0,
            }
        )
        ensure_login(login=arguments.login, tenant=tenant, subscription=arguments.subscription)
        model, name = load_agent_model(settings)
        response = model.invoke(arguments.prompt)
        if not response.text:
            raise SettingsError("Model returned no text; authentication settings were not saved.")
        print(response.text)
        print(f"Verified {name} using Entra ID (Azure CLI).")
        if arguments.configure:
            set_key(str(arguments.env_file), "SAGE_AZURE_OPENAI_AUTH", "entra-id")
            set_key(str(arguments.env_file), "SAGE_AZURE_OPENAI_CREDENTIAL", "azure-cli")
            if tenant:
                set_key(str(arguments.env_file), "SAGE_AZURE_OPENAI_TENANT_ID", tenant)
            print(
                "Saved non-secret authentication settings. Restart SAGE; "
                "process environment overrides .env."
            )
    except Exception as exc:
        messages = {
            "AuthenticationError": (
                "Model returned 401. Check Azure tenant, token scope and endpoint."
            ),
            "PermissionDeniedError": (
                "Model returned 403. Check resource data-plane role and network access."
            ),
            "ClientAuthenticationError": (
                "Entra token acquisition failed. Run again with --login and verify the tenant."
            ),
            "CredentialUnavailableError": (
                "Azure CLI credentials unavailable. Run again with --login."
            ),
            "APIConnectionError": (
                "Cannot reach the model endpoint. Check network/private endpoint access."
            ),
            "NotFoundError": (
                "Model deployment or API endpoint was not found. "
                "Check configured deployment and endpoint."
            ),
            "APITimeoutError": "Model request timed out; authentication settings were not saved.",
            "TimeoutExpired": "Azure CLI did not respond within the timeout.",
        }
        message = (
            str(exc)
            if isinstance(exc, SettingsError)
            else messages.get(
                type(exc).__name__,
                f"Verification failed ({type(exc).__name__}); settings were not confirmed.",
            )
        )
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
