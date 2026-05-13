"""Model name validators for each provider.

Only validates model names - does NOT enforce limits.
Let LLM providers use their own defaults for unspecified params.
"""

VALID_MODELS = {
    "openai": [
        # GPT-5 series (2025)
        "gpt-5.2",
        "gpt-5.1",
        "gpt-5",
        "gpt-5-mini",
        "gpt-5-nano",
        # GPT-4.1 series (2025)
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        # o-series reasoning models
        "o4-mini",
        "o3",
        "o3-mini",
        "o1",
        "o1-preview",
        # GPT-4o series (legacy but still supported)
        "gpt-4o",
        "gpt-4o-mini",
    ],
    "anthropic": [
        # Claude 4.5 series (2025)
        "claude-opus-4-5",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
        # Claude 4.x series
        "claude-opus-4-1-20250805",
        "claude-sonnet-4-20250514",
        # Claude 3.7 series
        "claude-3-7-sonnet-20250219",
        # Claude 3.5 series (legacy)
        "claude-3-5-haiku-20241022",
        "claude-3-5-sonnet-20241022",
    ],
    "google": [
        # Gemini 3 series (preview)
        "gemini-3-pro-preview",
        "gemini-3-flash-preview",
        # Gemini 2.5 series
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        # Gemini 2.0 series
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
    ],
    "xai": [
        # Grok 4.1 series
        "grok-4-1-fast",
        "grok-4-1-fast-reasoning",
        "grok-4-1-fast-non-reasoning",
        # Grok 4 series
        "grok-4",
        "grok-4-0709",
        "grok-4-fast-reasoning",
        "grok-4-fast-non-reasoning",
    ],
    "minimax": [
        # MiniMax M2 series
        "MiniMax-M2.7",
        "MiniMax-M2.7-highspeed",
        "MiniMax-M2.5",
        "MiniMax-M2.5-highspeed",
        "MiniMax-M2.1",
        "MiniMax-M2.1-highspeed",
        "MiniMax-M2",
    ],
    "qwen": [
        # Qwen commercial models
        "qwen3-max",
        "qwen3-max-preview",
        "qwen-max",
        "qwen-max-latest",
        "qwen3.5-plus",
        "qwen-plus",
        "qwen-plus-latest",
        "qwen3.5-flash",
        "qwen-flash",
        "qwen-turbo",
        "qwen-turbo-latest",
        # Qwen coder models
        "qwen3-coder-plus",
        "qwen3-coder-flash",
        # QwQ reasoning
        "qwq-plus",
        # Qwen open-source models
        "qwen3.5-397b-a17b",
        "qwen3.5-120b-a10b",
        "qwen3.5-27b",
        "qwen3-235b-a22b",
        "qwen3-32b",
        "qwen3-30b-a3b",
        "qwen3-14b",
        "qwen3-8b",
    ],
    "zhipu": [
        # GLM-5 series
        "glm-5.1",
        "glm-5.1-flash",
        "glm-5",
        # GLM-4.5 series
        "glm-4.5",
        # GLM-4 series
        "glm-4-plus",
        "glm-4-air",
        "glm-4-airx",
        "glm-4-long",
        "glm-4-flash",
        "glm-4-flashx",
        "glm-4",
    ],
}


def validate_model(provider: str, model: str) -> bool:
    """Check if model name is valid for the given provider.

    For ollama, openrouter - any model is accepted.
    """
    provider_lower = provider.lower()

    if provider_lower in ("ollama", "openrouter"):
        return True

    if provider_lower not in VALID_MODELS:
        return True

    return model in VALID_MODELS[provider_lower]
