import asyncio
import os
from datetime import date
from pathlib import Path

from .paths import ensure_data_root
from .shared_state import locked_json_state

DAILY_GENERATION_LIMIT = 5
USAGE_STATE_FILE = ensure_data_root() / "generation_usage.json"


def load_admin_ids() -> set[int]:
    raw = os.environ.get("ADMIN_TELEGRAM_IDS", "").strip()
    if not raw:
        return set()

    admin_ids: set[int] = set()
    for chunk in raw.split(","):
        value = chunk.strip()
        if not value:
            continue
        try:
            admin_ids.add(int(value))
        except ValueError:
            continue
    return admin_ids


class DailyUsageLimiter:
    def __init__(self, state_file: Path = USAGE_STATE_FILE, limit: int = DAILY_GENERATION_LIMIT):
        self._state_file = state_file
        self._limit = limit
        self._lock = asyncio.Lock()

    async def get_remaining(self, user_id: int, is_admin: bool = False) -> int | None:
        if is_admin:
            return None

        async with self._lock:
            with locked_json_state(self._state_file) as state:
                self._sanitize_state(state)
                today = self._today_key()
                used = int(state.get(today, {}).get(str(user_id), 0))
                return max(0, self._limit - used)

    async def consume(self, user_id: int, is_admin: bool = False) -> tuple[bool, int | None]:
        if is_admin:
            return True, None

        async with self._lock:
            with locked_json_state(self._state_file) as state:
                self._sanitize_state(state)
                today = self._today_key()
                today_usage = state.setdefault(today, {})
                used = int(today_usage.get(str(user_id), 0))
                if used >= self._limit:
                    return False, 0

                used += 1
                today_usage[str(user_id)] = used
                return True, self._limit - used

    async def refund(self, user_id: int, is_admin: bool = False) -> None:
        if is_admin:
            return

        async with self._lock:
            with locked_json_state(self._state_file) as state:
                self._sanitize_state(state)
                today = self._today_key()
                today_usage = state.get(today, {})
                used = int(today_usage.get(str(user_id), 0))
                if used <= 0:
                    return

                used -= 1
                if used == 0:
                    today_usage.pop(str(user_id), None)
                else:
                    today_usage[str(user_id)] = used

                if not today_usage:
                    state.pop(today, None)

    def _today_key(self) -> str:
        return date.today().isoformat()

    def _sanitize_state(self, data: dict) -> None:
        today = self._today_key()
        sanitized = {
            day: usage
            for day, usage in data.items()
            if isinstance(usage, dict) and day >= today
        }
        data.clear()
        data.update(sanitized)
