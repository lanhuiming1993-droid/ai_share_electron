from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Generic, TypeVar

import requests
from openai import AsyncOpenAI
from pydantic import BaseModel
from pydantic_ai import Agent, PromptedOutput
from pydantic_ai.models import create_async_http_client
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

from backend.logging_config import get_logger, log_event, log_exception

OutputT = TypeVar("OutputT", bound=BaseModel)
logger = get_logger("model_gateway")
ANTHROPIC_MESSAGES_PROTOCOL = "anthropic_messages"
ANTHROPIC_STRUCTURED_TOOL_NAME = "emit_structured_response"


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
        started_at = time.perf_counter()
        log_event(logger, "INFO", "model.text.started", provider_id=config.id, model=config.model, protocol=config.protocol, prompt_chars=len(prompt))
        try:
            if config.protocol == ANTHROPIC_MESSAGES_PROTOCOL:
                output = self._run_anthropic_text(config, prompt, instructions=instructions)
            else:
                agent = Agent(
                    model=self._model(config),
                    output_type=str,
                    instructions=instructions,
                    model_settings=self._model_settings(config),
                    retries=2,
                )
                result = agent.run_sync(prompt)
                output = self._result(result.output, result.usage)
            log_event(
                logger,
                "INFO",
                "model.text.completed",
                provider_id=config.id,
                model=config.model,
                latency_ms=int((time.perf_counter() - started_at) * 1000),
                input_tokens=output.input_tokens,
                output_tokens=output.output_tokens,
                requests=output.requests,
            )
            return output
        except Exception as exc:
            log_exception(
                logger,
                "model.text.failed",
                exc,
                provider_id=config.id,
                model=config.model,
                protocol=config.protocol,
                latency_ms=int((time.perf_counter() - started_at) * 1000),
            )
            raise

    def run_structured(
        self,
        config: ProviderRuntimeConfig,
        prompt: str,
        *,
        instructions: str,
        output_type: type[OutputT],
    ) -> GatewayResult[OutputT]:
        started_at = time.perf_counter()
        log_event(
            logger,
            "INFO",
            "model.structured.started",
            provider_id=config.id,
            model=config.model,
            protocol=config.protocol,
            output_type=output_type.__name__,
            prompt_chars=len(prompt),
        )
        try:
            if config.protocol == ANTHROPIC_MESSAGES_PROTOCOL:
                output = self._run_anthropic_structured(config, prompt, instructions=instructions, output_type=output_type)
            else:
                agent = Agent(
                    model=self._model(config),
                    output_type=PromptedOutput(output_type),
                    instructions=instructions,
                    model_settings=self._model_settings(config),
                    retries=2,
                )
                result = agent.run_sync(prompt)
                output = self._result(result.output, result.usage)
            log_event(
                logger,
                "INFO",
                "model.structured.completed",
                provider_id=config.id,
                model=config.model,
                output_type=output_type.__name__,
                latency_ms=int((time.perf_counter() - started_at) * 1000),
                input_tokens=output.input_tokens,
                output_tokens=output.output_tokens,
                requests=output.requests,
            )
            return output
        except Exception as exc:
            log_exception(
                logger,
                "model.structured.failed",
                exc,
                provider_id=config.id,
                model=config.model,
                protocol=config.protocol,
                output_type=output_type.__name__,
                latency_ms=int((time.perf_counter() - started_at) * 1000),
            )
            raise

    def _model(self, config: ProviderRuntimeConfig):
        cache = getattr(self._local, "models", None)
        if cache is None:
            cache = {}
            self._local.models = cache
        if config.protocol == ANTHROPIC_MESSAGES_PROTOCOL:
            raise ValueError("Anthropic Messages protocol uses the native REST adapter")
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
        log_event(
            logger,
            "INFO",
            "model.client.created",
            provider_id=config.id,
            base_url=config.base_url,
            model=config.model,
            protocol=config.protocol,
            timeout_seconds=self.timeout_seconds,
            connect_timeout_seconds=self.connect_timeout_seconds,
            network_retries=self.network_retries,
        )
        return model

    def _model_settings(self, config: ProviderRuntimeConfig) -> dict:
        settings = {
            "timeout": self.timeout_seconds,
            "extra_body": config.extra_body or {},
        }
        if config.protocol == "openai_responses":
            settings["openai_store"] = False
        elif config.protocol == "openai_chat_completions":
            settings["temperature"] = 0.2
        return settings

    def _run_anthropic_text(self, config: ProviderRuntimeConfig, prompt: str, *, instructions: str) -> GatewayResult[str]:
        response = self._anthropic_request(config, self._anthropic_body(config, prompt, instructions=instructions))
        return GatewayResult(
            output=self._anthropic_text_content(response),
            input_tokens=int((response.get("usage") or {}).get("input_tokens") or 0),
            output_tokens=int((response.get("usage") or {}).get("output_tokens") or 0),
            requests=1,
        )

    def _run_anthropic_structured(
        self,
        config: ProviderRuntimeConfig,
        prompt: str,
        *,
        instructions: str,
        output_type: type[OutputT],
    ) -> GatewayResult[OutputT]:
        schema = output_type.model_json_schema()
        body = self._anthropic_body(config, prompt, instructions=instructions)
        body["tools"] = [
            {
                "name": ANTHROPIC_STRUCTURED_TOOL_NAME,
                "description": "Return the final answer as structured JSON matching the input schema.",
                "input_schema": schema,
            }
        ]
        body["tool_choice"] = {"type": "tool", "name": ANTHROPIC_STRUCTURED_TOOL_NAME}
        response = self._anthropic_request(config, body)
        output_payload = self._anthropic_tool_input(response)
        if output_payload is None:
            output_payload = self._json_from_text(self._anthropic_text_content(response))
        return GatewayResult(
            output=output_type.model_validate(output_payload),
            input_tokens=int((response.get("usage") or {}).get("input_tokens") or 0),
            output_tokens=int((response.get("usage") or {}).get("output_tokens") or 0),
            requests=1,
        )

    def _anthropic_body(self, config: ProviderRuntimeConfig, prompt: str, *, instructions: str) -> dict:
        extra_body = dict(config.extra_body or {})
        extra_body.pop("anthropic_version", None)
        extra_body.pop("anthropic_beta", None)
        max_tokens = int(extra_body.pop("max_tokens", 4096))
        body = {
            "model": config.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if instructions:
            body["system"] = instructions
        if "temperature" not in extra_body and "thinking" not in extra_body:
            body["temperature"] = 0.2
        body.update(extra_body)
        return body

    def _anthropic_request(self, config: ProviderRuntimeConfig, body: dict) -> dict:
        headers = {
            "content-type": "application/json",
            "accept": "application/json",
            "x-api-key": config.api_key,
            "anthropic-version": str((config.extra_body or {}).get("anthropic_version") or "2023-06-01"),
        }
        anthropic_beta = (config.extra_body or {}).get("anthropic_beta")
        if anthropic_beta:
            headers["anthropic-beta"] = str(anthropic_beta)
        last_error: Exception | None = None
        for attempt in range(self.network_retries + 1):
            try:
                response = requests.post(
                    self._anthropic_messages_url(config.base_url),
                    headers=headers,
                    json=body,
                    timeout=(self.connect_timeout_seconds, self.timeout_seconds),
                )
                if response.status_code in {429, 500, 502, 503, 504} and attempt < self.network_retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                if response.status_code >= 400:
                    raise RuntimeError(f"Anthropic API {response.status_code}: {self._anthropic_error_message(response)}")
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RuntimeError("Anthropic API returned an invalid response")
                return payload
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.network_retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Anthropic API request failed: {exc}") from exc
        raise RuntimeError(f"Anthropic API request failed: {last_error}")

    @staticmethod
    def _anthropic_messages_url(base_url: str) -> str:
        base = base_url.rstrip("/")
        if base.endswith("/v1/messages") or base.endswith("/messages"):
            return base
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"

    @staticmethod
    def _anthropic_text_content(response: dict) -> str:
        content = response.get("content")
        if not isinstance(content, list):
            raise RuntimeError("Anthropic API response is missing content")
        text = "\n".join(str(block.get("text") or "") for block in content if isinstance(block, dict) and block.get("type") == "text").strip()
        if not text:
            raise RuntimeError("Anthropic API response did not include text content")
        return text

    @staticmethod
    def _anthropic_tool_input(response: dict) -> dict | None:
        content = response.get("content")
        if not isinstance(content, list):
            return None
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == ANTHROPIC_STRUCTURED_TOOL_NAME:
                value = block.get("input")
                return value if isinstance(value, dict) else None
        return None

    @staticmethod
    def _json_from_text(text: str) -> dict:
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                raise
            value = json.loads(match.group(0))
        if not isinstance(value, dict):
            raise RuntimeError("Anthropic structured response must be a JSON object")
        return value

    @staticmethod
    def _anthropic_error_message(response) -> str:
        try:
            payload = response.json()
        except Exception:
            return str(getattr(response, "text", "") or "request failed")[:1000]
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            return str(error.get("message") or error)
        return str(payload)[:1000]

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
