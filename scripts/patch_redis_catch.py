import pathlib

path = pathlib.Path("src/react_ai/memory/redis.py")
text = path.read_text(encoding="utf-8")

old_code = """            except Exception:
                pass"""

new_code = """            except (ImportError, AttributeError) as exc:
                logger.debug("redis_memory_settings_load_skip error=%s", exc)"""

if old_code in text:
    text = text.replace(old_code, new_code)
    path.write_text(text, encoding="utf-8")
    print("Patched catch block in redis.py")
else:
    print("Pattern not found")
