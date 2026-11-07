"""
filmlog/ai.py — Universal AI caller for FilmLog.

Supports Gemini (default, 100% free tier) and Anthropic (legacy).
Uses urllib.request so it has zero external dependencies and runs seamlessly on Vercel.
"""

import os
import json
import urllib.request
import urllib.error
import re
from typing import Optional


GEMINI_MODELS = [
    "gemini-flash-lite-latest",
    "gemini-3.1-flash-lite-preview",
    "gemini-flash-latest",
]


def call_ai(prompt: str, max_tokens: int = 2048, system_instruction: Optional[str] = None) -> str:
    """
    Call AI with automatic provider selection:
    1. Gemini API (if GEMINI_API_KEY is set)
    2. Anthropic API (if ANTHROPIC_API_KEY is set)
    """
    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        return _call_gemini(prompt, gemini_key, max_tokens, system_instruction)

    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    if anthropic_key:
        return _call_anthropic(prompt, anthropic_key, max_tokens)

    raise RuntimeError(
        "No AI API key found! Please set GEMINI_API_KEY in your environment or .env file."
    )


def _call_gemini(prompt: str, api_key: str, max_tokens: int = 2048, system_instruction: Optional[str] = None) -> str:
    """Call Gemini generateContent API via urllib."""
    last_err = None

    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": 0.7,
            }
        }
        if system_instruction:
            payload["systemInstruction"] = {
                "parts": [{"text": system_instruction}]
            }

        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                candidates = res.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        return parts[0].get("text", "").strip()
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            last_err = f"HTTP {e.code}: {err_body}"
            # If 503 or 404, try next model in fallback list
            if e.code in (404, 503, 429):
                continue
            raise RuntimeError(f"Gemini API error ({model}): {last_err}") from e
        except Exception as e:
            last_err = str(e)
            continue

    raise RuntimeError(f"All Gemini models failed. Last error: {last_err}")


def _call_anthropic(prompt: str, api_key: str, max_tokens: int = 2048) -> str:
    """Call Anthropic as fallback."""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text.strip()
