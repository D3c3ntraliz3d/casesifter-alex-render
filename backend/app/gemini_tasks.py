"""Per-task Gemini model config (extract / entity / contra / visual).

Two lanes (selected from scan profile):

- **Exhibit / forensic** — ``TASK_*`` primary/backup/thinking (best available).
- **Non-exhibit / court_filing** — ``GEMINI_LIGHT_*`` (cheapest/lightest).

Env overrides (examples)::

    TASK_D_VISUAL_PRIMARY=gemini-3.8-flash
    TASK_D_VISUAL_THINKING_LEVEL=medium
    GEMINI_LIGHT_PRIMARY=gemini-3.5-flash-lite
    GEMINI_LIGHT_BACKUP=gemini-3.5-flash-lite
    GEMINI_LIGHT_THINKING_LEVEL=minimal
    GEMINI_SERVICE_TIER=standard   # or priority (Task A only)
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

from app.config import gemini_model_is_valid, settings

logger = logging.getLogger(__name__)

TASK_A_EXTRACT = "extract"
TASK_B_ENTITY = "entity"
TASK_C_CONTRA = "contra"
TASK_D_VISUAL = "visual"

VALID_TASKS = frozenset(
    {TASK_A_EXTRACT, TASK_B_ENTITY, TASK_C_CONTRA, TASK_D_VISUAL}
)
VALID_THINKING_LEVELS = frozenset({"minimal", "low", "medium", "high"})
# gemini-3.8-flash rejects thinking_level=minimal (supports low/medium/high only).
FLASH_38_THINKING_LEVELS = frozenset({"low", "medium", "high"})
VALID_SERVICE_TIERS = frozenset({"standard", "priority", "flex"})

# models.get probe
_MODELS_GET_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}"

# Default True so startup probes / chat / callers without a scan profile keep
# the exhibit (best) matrix. Pipeline + contextual_logic set False for court_filing.
_exhibit_models: ContextVar[bool] = ContextVar("gemini_exhibit_models", default=True)


@dataclass(frozen=True)
class GeminiTaskConfig:
    task: str
    primary: str
    backup: str
    thinking_level: str
    lane: str = "exhibit"  # exhibit | light


def exhibit_models_for_profile(scan_profile: str | None) -> bool:
    """True when the scan lane should burn best models (forensic exhibits)."""
    return (scan_profile or "").strip().lower() != "court_filing"


def current_exhibit_models() -> bool:
    return bool(_exhibit_models.get())


@contextmanager
def gemini_model_lane(*, exhibit: bool) -> Iterator[None]:
    """Bind best vs light model matrix for the current scan / AI call stack."""
    token = _exhibit_models.set(bool(exhibit))
    try:
        yield
    finally:
        _exhibit_models.reset(token)


def _level(raw: str | None, default: str) -> str:
    level = (raw or default).strip().lower()
    if level not in VALID_THINKING_LEVELS:
        raise ValueError(
            f"invalid thinking_level={raw!r}; allowed: {sorted(VALID_THINKING_LEVELS)}"
        )
    return level


def _is_gemini_38_flash(model: str) -> bool:
    return "gemini-3.8-flash" in (model or "").strip().lower()


def thinking_level_for_model(model: str, level: str) -> str:
    """Clamp/validate thinking_level for the target model.

    gemini-3.8-flash does not accept ``minimal`` — only low/medium/high.
    """
    normalized = _level(level, level)
    if _is_gemini_38_flash(model) and normalized == "minimal":
        raise ValueError(
            f"thinking_level=minimal is not supported on {model}; "
            f"use one of {sorted(FLASH_38_THINKING_LEVELS)}"
        )
    return normalized


def _model(raw: str | None, *, label: str) -> str:
    model = (raw or "").strip()
    if not gemini_model_is_valid(model):
        raise ValueError(
            f"{label}={raw!r} failed ^gemini-[\\w.-]+$ validation"
        )
    return model


def _task(
    task: str,
    *,
    primary: str,
    backup: str,
    thinking_level: str,
    lane: str,
) -> GeminiTaskConfig:
    # Validate against both primary and backup — either may receive the level.
    thinking_level_for_model(primary, thinking_level)
    thinking_level_for_model(backup, thinking_level)
    return GeminiTaskConfig(
        task=task,
        primary=primary,
        backup=backup,
        thinking_level=thinking_level,
        lane=lane,
    )


def task_config(task: str, *, exhibit: bool | None = None) -> GeminiTaskConfig:
    """Return primary/backup/thinking for a task.

    ``exhibit=True`` (default / forensic profile) uses ``TASK_*`` best models.
    ``exhibit=False`` (court_filing / non-exhibit) uses ``GEMINI_LIGHT_*``.
    When ``exhibit`` is omitted, the active ``gemini_model_lane`` context is used.
    """
    key = (task or "").strip().lower()
    if key not in VALID_TASKS:
        raise ValueError(f"unknown gemini task={task!r}")
    use_exhibit = current_exhibit_models() if exhibit is None else bool(exhibit)
    if not use_exhibit:
        return _task(
            key,
            primary=_model(settings.gemini_light_primary, label="GEMINI_LIGHT_PRIMARY"),
            backup=_model(settings.gemini_light_backup, label="GEMINI_LIGHT_BACKUP"),
            thinking_level=_level(settings.gemini_light_thinking_level, "minimal"),
            lane="light",
        )
    if key == TASK_A_EXTRACT:
        return _task(
            key,
            primary=_model(settings.task_a_extract_primary, label="TASK_A_EXTRACT_PRIMARY"),
            backup=_model(settings.task_a_extract_backup, label="TASK_A_EXTRACT_BACKUP"),
            thinking_level=_level(settings.task_a_extract_thinking_level, "minimal"),
            lane="exhibit",
        )
    if key == TASK_B_ENTITY:
        return _task(
            key,
            primary=_model(settings.task_b_entity_primary, label="TASK_B_ENTITY_PRIMARY"),
            backup=_model(settings.task_b_entity_backup, label="TASK_B_ENTITY_BACKUP"),
            thinking_level=_level(settings.task_b_entity_thinking_level, "minimal"),
            lane="exhibit",
        )
    if key == TASK_C_CONTRA:
        return _task(
            key,
            primary=_model(settings.task_c_contra_primary, label="TASK_C_CONTRA_PRIMARY"),
            backup=_model(settings.task_c_contra_backup, label="TASK_C_CONTRA_BACKUP"),
            thinking_level=_level(settings.task_c_contra_thinking_level, "minimal"),
            lane="exhibit",
        )
    return _task(
        key,
        primary=_model(settings.task_d_visual_primary, label="TASK_D_VISUAL_PRIMARY"),
        backup=_model(settings.task_d_visual_backup, label="TASK_D_VISUAL_BACKUP"),
        thinking_level=_level(settings.task_d_visual_thinking_level, "minimal"),
        lane="exhibit",
    )


def all_task_configs(*, exhibit: bool = True) -> list[GeminiTaskConfig]:
    return [task_config(t, exhibit=exhibit) for t in sorted(VALID_TASKS)]


def configured_model_ids() -> list[str]:
    """Unique primary + backup model ids across exhibit and light lanes."""
    seen: list[str] = []
    for exhibit in (True, False):
        for cfg in all_task_configs(exhibit=exhibit):
            for mid in (cfg.primary, cfg.backup):
                if mid not in seen:
                    seen.append(mid)
    return seen


def service_tier_for_task(task: str) -> str:
    """Return request ``service_tier`` value, or empty string to omit (standard).

    Confirmed on **generateContent** (not Interactions-only):
    - REST ``GenerateContentRequest.serviceTier`` enum: standard | flex | priority
      https://ai.google.dev/api/generate-content
    - Priority guide curl posts ``service_tier`` to ``:generateContent``
      https://ai.google.dev/gemini-api/docs/generate-content/priority-inference

    JSON body uses snake_case ``service_tier``. Omitted = standard.
    Priority / flex testing is gated to Task A only (scans stay on generateContent, not Batch).
    Light (non-exhibit) lane never requests priority — keep pleadings cheap.
    """
    if not current_exhibit_models():
        return ""
    raw = (settings.gemini_service_tier or "standard").strip().lower()
    if raw not in VALID_SERVICE_TIERS:
        raise ValueError(
            f"GEMINI_SERVICE_TIER={settings.gemini_service_tier!r} invalid; "
            f"allowed: {sorted(VALID_SERVICE_TIERS)}"
        )
    if (task or "").strip().lower() != TASK_A_EXTRACT:
        return ""
    if raw == "standard":
        return ""
    return raw


def validate_gemini_task_settings() -> list[str]:
    """Return human-readable errors for invalid thinking levels / models / tier."""
    errors: list[str] = []
    try:
        tier = (settings.gemini_service_tier or "standard").strip().lower()
        if tier not in VALID_SERVICE_TIERS:
            errors.append(
                f"GEMINI_SERVICE_TIER={settings.gemini_service_tier!r} "
                f"not in {sorted(VALID_SERVICE_TIERS)}"
            )
    except Exception as exc:
        errors.append(str(exc))
    for exhibit in (True, False):
        for task in sorted(VALID_TASKS):
            try:
                task_config(task, exhibit=exhibit)
            except ValueError as exc:
                errors.append(str(exc))
    return errors


def probe_configured_models(*, timeout_s: float = 15.0) -> dict[str, bool]:
    """``models.get`` every configured primary/backup. Loud error if any missing."""
    key = (settings.gemini_api_key or "").strip()
    results: dict[str, bool] = {}
    if not key:
        logger.warning("gemini models.get skipped — GEMINI_API_KEY unset")
        return results
    for model in configured_model_ids():
        ok = _models_get(model, api_key=key, timeout_s=timeout_s)
        results[model] = ok
        if ok:
            logger.info("gemini models.get ok model=%s", model)
        else:
            logger.error(
                "GEMINI MODEL MISSING OR UNREACHABLE: models.get failed for %s — "
                "fix TASK_* / GEMINI_LIGHT_* env or Google model availability",
                model,
            )
    return results


def _models_get(model: str, *, api_key: str, timeout_s: float) -> bool:
    url = _MODELS_GET_URL.format(model=model)
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"x-goog-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return 200 <= int(resp.status) < 300
    except urllib.error.HTTPError as exc:
        logger.error(
            "gemini models.get HTTP %s for model=%s",
            exc.code,
            model,
        )
        return False
    except Exception as exc:
        logger.error("gemini models.get error model=%s err=%s", model, exc)
        return False


def startup_validate_and_probe() -> None:
    """Validate config at boot; probe models when an API key is present."""
    errors = validate_gemini_task_settings()
    if errors:
        for err in errors:
            logger.error("GEMINI TASK CONFIG INVALID: %s", err)
    else:
        for exhibit, label in ((True, "exhibit"), (False, "light")):
            for cfg in all_task_configs(exhibit=exhibit):
                logger.info(
                    "gemini lane=%s task=%s primary=%s backup=%s thinking_level=%s",
                    label,
                    cfg.task,
                    cfg.primary,
                    cfg.backup,
                    cfg.thinking_level,
                )
        tier = (settings.gemini_service_tier or "standard").strip().lower()
        logger.info(
            "gemini service_tier=%s (exhibit Task A only; light lane omits priority)",
            tier,
        )
    if (settings.gemini_api_key or "").strip():
        probe_configured_models()
