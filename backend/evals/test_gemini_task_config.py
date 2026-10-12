"""Per-task Gemini model config validation."""

from __future__ import annotations


def test_default_task_matrix():
    from app.gemini_tasks import (
        TASK_A_EXTRACT,
        TASK_B_ENTITY,
        TASK_C_CONTRA,
        TASK_D_VISUAL,
        task_config,
    )

    a = task_config(TASK_A_EXTRACT)
    assert a.primary == "gemini-3.6-flash"
    assert a.backup == "gemini-3.5-flash-lite"
    assert a.thinking_level == "minimal"

    b = task_config(TASK_B_ENTITY)
    assert b.primary == "gemini-3.8-flash"
    assert b.thinking_level == "medium"

    c = task_config(TASK_C_CONTRA)
    assert c.thinking_level == "high"

    d = task_config(TASK_D_VISUAL)
    assert d.primary == "gemini-3.8-flash"
    assert d.backup == "gemini-3.6-flash"


def test_invalid_thinking_level_flagged(monkeypatch):
    from app import config as config_mod
    from app.gemini_tasks import validate_gemini_task_settings

    monkeypatch.setattr(config_mod.settings, "task_a_extract_thinking_level", "MAX")
    errs = validate_gemini_task_settings()
    assert any("thinking_level" in e for e in errs)


def test_service_tier_only_on_task_a(monkeypatch):
    from app import config as config_mod
    from app.gemini_tasks import TASK_A_EXTRACT, TASK_D_VISUAL, service_tier_for_task

    monkeypatch.setattr(config_mod.settings, "gemini_service_tier", "priority")
    assert service_tier_for_task(TASK_A_EXTRACT) == "priority"
    assert service_tier_for_task(TASK_D_VISUAL) == ""
