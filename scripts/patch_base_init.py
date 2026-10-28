import pathlib

base_path = pathlib.Path("src/react_ai/memory/base.py")
text = base_path.read_text(encoding="utf-8")

old_init = """    def __init__(self):
        self._lock = threading.RLock()  # 读写锁，保证线程安全
        self._item_count: int = 0  # 当前记忆条目计数"""

new_init = """    def __init__(self, name: str | None = None, max_items: int | None = None):
        self._lock = threading.RLock()  # 读写锁，保证线程安全
        self._item_count: int = 0  # 当前记忆条目计数
        if name is not None:
            self.name = name
        if max_items is not None:
            self.max_items = max_items"""

if old_init in text:
    text = text.replace(old_init, new_init)
    base_path.write_text(text, encoding="utf-8")
    print("Updated BaseMemory.__init__")
else:
    print("old_init pattern not matched")
