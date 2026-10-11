"""Gotcha verify providers: claude (default when keyed), gemini, openai.

Hard vetoes and the four gates stay in gotcha.py. This module only judges
*already-proposed* candidates — it is not a free-form deception / linguistics
pass and must not be used as one. GOTCHA_VERIFY_PROVIDER=auto picks Claude when
ANTHROPIC_API_KEY is set; Gemini remains the multimodal forensics engine.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from app.config import settings
from app.engines.contextual_logic import _generation_config, _parse_json_payload, _post_gemini
from app.practice_packs import pack_verify_appendix


def resolve_verify_provider() -> str:
    raw = (settings.gotcha_verify_provider or "auto").strip().lower()
    if raw in {"claude", "anthropic"}:
        return "claude" if settings.anthropic_api_key else "gemini"
    if raw in {"openai", "gpt"}:
        return "openai" if settings.openai_api_key else "gemini"
    if raw == "gemini":
        return "gemini"
    # auto
    if settings.anthropic_api_key:
        return "claude"
    if settings.gemini_api_key:
        return "gemini"
    return "none"


def ask_verify(
    candidates: list[dict[str, Any]],
    *,
    verify_prompt: str,
    verify_schema: dict[str, Any],
    first_fail,
) -> dict[int, dict[str, Any]]:
    if not candidates:
        return {}
    provider = resolve_verify_provider()
    appendix = pack_verify_appendix()
    blob = "\n\n".join(
        f"[{idx}] CLAIM ({item.get('claim_source')}): “{item.get('claim_quote')}”\n"
        f"EVIDENCE ({item.get('evidence_source')}): “{item.get('evidence_quote')}”"
        for idx, item in enumerate(candidates)
    )
    prompt = verify_prompt.format(candidates=blob) + "\n\n" + appendix
    if provider == "claude":
        return _claude_verify(prompt, first_fail)
    if provider == "openai":
        return _openai_verify(prompt, first_fail)
    if provider == "gemini" and settings.gemini_api_key:
        return _gemini_verify(prompt, verify_schema, first_fail)
    return {}


def _gemini_verify(prompt: str, verify_schema: dict[str, Any], first_fail) -> dict[int, dict[str, Any]]:
    try:
        from app.gemini_tasks import TASK_C_CONTRA, task_config

        contra = task_config(TASK_C_CONTRA)
        body = _post_gemini(
            {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": _generation_config(
                    verify_schema, contra.thinking_level, model=contra.primary
                ),
            },
            timeout=max(settings.gemini_timeout_s, 45),
            task=TASK_C_CONTRA,
            step="gotcha_verify",
            chunk_id="verify",
        )
        parsed = _parse_json_payload(body)
    except Exception:
        return {}
    return _index_results(parsed, first_fail)


def _claude_verify(prompt: str, first_fail) -> dict[int, dict[str, Any]]:
    key = (settings.anthropic_api_key or "").strip()
    if not key:
        return {}
    model = settings.claude_model or "claude-sonnet-4-20250514"
    payload = {
        "model": model,
        "max_tokens": 4096,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": (
                    prompt
                    + "\n\nReturn ONLY a JSON object with key \"results\" "
                    "(array of per-candidate verification objects with index, pass, "
                    "same_fact, directionality, materiality, textual_grounding, "
                    "optional contradiction_type and failed_step)."
                ),
            }
        ],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=max(settings.gemini_timeout_s, 60)) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return {}
    text = _anthropic_text(body)
    parsed = _extract_json_obj(text)
    if not parsed:
        return {}
    return _index_results(parsed, first_fail)


def _openai_verify(prompt: str, first_fail) -> dict[int, dict[str, Any]]:
    key = (settings.openai_api_key or "").strip()
    if not key:
        return {}
    model = settings.openai_verify_model or "gpt-4.1"
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "user",
                "content": prompt
                + '\n\nReturn JSON with key "results" as specified in the instructions.',
            }
        ],
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=max(settings.gemini_timeout_s, 60)) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return {}
    try:
        text = body["choices"][0]["message"]["content"]
        parsed = json.loads(text)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return {}
    return _index_results(parsed, first_fail)


def _anthropic_text(body: dict[str, Any]) -> str:
    parts = []
    for block in body.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "\n".join(parts).strip()


def _extract_json_obj(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _index_results(parsed: dict[str, Any], first_fail) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for item in parsed.get("results") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if not item.get("pass"):
            item["failed_step"] = item.get("failed_step") or first_fail(item)
        out[idx] = item
    return out
