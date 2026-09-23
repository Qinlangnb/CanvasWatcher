"""Server-owned endpoints; clients cannot supply an arbitrary destination."""

PROVIDERS = {
    "kimi": ("Kimi · China", "https://api.moonshot.cn/v1", "openai"),
    "deepseek": ("DeepSeek (DS)", "https://api.deepseek.com/v1", "openai"),
    "qwen": ("Qwen · Beijing", "https://dashscope.aliyuncs.com/compatible-mode/v1", "openai"),
    "qwen_sg": ("Qwen · Singapore", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "openai"),
    "qwen_us": ("Qwen · US", "https://dashscope-us.aliyuncs.com/compatible-mode/v1", "openai"),
    "glm": ("GLM · BigModel", "https://open.bigmodel.cn/api/paas/v4", "openai"),
    "glm_global": ("GLM · Z.ai", "https://api.z.ai/api/paas/v4", "openai"),
    "gpt": ("GPT · OpenAI", "https://api.openai.com/v1", "openai"),
    "claude": ("Claude · Anthropic", "https://api.anthropic.com/v1", "anthropic"),
    "ollama": ("Ollama · Docker host", "http://host.docker.internal:11434/v1", "local"),
    "ollama_local": ("Ollama · Native backend on localhost", "http://127.0.0.1:11434/v1", "local"),
}


def provider_choices() -> list[dict]:
    return [{"id": key, "label": label, "base_url": base, "requires_key": protocol != "local"}
            for key, (label, base, protocol) in PROVIDERS.items()]


def provider_for_url(base: str) -> str:
    return next((key for key, (_, url, _) in PROVIDERS.items() if url == base), "")
