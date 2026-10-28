import pathlib

path = pathlib.Path("src/react_ai/memory/redis.py")
text = path.read_text(encoding="utf-8")

old_code = """        self._redis_url = redis_url
        self._host = host
        self._port = port
        self._db = db
        self._password = password
        self._ssl = ssl
        self._client: redis.Redis | None = client"""

new_code = """        # 若未提供显式连接参数且无外部 client，则自动从全局 settings 继承配置
        if (
            redis_url is None
            and client is None
            and host == "localhost"
            and port == 6379
            and password is None
        ):
            try:
                from react_ai.config.settings import settings
                if settings.memory.redis_url or settings.memory.password or settings.memory.port != 6379:
                    redis_url = settings.memory.resolved_redis_url
                    host = settings.memory.host
                    port = settings.memory.port
                    password = settings.memory.password
                    db = settings.memory.db
            except Exception:
                pass

        self._redis_url = redis_url
        self._host = host
        self._port = port
        self._db = db
        self._password = password
        self._ssl = ssl
        self._client: redis.Redis | None = client"""

if old_code in text:
    text = text.replace(old_code, new_code)
    path.write_text(text, encoding="utf-8")
    print("Updated RedisMemory default configuration inheritance")
else:
    print("Pattern not found in redis.py")
