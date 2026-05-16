# -*- coding: utf-8 -*-
"""LiteLLM generation-parameter compatibility helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# Kimi K2.6 is consumed through Moonshot's OpenAI-compatible API in this
# repository. Official references:
# - https://platform.kimi.ai/docs/guide/kimi-k2-6-quickstart
# - https://platform.moonshot.ai/docs/guide/compatibility#parameters-differences-in-request-body
# - https://huggingface.co/moonshotai/Kimi-K2.6
# - https://docs.litellm.ai/docs/providers/openai_compatible
_FIXED_TEMPERATURE_LITELLM_MODELS: Dict[str, Dict[str, float]] = {
    "kimi-k2.6": {
        "thinking": 1.0,
        "non_thinking": 0.6,
    },
}


@dataclass(frozen=True)
class TemperatureDirective:
    """Request-scoped temperature strategy for one LiteLLM model call."""

    temperature: Optional[float] = None
    omit_temperature: bool = False
    reason: str = ""


def _resolve_litellm_model_list_entry(
    model: str,
    model_list: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Return the Router model_list entry matching the configured alias."""
    normalized_model = (model or "").strip()
    if not normalized_model or not model_list:
        return None

    for entry in model_list:
        model_name = str(entry.get("model_name") or "").strip()
        if not model_name:
            params = entry.get("litellm_params", {}) or {}
            model_name = str(params.get("model") or "").strip()
        if model_name == normalized_model:
            return entry
    return None


def resolve_litellm_wire_model(
    model: str,
    model_list: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Resolve a router alias to its underlying LiteLLM wire model."""
    normalized_model = (model or "").strip()
    if not normalized_model or not model_list:
        return normalized_model

    model_entry = _resolve_litellm_model_list_entry(normalized_model, model_list)
    if not model_entry:
        return normalized_model

    params = model_entry.get("litellm_params", {}) or {}
    wire_model = str(params.get("model") or "").strip()
    if wire_model:
        return wire_model
    return normalized_model


def _extract_thinking_config(payload: Optional[Dict[str, Any]]) -> Any:
    """Extract a thinking-mode flag from LiteLLM-style request kwargs."""
    if not isinstance(payload, dict):
        return None
    extra_body = payload.get("extra_body")
    if isinstance(extra_body, dict) and "thinking" in extra_body:
        return extra_body.get("thinking")
    if "thinking" in payload:
        return payload.get("thinking")
    return None


def _parse_thinking_enabled(value: Any) -> Optional[bool]:
    """Parse thinking-mode config into True/False/unknown."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"enabled", "enable", "true", "1", "on", "thinking"}:
            return True
        if normalized in {"disabled", "disable", "false", "0", "off", "none", "non-thinking", "non_thinking"}:
            return False
        return None
    if isinstance(value, dict):
        if "enabled" in value:
            return _parse_thinking_enabled(value.get("enabled"))
        if "type" in value:
            return _parse_thinking_enabled(value.get("type"))
    return None


def resolve_litellm_thinking_enabled(
    model: str,
    model_list: Optional[List[Dict[str, Any]]] = None,
    request_overrides: Optional[Dict[str, Any]] = None,
) -> Optional[bool]:
    """Resolve whether the outgoing LiteLLM request explicitly enables thinking."""
    thinking_config = None
    model_entry = _resolve_litellm_model_list_entry(model, model_list)
    if model_entry:
        thinking_config = _extract_thinking_config(model_entry)
        entry_params = model_entry.get("litellm_params", {}) or {}
        entry_thinking_config = _extract_thinking_config(entry_params)
        if entry_thinking_config is not None:
            thinking_config = entry_thinking_config

    override_thinking_config = _extract_thinking_config(request_overrides)
    if override_thinking_config is not None:
        thinking_config = override_thinking_config
    return _parse_thinking_enabled(thinking_config)


def _model_parts(model: str) -> List[str]:
    return [part for part in re.split(r"[/:\s]+", (model or "").lower()) if part]


def _matches_model_family(model: str, family: str) -> bool:
    return any(part == family or part.startswith(f"{family}-") for part in _model_parts(model))


def _should_omit_litellm_temperature(model: str) -> bool:
    """Return whether a model family should rely on the provider default temperature."""
    return any(
        part.startswith(("gpt-5", "gpt5"))
        or part in {"o1", "o3", "o4"}
        or part.startswith(("o1-", "o3-", "o4-"))
        for part in _model_parts(model)
    )


def get_fixed_litellm_temperature(
    model: str,
    model_list: Optional[List[Dict[str, Any]]] = None,
    request_overrides: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Return a provider-mandated temperature for known strict models."""
    normalized_model = resolve_litellm_wire_model(model, model_list).lower()
    if not normalized_model:
        return None
    thinking_enabled = resolve_litellm_thinking_enabled(
        model,
        model_list=model_list,
        request_overrides=request_overrides,
    )
    for model_name, temperatures in _FIXED_TEMPERATURE_LITELLM_MODELS.items():
        if _matches_model_family(normalized_model, model_name):
            if thinking_enabled is False and temperatures.get("non_thinking") is not None:
                return temperatures["non_thinking"]
            if temperatures.get("thinking") is not None:
                return temperatures["thinking"]
            if temperatures.get("non_thinking") is not None:
                return temperatures["non_thinking"]
    return None


def resolve_litellm_temperature_directive(
    model: str,
    *,
    model_list: Optional[List[Dict[str, Any]]] = None,
    request_overrides: Optional[Dict[str, Any]] = None,
) -> TemperatureDirective:
    """Resolve the request-scoped temperature directive for a LiteLLM model."""
    fixed_temperature = get_fixed_litellm_temperature(
        model,
        model_list=model_list,
        request_overrides=request_overrides,
    )
    if fixed_temperature is not None:
        return TemperatureDirective(
            temperature=fixed_temperature,
            reason="fixed_model_temperature",
        )

    wire_model = resolve_litellm_wire_model(model, model_list)
    if _should_omit_litellm_temperature(wire_model):
        return TemperatureDirective(
            omit_temperature=True,
            reason="provider_default_temperature",
        )
    return TemperatureDirective()


def normalize_litellm_temperature(
    model: str,
    temperature: Optional[float],
    *,
    default: float = 0.7,
    model_list: Optional[List[Dict[str, Any]]] = None,
    request_overrides: Optional[Dict[str, Any]] = None,
) -> float:
    """Return the legacy float temperature normalization for callers that need it."""
    fixed_temperature = get_fixed_litellm_temperature(
        model,
        model_list=model_list,
        request_overrides=request_overrides,
    )
    if fixed_temperature is not None:
        return fixed_temperature
    if temperature is None:
        return default
    return float(temperature)


def apply_litellm_generation_params(
    call_kwargs: Dict[str, Any],
    model: str,
    temperature: Optional[float],
    *,
    default_temperature: float = 0.7,
    model_list: Optional[List[Dict[str, Any]]] = None,
    request_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return kwargs with model-compatible generation parameters applied."""
    updated = dict(call_kwargs)
    effective_overrides = request_overrides if request_overrides is not None else updated
    directive = resolve_litellm_temperature_directive(
        model,
        model_list=model_list,
        request_overrides=effective_overrides,
    )
    if directive.omit_temperature:
        updated.pop("temperature", None)
    elif directive.temperature is not None:
        updated["temperature"] = directive.temperature
    else:
        updated["temperature"] = default_temperature if temperature is None else float(temperature)
    return updated
