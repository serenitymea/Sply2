import asyncio
import time
import uuid
from pathlib import Path

from .paths import ensure_data_root
from .shared_state import locked_json_state


RUNTIME_STATE_FILE = ensure_data_root() / "runtime_state.json"
PREPROCESS_WAIT_TIMEOUT_SEC = 180
PREPROCESS_POLL_SEC = 0.5


class RuntimeControl:
    def __init__(self, state_file: Path = RUNTIME_STATE_FILE):
        self._state_file = state_file

    def try_register_session(self, user_id: int, limit: int, ttl_sec: float) -> bool:
        now = time.time()
        with locked_json_state(self._state_file) as state:
            sessions = state.setdefault("sessions", {})
            self._cleanup_sessions(sessions, now)
            key = str(user_id)
            if key not in sessions and len(sessions) >= limit:
                return False
            sessions[key] = {"expires_at": now + ttl_sec}
            return True

    def touch_session(self, user_id: int, ttl_sec: float) -> None:
        now = time.time()
        with locked_json_state(self._state_file) as state:
            sessions = state.setdefault("sessions", {})
            self._cleanup_sessions(sessions, now)
            key = str(user_id)
            if key in sessions:
                sessions[key]["expires_at"] = now + ttl_sec

    def unregister_session(self, user_id: int) -> None:
        with locked_json_state(self._state_file) as state:
            sessions = state.setdefault("sessions", {})
            sessions.pop(str(user_id), None)

    async def acquire_preprocess_slot(self, user_id: int, limit: int, ttl_sec: float) -> str:
        return await self.acquire_slot("preprocess_slots", user_id, limit, ttl_sec)

    async def acquire_pipeline_slot(self, user_id: int, limit: int, ttl_sec: float) -> str:
        return await self.acquire_slot("pipeline_slots", user_id, limit, ttl_sec)

    async def acquire_slot(self, slot_group: str, user_id: int, limit: int, ttl_sec: float) -> str:
        deadline = time.monotonic() + PREPROCESS_WAIT_TIMEOUT_SEC
        while True:
            token = self._try_acquire_slot(slot_group, user_id, limit, ttl_sec)
            if token:
                return token
            if time.monotonic() >= deadline:
                raise asyncio.TimeoutError("Timed out waiting for preprocessing slot.")
            await asyncio.sleep(PREPROCESS_POLL_SEC)

    def _try_acquire_preprocess_slot(self, user_id: int, limit: int, ttl_sec: float) -> str | None:
        return self._try_acquire_slot("preprocess_slots", user_id, limit, ttl_sec)

    def _try_acquire_slot(self, slot_group: str, user_id: int, limit: int, ttl_sec: float) -> str | None:
        now = time.time()
        with locked_json_state(self._state_file) as state:
            slots = state.setdefault(slot_group, {})
            self._cleanup_slots(slots, now)
            if len(slots) >= limit:
                return None

            token = uuid.uuid4().hex
            slots[token] = {
                "user_id": user_id,
                "expires_at": now + ttl_sec,
            }
            return token

    def release_preprocess_slot(self, token: str | None) -> None:
        self.release_slot("preprocess_slots", token)

    def release_pipeline_slot(self, token: str | None) -> None:
        self.release_slot("pipeline_slots", token)

    def release_slot(self, slot_group: str, token: str | None) -> None:
        if not token:
            return
        with locked_json_state(self._state_file) as state:
            slots = state.setdefault(slot_group, {})
            slots.pop(token, None)

    @staticmethod
    def _cleanup_sessions(sessions: dict, now: float) -> None:
        expired = [key for key, meta in sessions.items() if float(meta.get("expires_at", 0)) <= now]
        for key in expired:
            sessions.pop(key, None)

    @staticmethod
    def _cleanup_slots(slots: dict, now: float) -> None:
        expired = [key for key, meta in slots.items() if float(meta.get("expires_at", 0)) <= now]
        for key in expired:
            slots.pop(key, None)
