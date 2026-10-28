import pathlib

path = pathlib.Path("src/react_ai/config/settings.py")
text = path.read_text(encoding="utf-8")

old_memory_config = """class MemoryConfig(BaseSettings):
    memory_type: str = Field(default="in_memory", alias="MEMORY_TYPE")
    redis_url: Optional[str] = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    session_ttl_seconds: int = Field(default=86400, alias="SESSION_TTL")"""

new_memory_config = """class MemoryConfig(BaseSettings):
    memory_type: str = Field(default="in_memory", alias="MEMORY_TYPE")
    host: str = Field(default="127.0.0.1", alias="REDIS_HOST")
    port: int = Field(default=6380, alias="REDIS_PORT")
    password: Optional[str] = Field(default=None, alias="REDIS_PASSWORD")
    db: int = Field(default=0, alias="REDIS_DB")
    redis_url: Optional[str] = Field(default=None, alias="REDIS_URL")
    session_ttl_seconds: int = Field(default=86400, alias="SESSION_TTL")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @property
    def resolved_redis_url(self) -> str:
        if self.redis_url:
            return self.redis_url
        if self.password:
            return f"redis://:{self.password}@{self.host}:{self.port}/{self.db}"
        return f"redis://{self.host}:{self.port}/{self.db}\""""

if old_memory_config in text:
    text = text.replace(old_memory_config, new_memory_config)
    path.write_text(text, encoding="utf-8")
    print("Updated settings.py successfully")
else:
    print("Pattern not found in settings.py")
