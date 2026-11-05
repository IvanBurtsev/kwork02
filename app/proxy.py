from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ApiKey, Log, User

logger = logging.getLogger(__name__)

MODEL_PRICING: dict[str, float] = {
    "deepseek-chat": 0.5,
    "deepseek-reasoner": 1.5,
    "gpt-4o-mini": 0.6,
}


class ProxyError(RuntimeError):
    pass


class QuotaExceededError(ProxyError):
    pass


class KeyInactiveError(ProxyError):
    pass


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price = MODEL_PRICING.get(model, 0.5)
    total = prompt_tokens + completion_tokens
    return round((total / 1_000_000) * price, 6)


async def resolve_key(session: AsyncSession, api_key: str) -> tuple[ApiKey, User]:
    key_obj = await session.scalar(select(ApiKey).where(ApiKey.key == api_key))
    if key_obj is None:
        raise KeyInactiveError("Invalid API key")
    if not key_obj.is_active:
        raise KeyInactiveError("API key is inactive")
    user = await session.get(User, key_obj.user_id)
    if user is None or not user.is_active:
        raise KeyInactiveError("User is inactive")
    if key_obj.tokens_used >= key_obj.tokens_limit:
        raise QuotaExceededError(f"Token limit reached ({key_obj.tokens_used}/{key_obj.tokens_limit})")
    return key_obj, user


async def proxy_chat_completion(
    session: AsyncSession,
    api_key_obj: ApiKey,
    user: User,
    body: dict[str, Any],
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    request_body = {
        "model": body.get("model", settings.default_model),
        "messages": body.get("messages", []),
        "temperature": body.get("temperature", 0.7),
        "max_tokens": body.get("max_tokens", 4096),
        "stream": False,
    }
    url = f"{settings.api_base_url}/v1/chat/completions"

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as client:
        try:
            async with client.post(url, json=request_body, headers=headers) as resp:
                data = await resp.json()
                if resp.status != 200:
                    raise ProxyError(
                        f"Provider error {resp.status}: {data.get('error', {}).get('message', str(data))[:200]}"
                    )
                usage = data.get("usage", {})
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                total_tokens = usage.get("total_tokens", 0)
                model = data.get("model", request_body["model"])
                cost = estimate_cost(model, prompt_tokens, completion_tokens)
                api_key_obj.tokens_used += total_tokens
                log = Log(
                    api_key_id=api_key_obj.id,
                    user_id=user.id,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    cost=cost,
                    status="success",
                )
                session.add(log)
                await session.commit()
                return {
                    "id": data.get("id"),
                    "model": model,
                    "choices": data.get("choices", []),
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": total_tokens,
                        "cost": cost,
                    },
                }
        except aiohttp.ClientError as exc:
            raise ProxyError(f"Provider unreachable: {exc}") from exc


async def proxy_completion_demo(messages: list[dict[str, str]]) -> dict[str, Any]:
    import time
    time.sleep(0.3)
    prompt = messages[-1].get("content", "") if messages else ""
    return {
        "id": "demo-0000000000",
        "model": settings.default_model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": f"[DEMO] \u042d\u0445\u043e \u043e\u0442\u0432\u0435\u0442 \u043d\u0430: {prompt[:100]}.\n\n\u042d\u0442\u043e \u0442\u0435\u0441\u0442\u043e\u0432\u044b\u0439 \u0440\u0435\u0436\u0438\u043c. \u0414\u043e\u0431\u0430\u0432\u044c\u0442\u0435 DEEPSEEK_API_KEY \u0432 .env \u0434\u043b\u044f \u0440\u0435\u0430\u043b\u044c\u043d\u044b\u0445 \u0437\u0430\u043f\u0440\u043e\u0441\u043e\u0432.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": len(prompt),
            "completion_tokens": 30,
            "total_tokens": len(prompt) + 30,
            "cost": 0.0,
        },
    }