from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    llm_api_key: str = os.getenv("LLM_API_KEY", "").strip()
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    llm_model: str = os.getenv("LLM_MODEL", "deepseek-chat").strip()
    app_base_url: str = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    allow_shared_cnki: bool = os.getenv("ALLOW_SHARED_CNKI", "false").lower() in {"1", "true", "yes"}
    max_file_bytes: int = int(os.getenv("MAX_FILE_MB", "20")) * 1024 * 1024
    source_timeout_seconds: float = float(os.getenv("SOURCE_TIMEOUT_SECONDS", "10"))
    job_ttl_seconds: int = int(os.getenv("JOB_TTL_SECONDS", "7200"))
    database_url: str = os.getenv("DATABASE_URL", "").strip()
    contact_email: str = os.getenv("CONTACT_EMAIL", "").strip()

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)


settings = Settings()

