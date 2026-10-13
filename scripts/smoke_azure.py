"""Verify Azure OpenAI v1 connectivity with API Key or Entra ID."""

import argparse
import os

from openai import OpenAI

from sage.agent_model import azure_token_provider
from sage.settings import SettingsError, load_agent_settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prompt",
        default="Reply with exactly: Azure OpenAI connection succeeded",
    )
    arguments = parser.parse_args()
    settings = load_agent_settings()
    authentication = settings.authentication_label()
    if authentication == "entra-id":
        api_key = azure_token_provider(settings)
    elif authentication == "api-key":
        api_key = os.getenv("AZURE_OPENAI_API_KEY", "").strip()
        if not api_key:
            raise SettingsError("AZURE_OPENAI_API_KEY is not configured")
    else:
        parser.error("Configure an Azure OpenAI model in .env first")

    client = OpenAI(
        base_url=settings.azure_v1_base_url(),
        api_key=api_key,
        timeout=settings.timeout_seconds,
        max_retries=settings.max_retries,
    )
    response = client.responses.create(
        model=settings.azure_deployment_name(),
        input=arguments.prompt,
        max_output_tokens=min(settings.max_tokens or 256, 256),
    )
    print(response.output_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
