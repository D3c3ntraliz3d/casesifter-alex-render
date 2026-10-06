from pathlib import Path
import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent.parent.parent / ".env"),
        extra="ignore"
    )

    app_name: str = "CaseSifter"
    app_version: str = "1.0.0"
    # vault = local John testing (full /api/cases). analysis = Render hybrid (v1 only).
    # Empty / unset → resolve_app_mode() picks vault locally, analysis on Render.
    app_mode: str = ""
    app_env: str = "development"
    # Comma-separated origins. Use * only for local vault; analysis mode rejects *.
    cors_allow_origins: str = "*"
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
    # Heavy image forensics is capped so a large exhibit cannot pin the machine.
    max_forensic_pages: int = 12
    render_scale: float = 1.15
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

    # Optional crash reporting (scrubbed — no document bytes or paths).
    sentry_dsn: str | None = None
    sentry_environment: str | None = None

    # Ephemeral analysis API (hybrid desktop): process then delete job files.
    ephemeral_jobs: bool = True
    ephemeral_ttl_hours: int = 6
    analysis_api_keys: str = ""  # comma-separated bearer tokens; empty = rely on entitlement only
    ocr_max_pages: int = 40
    ocr_sparse_char_threshold: int = 80

    # Persistent per-matter fact memory (no PDF retention).
    # SOURCE_FILE_RETENTION=immediate keeps current delete-after-run behavior.
    source_file_retention: str = "immediate"
    persist_facts: bool = True  # global default; per-firm firms.persist_facts can opt out
    database_url: str | None = None  # Postgres in prod; SQLite fallback when unset
    fact_memory_master_key: str | None = None  # wraps per-firm Fernet keys

    # Scan watchdog: fail + release slot when a running job goes silent or runs too long.
    scan_stall_seconds: int = 420  # 7 minutes with no progress update
    scan_max_seconds: int = 1080  # 18 minutes wall-clock once running

    # Gotcha logic verifier (Track B). Historical impossibilities stay on Gemini (Track C).
    # auto = Claude if ANTHROPIC_API_KEY set, else Gemini.
    gotcha_verify_provider: str = "auto"
    anthropic_api_key: str | None = None
    claude_model: str = "claude-sonnet-4-5-20250929"
    openai_api_key: str | None = None
    openai_verify_model: str = "gpt-4.1"
    practice_pack: str = "foreclosure"
    # Separate Gemini vision pass for logo/chrome. On by default — strongest
    # upperhand lane for wrong-era / template chrome on consumer statements.
    logo_chrome_pass: bool = True

    def persist_facts_enabled(self) -> bool:
        return bool(self.persist_facts)

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def cases_dir(self) -> Path:
        return self.data_dir / "cases"

    def analysis_api_key_set(self) -> set[str]:
        return {part.strip() for part in (self.analysis_api_keys or "").split(",") if part.strip()}

    def cors_origin_list(self) -> list[str]:
        raw = (self.cors_allow_origins or "").strip()
        if not raw:
            return ["http://127.0.0.1:5173", "http://localhost:5173"]
        return [part.strip() for part in raw.split(",") if part.strip()]

    def resolve_app_mode(self) -> str:
        """vault locally; analysis on Render / production unless explicitly vault."""
        explicit = (self.app_mode or "").strip().lower()
        if explicit in {"vault", "analysis"}:
            return explicit
        # Render always injects RENDER=true. Hosted analysis image must not expose vault.
        if (os.environ.get("RENDER") or "").strip().lower() in {"true", "1", "yes"}:
            return "analysis"
        if (self.app_env or "").strip().lower() == "production":
            return "analysis"
        return "vault"


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.jobs_dir.mkdir(parents=True, exist_ok=True)
settings.cases_dir.mkdir(parents=True, exist_ok=True)
