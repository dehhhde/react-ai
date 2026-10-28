import pathlib

env_path = pathlib.Path(".env")
env_example_path = pathlib.Path(".env.example")

redis_env_snippet = """
# Redis Configuration
REDIS_HOST=127.0.0.1
REDIS_PORT=6380
REDIS_PASSWORD=123456
REDIS_DB=0
REDIS_URL=redis://:123456@127.0.0.1:6380/0
MEMORY_TYPE=redis
"""

redis_env_example_snippet = """
# Redis Configuration
REDIS_HOST=127.0.0.1
REDIS_PORT=6380
REDIS_PASSWORD=your_redis_password_here
REDIS_DB=0
REDIS_URL=redis://:your_redis_password_here@127.0.0.1:6380/0
MEMORY_TYPE=redis
"""

env_text = env_path.read_text(encoding="utf-8")
if "REDIS_HOST" not in env_text:
    env_path.write_text(env_text.strip() + "\n" + redis_env_snippet.strip() + "\n", encoding="utf-8")
    print("Updated .env")

example_text = env_example_path.read_text(encoding="utf-8")
if "REDIS_HOST" not in example_text:
    env_example_path.write_text(example_text.strip() + "\n" + redis_env_example_snippet.strip() + "\n", encoding="utf-8")
    print("Updated .env.example")
