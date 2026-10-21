"""Per-task Gemini model config validation."""

from __future__ import annotations


def test_default_exhibit_task_matrix():
    from app.gemini_tasks import (
        TASK_A_EXTRACT,
        TASK_B_ENTITY,
        TASK_C_CONTRA,
        TASK_D_VISUAL,
        task_config,
    )

    a = task_config(TASK_A_EXTRACT, exhibit=True)
    assert a.primary == "gemini-3.6-flash"
    assert a.backup == "gemini-3.5-flash-lite"
    assert a.thinking_level == "minimal"
    assert a.lane == "exhibit"

    b = task_config(TASK_B_ENTITY, exhibit=True)
    assert b.primary == "gemini-3.8-flash"
    assert b.backup == "gemini-3.6-flash"
    assert b.thinking_level == "medium"
    assert b.lane == "exhibit"

    c = task_config(TASK_C_CONTRA, exhibit=True)
    assert c.primary == "gemini-3.8-flash"
    assert c.thinking_level == "high"

    d = task_config(TASK_D_VISUAL, exhibit=True)
    assert d.primary == "gemini-3.8-flash"
    assert d.backup == "gemini-3.5-flash-lite"
    assert d.thinking_level == "medium"


def test_light_lane_uses_gemini_light_env():
    from app.gemini_tasks import TASK_A_EXTRACT, TASK_D_VISUAL, task_config

    for task in (TASK_A_EXTRACT, TASK_D_VISUAL):
        cfg = task_config(task, exhibit=False)
        assert cfg.primary == "gemini-3.5-flash-lite"
        assert cfg.backup == "gemini-3.5-flash-lite"
        assert cfg.thinking_level == "minimal"
        assert cfg.lane == "light"


def test_exhibit_models_for_profile():
    from app.gemini_tasks import exhibit_models_for_profile

    assert exhibit_models_for_profile("forensic") is True
    assert exhibit_models_for_profile("court_filing") is False
    assert exhibit_models_for_profile(None) is True


def test_gemini_model_lane_context():
    from app.gemini_tasks import (
        TASK_D_VISUAL,
        current_exhibit_models,
        gemini_model_lane,
        task_config,
    )

    assert current_exhibit_models() is True
    with gemini_model_lane(exhibit=False):
        assert current_exhibit_models() is False
        assert task_config(TASK_D_VISUAL).lane == "light"
        assert task_config(TASK_D_VISUAL).primary == "gemini-3.5-flash-lite"
    assert current_exhibit_models() is True
    assert task_config(TASK_D_VISUAL).lane == "exhibit"


def test_invalid_thinking_level_flagged(monkeypatch):
    from app import config as config_mod
    from app.gemini_tasks import validate_gemini_task_settings

    monkeypatch.setattr(config_mod.settings, "task_a_extract_thinking_level", "MAX")
    errs = validate_gemini_task_settings()
    assert any("thinking_level" in e for e in errs)


def test_service_tier_only_on_task_a_exhibit(monkeypatch):
    from app import config as config_mod
    from app.gemini_tasks import (
        TASK_A_EXTRACT,
        TASK_D_VISUAL,
        gemini_model_lane,
        service_tier_for_task,
    )

    monkeypatch.setattr(config_mod.settings, "gemini_service_tier", "priority")
    assert service_tier_for_task(TASK_A_EXTRACT) == "priority"
    assert service_tier_for_task(TASK_D_VISUAL) == ""
    with gemini_model_lane(exhibit=False):
        assert service_tier_for_task(TASK_A_EXTRACT) == ""


def test_gemini_38_flash_rejects_thinking_minimal(monkeypatch):
    from app import config as config_mod
    from app.gemini_tasks import validate_gemini_task_settings

    monkeypatch.setattr(config_mod.settings, "task_d_visual_primary", "gemini-3.8-flash")
    monkeypatch.setattr(config_mod.settings, "task_d_visual_thinking_level", "minimal")
    errs = validate_gemini_task_settings()
    assert any("minimal" in e and "3.8-flash" in e for e in errs)


def test_gemini_38_flash_allows_low_medium_high(monkeypatch):
    from app import config as config_mod
    from app.gemini_tasks import TASK_D_VISUAL, task_config

    monkeypatch.setattr(config_mod.settings, "task_d_visual_primary", "gemini-3.8-flash")
    monkeypatch.setattr(config_mod.settings, "task_d_visual_backup", "gemini-3.6-flash")
    for level in ("low", "medium", "high"):
        monkeypatch.setattr(config_mod.settings, "task_d_visual_thinking_level", level)
        assert task_config(TASK_D_VISUAL, exhibit=True).thinking_level == level
