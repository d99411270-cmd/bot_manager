from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    telegram_proxy_url: str = Field("", alias="TELEGRAM_PROXY_URL")
    deepseek_api_key: str = Field(alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field("https://api.deepseek.com", alias="DEEPSEEK_BASE_URL")
    deepseek_model: str = Field("deepseek-chat", alias="DEEPSEEK_MODEL")
    google_spreadsheet_id: str = Field(alias="GOOGLE_SPREADSHEET_ID")
    google_service_account_file: str | None = Field(None, alias="GOOGLE_SERVICE_ACCOUNT_FILE")
    google_service_account_json: str | None = Field(None, alias="GOOGLE_SERVICE_ACCOUNT_JSON")
    deepseek_timeout_seconds: float = Field(20.0, alias="DEEPSEEK_TIMEOUT_SECONDS", gt=0)
    deepseek_max_tokens: int = Field(350, alias="DEEPSEEK_MAX_TOKENS", ge=50, le=2000)
    reply_delay_probability: float = Field(0.7, alias="REPLY_DELAY_PROBABILITY", ge=0, le=1)

    @model_validator(mode="after")
    def validate_settings(self):
        if not (self.google_service_account_file or self.google_service_account_json):
            raise ValueError("Нужны реквизиты сервисного аккаунта Google")
        return self
