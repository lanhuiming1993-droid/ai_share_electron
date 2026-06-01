from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Generic, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel
from pydantic_ai import Agent, PromptedOutput
from pydantic_ai.models import create_async_http_client
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

OutputT = TypeVar("OutputT", bound=BaseModel)


@dataclass(frozen=True)
class ProviderRuntimeConfig:
    id: str
    base_url: str
    model: str
    protocol: str
    api_key: str
    extra_body: dict


@dataclass(frozen=True)
class GatewayResult(Generic[OutputT]):
    output: str | OutputT
    input_tokens: int
    output_tokens: int
    requests: int


class ModelGateway:
    def __init__(self, *, timeout_seconds: int = 90, connect_timeout_seconds: int = 10, network_retries: int = 2) -> None:
        self.timeout_seconds = timeout_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self.network_retries = network_retries
        self._local = threading.local()

    def run_text(self, config: ProviderRuntimeConfig, prompt: str, *, instructions: str) -> GatewayResult:
        agent = Agent(
            model=self._model(config),
            output_type=str,
            instructions=instructions,
            model_settings=self._model_settings(config),
            retries=2,
        )
        result = agent.run_sync(prompt)
        return self._result(result.output, result.usage)

    def run_structured(
        self,
        config: ProviderRuntimeConfig,
        prompt: str,
        *,
        instructions: str,
        output_type: type[OutputT],
    ) -> GatewayResult[OutputT]:
        agent = Agent(
            model=self._model(config),
            output_type=PromptedOutput(output_type),
            instructions=instructions,
            model_settings=self._model_settings(config),
            retries=2,
        )
        result = agent.run_sync(prompt)
        return self._result(result.output, result.usage)

    def _model(self, config: ProviderRuntimeConfig):
        cache = getattr(self._local, "models", None)
        if cache is None:
            cache = {}
            self._local.models = cache
        fingerprint = self._fingerprint(config)
        if fingerprint in cache:
            return cache[fingerprint]
        http_client = create_async_http_client(timeout=self.timeout_seconds, connect=self.connect_timeout_seconds)
        openai_client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url.rstrip("/") + "/",
            timeout=self.timeout_seconds,
            max_retries=self.network_retries,
            http_client=http_client,
        )
        provider = OpenAIProvider(openai_client=openai_client)
        model = (
            OpenAIResponsesModel(config.model, provider=provider)
            if config.protocol == "openai_responses"
            else OpenAIChatModel(config.model, provider=provider)
        )
        cache[fingerprint] = model
        return model

    def _model_settings(self, config: ProviderRuntimeConfig) -> dict:
        settings = {
            "timeout": self.timeout_seconds,
            "extra_body": config.extra_body or {},
        }
        if config.protocol == "openai_responses":
            settings["openai_store"] = False
        else:
            settings["temperature"] = 0.2
        return settings

    @staticmethod
    def _fingerprint(config: ProviderRuntimeConfig) -> str:
        value = json.dumps(
            {
                "id": config.id,
                "base_url": config.base_url,
                "model": config.model,
                "protocol": config.protocol,
                "api_key_hash": hashlib.sha256(config.api_key.encode()).hexdigest(),
                "extra_body": config.extra_body,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def _result(output, usage) -> GatewayResult:
        return GatewayResult(
            output=output,
            input_tokens=int(usage.input_tokens or 0),
            output_tokens=int(usage.output_tokens or 0),
            requests=int(usage.requests or 0),
        )
