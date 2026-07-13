"""Optional LLM client: OpenAI-compatible llama-server on 127.0.0.1:8080.

Every call degrades silently when the server is unreachable — the tracer must
never fail because the LLM is absent. Uses only stdlib urllib; no network at
import time. All requests are local (127.0.0.1); no external egress.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Optional

from .config import Config

log = logging.getLogger("code_tracer.llm")

_PROBE_CACHE: dict[str, bool] = {}


def _no_think(prompt: str, cfg: Config) -> tuple[str, dict]:
    """Return (prompt, extra_body) applying the configured no-think mode."""
    extra: dict = {}
    mode = cfg.no_think_mode
    if not cfg.embed_disable_thinking:
        return prompt, extra
    if mode in ("suffix", "both"):
        prompt = prompt + " /no_think"
    if mode in ("kwargs", "both"):
        extra["chat_template_kwargs"] = {"enable_thinking": False}
    return prompt, extra


def server_available(cfg: Config, timeout: float = 0.4) -> bool:
    base = cfg.llm_url.rstrip("/")
    if base in _PROBE_CACHE:
        return _PROBE_CACHE[base]
    url = base + "/models"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = resp.status == 200
    except Exception:
        ok = False
    _PROBE_CACHE[base] = ok
    return ok


def clear_probe_cache() -> None:
    _PROBE_CACHE.clear()


def chat(
    prompt: str,
    cfg: Config,
    *,
    system: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 256,
    timeout: float = 30.0,
) -> Optional[str]:
    """Single-turn completion. Returns text or None on any failure."""
    if not server_available(cfg):
        return None
    prompt, extra = _no_think(prompt, cfg)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {
        "model": cfg.llm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        **extra,
    }
    data = json.dumps(body).encode("utf-8")
    url = cfg.llm_url.rstrip("/") + "/chat/completions"
    try:
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        text = payload["choices"][0]["message"]["content"]
        # strip any leaked <think>...</think>
        if "<think>" in text and "</think>" in text:
            text = text.split("</think>", 1)[1]
        return text.strip()
    except Exception as exc:  # noqa: BLE001
        log.debug("llm chat failed: %s", exc)
        return None
