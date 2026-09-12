from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent.parent.parent / ".env"),
        extra="ignore"
    )

    app_name: str = "Court Exhibit Scanner"
    app_version: str = "1.0.0"
    max_file_mb: int = 100
    data_dir: Path = Path(__file__).resolve().parent.parent / "data"
    allowed_extensions: set[str] = {
        ".pdf",
        ".docx",
        ".xlsx",
        ".txt",
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
    # opt-in because it uploads the original file and extracted text to Google.
    # Leave false for sealed, privileged, or otherwise restricted evidence.
    contextual_multimodal: bool = False
    # Inline request bodies are capped by the API; a larger exhibit degrades to text-only.
    contextual_max_upload_mb: int = 15
    contextual_max_ai_findings: int = 12
    gemini_document_timeout_s: int = 90
    # Cap Gemini "thinking" to cut latency and token burn. Gemini 3.x uses thinkingLevel
    # (minimal|low|medium|high). Gemini 2.5 Flash uses thinkingBudget (0 disables).
    gemini_thinking_level: str = "minimal"
    gemini_claim_thinking_level: str = "minimal"

    # Hybrid control plane. "open" unlocks scans locally; "license" needs LICENSE_KEY;
    # "cloud" expects login against CLOUD_CONTROL_URL (stubbed when URL is empty).
    entitlement_mode: str = "open"
    license_key: str | None = None
    cloud_control_url: str | None = None
    share_anonymized_outcomes: bool = True

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def cases_dir(self) -> Path:
        return self.data_dir / "cases"


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.jobs_dir.mkdir(parents=True, exist_ok=True)
settings.cases_dir.mkdir(parents=True, exist_ok=True)
