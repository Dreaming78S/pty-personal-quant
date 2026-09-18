from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从 .env 读取的数据库与 Tushare 配置。"""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    aliyun_rds_host: str
    aliyun_rds_port: int = 3306
    aliyun_rds_user: str
    aliyun_rds_passport: str
    aliyun_rds_database: str
    tushare_token: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
