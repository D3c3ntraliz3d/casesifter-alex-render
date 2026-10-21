"""SOURCE_FILE_RETENTION=s3 must put bytes and only claim keys that exist."""

from __future__ import annotations

from pathlib import Path


def test_retain_and_resolve_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_FILE_RETENTION", "s3")
    monkeypatch.setenv("BLOB_BACKEND", "local")

    from app.config import Settings
    import app.config as config_mod
    import app.blob_store as blob_mod
    import app.scan_dispatch as dispatch_mod

    settings = Settings(
        data_dir=tmp_path / "data",
        source_file_retention="s3",
        blob_backend="local",
    )
    settings.blob_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr(blob_mod, "settings", settings)
    monkeypatch.setattr(dispatch_mod, "settings", settings)
    blob_mod.reset_blob_store_for_tests(None)

    pdf = tmp_path / "tiny.pdf"
    pdf.write_bytes(b"%PDF-1.4 fixture")
    digest = "a" * 64

    key = dispatch_mod.retain_source_blob(
        firm_id="firm-1",
        matter_id="matter-1",
        sha256=digest,
        source_path=pdf,
    )
    assert key
    assert key.startswith("firms/")
    assert blob_mod.get_blob_store().exists(key)
    assert blob_mod.get_blob_store().get(key) == b"%PDF-1.4 fixture"

    resolved = dispatch_mod.resolve_retained_source_key(
        firm_id="firm-1",
        matter_id="matter-1",
        sha256=digest,
        preferred_key=key,
    )
    assert resolved == key

    # Invented key with no object must not resolve.
    missing = dispatch_mod.resolve_retained_source_key(
        firm_id="firm-1",
        matter_id="matter-1",
        sha256="b" * 64,
    )
    assert missing is None


def test_retain_disabled_when_immediate(tmp_path, monkeypatch):
    from app.config import Settings
    import app.config as config_mod
    import app.scan_dispatch as dispatch_mod

    settings = Settings(data_dir=tmp_path / "data", source_file_retention="immediate")
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr(dispatch_mod, "settings", settings)

    pdf = tmp_path / "tiny.pdf"
    pdf.write_bytes(b"%PDF-1.4 fixture")
    assert (
        dispatch_mod.retain_source_blob(
            firm_id="firm-1",
            matter_id="matter-1",
            sha256="c" * 64,
            source_path=pdf,
        )
        is None
    )
