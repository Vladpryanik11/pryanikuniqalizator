from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    # empty values (as in .env.example) fall back to the defaults
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    bot_token: SecretStr | None = None
    bot_api_url: str | None = None  # None = cloud Bot API, development only
    channel_id: str = ""  # -100... or @username
    channel_url: str = ""
    admin_ids: str = ""
    max_file_mb: int = 200
    max_duration_sec: int = 180
    render_timeout_sec: int = 900
    data_dir: Path = PROJECT_ROOT / "data"
    uniq_config: Path = PROJECT_ROOT / "config" / "uniq.yaml"
    tz: str = "Europe/Moscow"

    @field_validator("data_dir")
    @classmethod
    def _anchor_data_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else PROJECT_ROOT / value

    @property
    def token_value(self) -> str | None:
        """The bot token as a plain string, for redacting it from text; None if it is not set."""
        return self.bot_token.get_secret_value() if self.bot_token else None

    @property
    def admin_id_list(self) -> list[int]:
        return [int(x) for x in self.admin_ids.replace(" ", "").split(",") if x]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.work_dir):
            d.mkdir(parents=True, exist_ok=True)
