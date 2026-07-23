from __future__ import annotations

from src.connectors.bitrix_openlines.dialog_cache import RedisDialogConfigCache
from src.connectors.bitrix_openlines.models import DialogMetadata


class StubRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def hget(self, name: str, key: str) -> str | None:
        return self.values.get(f"{name}:{key}")

    def hset(self, name: str, key: str, value: str) -> None:
        self.values[f"{name}:{key}"] = value

    def delete(self, name: str) -> None:
        for hashed in list(self.values):
            if hashed.startswith(f"{name}:"):
                self.values.pop(hashed, None)

    def close(self) -> None:
        return None


def test_dialog_cache_round_trips_resolved_metadata() -> None:
    redis = StubRedis()
    cache = RedisDialogConfigCache(redis)

    assert cache.get(77) is None
    cache.set(77, DialogMetadata(77, "46", "facebook"))

    cached = cache.get(77)
    assert cached == DialogMetadata(77, "46", "facebook")


def test_dialog_cache_returns_none_for_unknown_chat() -> None:
    cache = RedisDialogConfigCache(StubRedis())

    assert cache.get(999) is None
