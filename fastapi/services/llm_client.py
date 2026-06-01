import httpx
from config import settings

_PROVIDERS = {
    "openrouter": lambda: (settings.openrouter_api_url, settings.openrouter_api_key, settings.openrouter_model),
    "gemini":     lambda: (settings.gemini_api_url,     settings.gemini_api_key,     settings.gemini_model),
    "groq":       lambda: (settings.groq_api_url,       settings.groq_api_key,       settings.groq_model),
}


async def call_llm(prompt: str) -> str:
    provider = settings.use_provider
    if provider not in _PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}. Valid: {list(_PROVIDERS)}")
    url, api_key, model = _PROVIDERS[provider]()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 250,
        "temperature": 0.2,
    }
    async with httpx.AsyncClient(timeout=settings.llm_agent_timeout) as client:
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
