"""Application configuration, loaded from environment / .env file."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # AI — provider-agnostic. ai_provider picks the primary text/vision brain;
    # calls fall back to any other provider that has a key, so a single
    # provider running out of credit never takes the product down.
    ai_provider: str = "anthropic"  # anthropic | openai | gemini
    anthropic_api_key: str | None = None
    ai_model: str = "claude-opus-4-8"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o"
    gemini_api_key: str | None = None
    # Розшифровка голосових. Deepgram — окремий баланс власника; ключ лише з env.
    deepgram_api_key: str | None = None
    deepgram_model: str = "nova-2"
    # Порожньо = перший, у кого є ключ: deepgram -> openai -> gemini.
    transcribe_provider: str | None = None
    gemini_model: str = "gemini-2.0-flash"

    # Embeddings for semantic search over the network. Uses OpenAI (or Voyage)
    # — Anthropic has no embeddings API. Disabled if neither key is set.
    voyage_api_key: str | None = None
    embeddings_model: str = "text-embedding-3-small"  # OpenAI default (1536 dims)

    # Database
    database_url: str = "sqlite:///./networking.db"

    # Authentication (single-user). When app_password is set, the whole site +
    # API require HTTP Basic auth; service-to-service callers (the Chater
    # bridge) can instead send the X-API-Key header. Empty password = auth off.
    app_username: str = "admin"
    app_password: str | None = None
    api_key: str | None = None  # defaults to app_password if unset

    # Behaviour
    daily_suggestions: int = 5
    upcoming_window_days: int = 14

    # CORS
    cors_origins: str = "*"

    # Google integration (Gmail + Calendar sync)
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str = "http://localhost:8000/api/integrations/google/callback"
    # How many days back to pull emails / calendar events when syncing.
    # Консолідація памʼяті: після скількох нових взаємодій переписувати досьє
    # й витягати факти, скільки контактів обробляти за один прохід, і через
    # скільки днів досьє вважати застарілим, якщо була хоч одна зміна.
    consolidate_every: int = 5
    consolidate_max_per_run: int = 15
    consolidate_stale_days: int = 30
    # Публічна адреса сайту (для адреси MCP у кабінеті). Порожньо — беремо
    # з заголовків запиту (x-forwarded-proto / host за nginx).
    public_base_url: str | None = None
    # Фоновий AI: хто думає за фонові джоби.
    #   api         — модель через API-ключ (AI_PROVIDER: anthropic/openai/gemini)
    #   claude_code — Claude Code на сервері за підпискою (воркер, черга ai_jobs)
    #   off         — фон без AI (детерміновані частини працюють, чернеток немає)
    background_ai: str = "api"
    # Куди відкочуватись, якщо claude_code впав кілька разів: api | none.
    background_ai_fallback: str = "api"
    claude_code_bin: str = "claude"
    claude_code_model: str = "sonnet"
    claude_code_max_turns: int = 8
    claude_code_timeout: int = 240
    # Адреса нашого ж MCP зсередини docker-мережі — без nginx і без TLS.
    mcp_internal_url: str = "http://web:8000"
    aijobs_max_attempts: int = 3
    aijobs_max_per_hour: int = 40
    # Тихі години у форматі "23-7" (локальний час сервера); порожньо = немає.
    aijobs_quiet_hours: str | None = None
    # Вхідні, що прийшли протягом цих секунд, — одна задача, не десять.
    aijobs_debounce_seconds: int = 45
    google_tasklist_title: str = "Networking AI"
    sync_window_days: int = 120

    # Daily automation (background scheduler)
    scheduler_enabled: bool = False
    daily_run_hour: int = 8  # local server hour to run the daily job
    digest_email_to: str | None = None  # where to send the daily digest
    # Don't send the Telegram digest when there is nothing actionable.
    digest_quiet_when_empty: bool = True

    # Follow-up cycle: nudge when our last message got no reply for N days.
    followup_after_days: int = 4

    # Pre-meeting briefs: send a contact brief ~lead minutes before events.
    meeting_brief_enabled: bool = True
    meeting_brief_lead_minutes: int = 60

    # Proactive check-ins / reviews to Telegram.
    checkin_enabled: bool = True
    checkin_hour: int = 13  # local server hour for the midday nudge
    evening_digest_hour: int = 22   # вечірнє зведення дня
    evening_digest_minute: int = 10
    weekly_review_enabled: bool = True
    weekly_review_hour: int = 9  # Monday morning network review
    reflection_enabled: bool = True
    reflection_hour: int = 10  # Thursday relationship reflection
    reflection_count: int = 3  # how many relationships to reflect on

    # Daily social sweep: re-scrape socials of the N most-due contacts to
    # detect fresh life events (needs a scraper provider). 0 = off.
    social_sweep_daily: int = 5

    # Монітор соцмереж: провайдер дописів. Зараз — Apify (керовані проксі,
    # оплата за результат, без ризику для власного акаунта). Токен лише з env.
    apify_api_token: str | None = None
    apify_instagram_actor: str = "apify~instagram-scraper"
    # Скільки контактів перевіряти за прохід і не частіше ніж раз на N днів
    # кожного; скільки дописів тягнути з профілю за раз.
    social_monitor_per_run: int = 10
    social_monitor_every_days: int = 3
    social_monitor_posts_limit: int = 12

    # Social scraping provider (for JS-heavy networks)
    scraper_provider: str | None = None  # e.g. "scrapingbee"
    scraper_api_key: str | None = None

    # AI sentiment scoring of synced interactions (emails/meetings/telegram)
    sync_ai_sentiment: bool = True

    # Chater importer (reuse the existing Telegram bot's database)
    chater_database_url: str | None = None

    # Telegram (digest delivery + lightweight command bot)
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    # ALiSiO CRM: той самий бот, її чати лідів (docs/ALISIO.md).
    # Секрет = значення CRM_TELEGRAM_WEBHOOK_SECRET з ALiSiO. Порожньо — не пересилаємо.
    alisio_webhook_url: str = "https://alisio.rozum.one/api/webhooks/crm/telegram"
    alisio_webhook_secret: str | None = None
    # Run Networking AI's OWN long-polling bot. Keep OFF when reusing the
    # Chater bot (two pollers on one token cause a 409 Conflict). Default: the
    # single-bot bridge — Chater pulls /api/digest/text and serves commands.
    telegram_polling_enabled: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def ai_enabled(self) -> bool:
        return bool(
            self.anthropic_api_key or self.openai_api_key or self.gemini_api_key
        )

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.app_password)

    @property
    def effective_api_key(self) -> str | None:
        return self.api_key or self.app_password

    @property
    def chater_configured(self) -> bool:
        return bool(self.chater_database_url)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
