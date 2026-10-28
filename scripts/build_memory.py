import pathlib

base_path = pathlib.Path("src/react_ai/memory/base.py")
text = base_path.read_text(encoding="utf-8")

methods = """
    def to_dict(self) -> dict[str, Any]:
        \"\"\"转换为字典结构，便于序列化。\"\"\"
        return {
            "id": self.id,
            "type": self.type.value if isinstance(self.type, MemoryType) else str(self.type),
            "key": self.key,
            "value": self.value,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "ttl_seconds": self.ttl_seconds,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "access_count": self.access_count,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryItem":
        \"\"\"从字典反序列化生成 MemoryItem 实例。\"\"\"
        raw_type = data.get("type", MemoryType.SESSION)
        if isinstance(raw_type, str):
            try:
                mem_type = MemoryType(raw_type)
            except ValueError:
                mem_type = MemoryType.SESSION
        elif isinstance(raw_type, MemoryType):
            mem_type = raw_type
        else:
            mem_type = MemoryType.SESSION

        return cls(
            id=data.get("id"),
            type=mem_type,
            key=data.get("key", ""),
            value=data.get("value"),
            session_id=data.get("session_id"),
            user_id=data.get("user_id"),
            tenant_id=data.get("tenant_id"),
            ttl_seconds=data.get("ttl_seconds"),
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
            access_count=data.get("access_count", 0),
            metadata=data.get("metadata") or {},
        )
"""

if "def to_dict" not in text:
    target = "return (time.time() - self.created_at) > self.ttl_seconds"
    idx = text.find(target)
    if idx != -1:
        end_line = text.find("\n", idx)
        text = text[:end_line] + "\n" + methods + text[end_line:]
        base_path.write_text(text, encoding="utf-8")
        print("Updated base.py with to_dict / from_dict")
    else:
        print("Target not found in base.py")
else:
    print("to_dict already in base.py")
