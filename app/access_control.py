import asyncio
import json
import os
import tempfile
from datetime import date
from pathlib import Path

from .paths import ensure_data_root

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
            state = self._read_state()
            today = self._today_key()
            used = int(state.get(today, {}).get(str(user_id), 0))
            return max(0, self._limit - used)

    async def consume(self, user_id: int, is_admin: bool = False) -> tuple[bool, int | None]:
        if is_admin:
            return True, None

        async with self._lock:
            state = self._read_state()
            today = self._today_key()
            today_usage = state.setdefault(today, {})
            used = int(today_usage.get(str(user_id), 0))
            if used >= self._limit:
                return False, 0

            used += 1
            today_usage[str(user_id)] = used
            self._write_state(state)
            return True, self._limit - used

    async def refund(self, user_id: int, is_admin: bool = False) -> None:
        if is_admin:
            return

        async with self._lock:
            state = self._read_state()
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

            self._write_state(state)

    def _today_key(self) -> str:
        return date.today().isoformat()

    def _read_state(self) -> dict:
        if not self._state_file.exists():
            return {}

        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

        if not isinstance(data, dict):
            return {}

        today = self._today_key()
        return {
            day: usage
            for day, usage in data.items()
            if isinstance(usage, dict) and day >= today
        }

    def _write_state(self, state: dict) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, ensure_ascii=True, indent=2)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self._state_file.parent,
            delete=False,
        ) as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, self._state_file)
