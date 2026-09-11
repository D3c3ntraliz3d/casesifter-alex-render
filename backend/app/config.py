from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Court Exhibit Scanner"
    app_version: str = "1.0.0"
    max_file_mb: int = 100
    data_dir: Path = Path(__file__).resolve().parent.parent / "data"
    allowed_extensions: set[str] = {
        ".pdf",
        ".docx",
        ".xlsx",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
    }
    # Heavy image forensics is capped so a 50-page PDF stays near the 2-minute target.
    max_forensic_pages: int = 50
    render_scale: float = 1.4
    ela_quality: int = 90
    ela_scale: int = 18

    # Contextual engine, tier 2: claim verification. Off until a key is supplied.
    # Sends only generated questions to a third party, never document content.
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_timeout_s: int = 20
    contextual_max_chars: int = 40_000
    contextual_max_claims: int = 5
    contextual_min_confidence: int = 70
    contextual_window: int = 400

    # Contextual engine, tier 3: full document analysis. Requires a second, deliberate
    # opt-in because it uploads rendered page images and extracted text to Google.
    # Leave false for sealed, privileged, or otherwise restricted evidence.
    contextual_multimodal: bool = False
    contextual_max_pages: int = 4
    contextual_image_max_px: int = 1600
    contextual_image_quality: int = 80
    contextual_max_ai_findings: int = 12
    gemini_document_timeout_s: int = 90

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.jobs_dir.mkdir(parents=True, exist_ok=True)
