"""Shared model catalog for CLI selections and validation."""

from __future__ import annotations

from typing import Dict, List, Tuple

ModelOption = Tuple[str, str]
ProviderModeOptions = Dict[str, Dict[str, List[ModelOption]]]


MODEL_OPTIONS: ProviderModeOptions = {
    "openai": {
        "quick": [
            ("GPT-5.4 Mini - Fast, strong coding and tool use", "gpt-5.4-mini"),
            ("GPT-5.4 Nano - Cheapest, high-volume tasks", "gpt-5.4-nano"),
            ("GPT-5.4 - Latest frontier, 1M context", "gpt-5.4"),
            ("GPT-4.1 - Smartest non-reasoning model", "gpt-4.1"),
        ],
        "deep": [
            ("GPT-5.4 - Latest frontier, 1M context", "gpt-5.4"),
            ("GPT-5.2 - Strong reasoning, cost-effective", "gpt-5.2"),
            ("GPT-5.4 Mini - Fast, strong coding and tool use", "gpt-5.4-mini"),
            ("GPT-5.4 Pro - Most capable, expensive ($30/$180 per 1M tokens)", "gpt-5.4-pro"),
        ],
    },
    "anthropic": {
        "quick": [
            ("Claude Sonnet 4.6 - Best speed and intelligence balance", "claude-sonnet-4-6"),
            ("Claude Haiku 4.5 - Fast, near-instant responses", "claude-haiku-4-5"),
            ("Claude Sonnet 4.5 - Agents and coding", "claude-sonnet-4-5"),
        ],
        "deep": [
            ("Claude Opus 4.6 - Most intelligent, agents and coding", "claude-opus-4-6"),
            ("Claude Opus 4.5 - Premium, max intelligence", "claude-opus-4-5"),
            ("Claude Sonnet 4.6 - Best speed and intelligence balance", "claude-sonnet-4-6"),
            ("Claude Sonnet 4.5 - Agents and coding", "claude-sonnet-4-5"),
        ],
    },
    "google": {
        "quick": [
            ("Gemini 3 Flash - Next-gen fast", "gemini-3-flash-preview"),
            ("Gemini 2.5 Flash - Balanced, stable", "gemini-2.5-flash"),
            ("Gemini 3.1 Flash Lite - Most cost-efficient", "gemini-3.1-flash-lite-preview"),
            ("Gemini 2.5 Flash Lite - Fast, low-cost", "gemini-2.5-flash-lite"),
        ],
        "deep": [
            ("Gemini 3.1 Pro - Reasoning-first, complex workflows", "gemini-3.1-pro-preview"),
            ("Gemini 3 Flash - Next-gen fast", "gemini-3-flash-preview"),
            ("Gemini 2.5 Pro - Stable pro model", "gemini-2.5-pro"),
            ("Gemini 2.5 Flash - Balanced, stable", "gemini-2.5-flash"),
        ],
    },
    "xai": {
        "quick": [
            ("Grok 4.1 Fast (Non-Reasoning) - Speed optimized, 2M ctx", "grok-4-1-fast-non-reasoning"),
            ("Grok 4 Fast (Non-Reasoning) - Speed optimized", "grok-4-fast-non-reasoning"),
            ("Grok 4.1 Fast (Reasoning) - High-performance, 2M ctx", "grok-4-1-fast-reasoning"),
        ],
        "deep": [
            ("Grok 4 - Flagship model", "grok-4-0709"),
            ("Grok 4.1 Fast (Reasoning) - High-performance, 2M ctx", "grok-4-1-fast-reasoning"),
            ("Grok 4 Fast (Reasoning) - High-performance", "grok-4-fast-reasoning"),
            ("Grok 4.1 Fast (Non-Reasoning) - Speed optimized, 2M ctx", "grok-4-1-fast-non-reasoning"),
        ],
    },
    "openrouter": {
        "quick": [
            ("NVIDIA Nemotron 3 Nano 30B (free)", "nvidia/nemotron-3-nano-30b-a3b:free"),
            ("Z.AI GLM 4.5 Air (free)", "z-ai/glm-4.5-air:free"),
        ],
        "deep": [
            ("Z.AI GLM 4.5 Air (free)", "z-ai/glm-4.5-air:free"),
            ("NVIDIA Nemotron 3 Nano 30B (free)", "nvidia/nemotron-3-nano-30b-a3b:free"),
        ],
    },
    "minimax": {
        "quick": [
            ("MiniMax-M2.7 Highspeed - Fast, 204K context", "MiniMax-M2.7-highspeed"),
            ("MiniMax-M2.5 Highspeed - Balanced, fast", "MiniMax-M2.5-highspeed"),
            ("MiniMax-M2.1 Highspeed - Coding-focused, fast", "MiniMax-M2.1-highspeed"),
            ("MiniMax-M2 - Agentic capabilities", "MiniMax-M2"),
        ],
        "deep": [
            ("MiniMax-M2.7 - Recursive self-improvement, 204K ctx", "MiniMax-M2.7"),
            ("MiniMax-M2.5 - Peak performance, 204K ctx", "MiniMax-M2.5"),
            ("MiniMax-M2.1 - Multi-language coding", "MiniMax-M2.1"),
            ("MiniMax-M2 - Agentic capabilities", "MiniMax-M2"),
        ],
    },
    "qwen": {
        "quick": [
            ("Qwen3.5 Flash - Fast, low-cost", "qwen3.5-flash"),
            ("Qwen Flash - Balanced speed", "qwen-flash"),
            ("Qwen Turbo - High-throughput", "qwen-turbo"),
            ("Qwen Plus - Strong performance", "qwen-plus"),
        ],
        "deep": [
            ("Qwen3 Max - Flagship, strongest reasoning", "qwen3-max"),
            ("Qwen3.5 Plus - Latest balanced model", "qwen3.5-plus"),
            ("Qwen Plus - Strong all-around", "qwen-plus"),
            ("QwQ Plus - Deep reasoning", "qwq-plus"),
        ],
    },
    "zhipu": {
        "quick": [
            ("GLM-4 Flash - Fast, free tier", "glm-4-flash"),
            ("GLM-4 FlashX - Enhanced speed", "glm-4-flashx"),
            ("GLM-4 Air - Balanced", "glm-4-air"),
            ("GLM-4 AirX - Enhanced balanced", "glm-4-airx"),
        ],
        "deep": [
            ("GLM-5 - Latest flagship, 200K context", "glm-5"),
            ("GLM-4.5 - Unified agentic model", "glm-4.5"),
            ("GLM-4 Plus - Enhanced capabilities", "glm-4-plus"),
            ("GLM-4 - Reliable general purpose", "glm-4"),
        ],
    },
    "ollama": {
        "quick": [
            ("Qwen3:latest (8B, local)", "qwen3:latest"),
            ("GPT-OSS:latest (20B, local)", "gpt-oss:latest"),
            ("GLM-4.7-Flash:latest (30B, local)", "glm-4.7-flash:latest"),
        ],
        "deep": [
            ("GLM-4.7-Flash:latest (30B, local)", "glm-4.7-flash:latest"),
            ("GPT-OSS:latest (20B, local)", "gpt-oss:latest"),
            ("Qwen3:latest (8B, local)", "qwen3:latest"),
        ],
    },
}


def get_model_options(provider: str, mode: str) -> List[ModelOption]:
    """Return shared model options for a provider and selection mode."""
    return MODEL_OPTIONS[provider.lower()][mode]


def get_known_models() -> Dict[str, List[str]]:
    """Build known model names from the shared CLI catalog."""
    return {
        provider: sorted(
            {
                value
                for options in mode_options.values()
                for _, value in options
            }
        )
        for provider, mode_options in MODEL_OPTIONS.items()
    }
