"""Provider-agnostic JSON completion: one stateless call, no memory between calls.

The model per task is configuration, so a step can move between providers (DeepSeek for
development; a company-approved provider in production) without code changes.
"""
import json
import time

import requests

from . import config

PROVIDERS = {  # OpenAI-compatible chat completions endpoints
    "deepseek": ("https://api.deepseek.com/chat/completions", "DEEPSEEK_API_KEY"),
}
MODELS = {
    "classify_reply": ("deepseek", "deepseek-chat"),
    "brief": ("deepseek", "deepseek-chat"),
    "investigate": ("deepseek", "deepseek-chat"),
    "decide_action": ("deepseek", "deepseek-chat"),  # scripts/eval_actions.py
}


def chat(task: str, messages: list[dict], tools: list[dict] | None = None,
         json_mode: bool = True) -> tuple[dict, dict]:
    """One stateless call: returns (assistant message, call metadata for llm_calls)."""
    provider, model = MODELS[task]
    url, key_env = PROVIDERS[provider]
    payload = {"model": model, "temperature": 0, "messages": messages}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if tools:
        payload["tools"] = tools
    t0 = time.monotonic()
    resp = requests.post(url, timeout=90, json=payload,
                         headers={"Authorization": f"Bearer {config.require(key_env)}"})
    meta = {"task": task, "ms": round((time.monotonic() - t0) * 1000), "model": model}
    if resp.status_code != 200:
        raise RuntimeError(f"{provider} {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    usage = body.get("usage", {})
    meta.update(model=body.get("model", model), prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"))
    return body["choices"][0]["message"], meta


def complete_json(task: str, system: str, user: str) -> tuple[dict, dict]:
    """Returns (parsed JSON, call metadata). Raises on HTTP or JSON errors."""
    msg, meta = chat(task, [{"role": "system", "content": system},
                            {"role": "user", "content": user}])
    return json.loads(msg["content"]), meta
