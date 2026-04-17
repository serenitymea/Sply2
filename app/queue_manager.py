import asyncio
import logging
from typing import Awaitable, Callable, Literal, Optional

logger = logging.getLogger(__name__)

PROCESSING_TIMEOUT = 500
CancelResult = Literal["queued", "active", "missing"]


class QueueManager:
    def __init__(self, process_callback: Optional[Callable[[int], Awaitable[None]]] = None):
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._active_user: Optional[int] = None
        self._lock = asyncio.Lock()
        self._waiting_users: list[int] = []
        self._process_callback = process_callback
        self._worker_task: Optional[asyncio.Task] = None
        self._current_process_task: Optional[asyncio.Task] = None

    def set_process_callback(self, callback: Callable[[int], Awaitable[None]]) -> None:
        self._process_callback = callback

    def start(self) -> None:
        if self._worker_task and not self._worker_task.done():
            logger.warning("QueueManager worker is already running")
            return
        self._worker_task = asyncio.create_task(self._worker())
        logger.info("QueueManager started")

    async def enqueue(self, user_id: int) -> Optional[int]:
        async with self._lock:
            if user_id == self._active_user or user_id in self._waiting_users:
                return None
            self._waiting_users.append(user_id)
            await self._queue.put(user_id)
            return len(self._waiting_users)

    async def get_position(self, user_id: int) -> Optional[int]:
        async with self._lock:
            if user_id == self._active_user:
                return 0
            if user_id in self._waiting_users:
                return self._waiting_users.index(user_id) + 1
        return None

    async def cancel(self, user_id: int) -> CancelResult:
        async with self._lock:
            if user_id == self._active_user:
                if self._current_process_task and not self._current_process_task.done():
                    self._current_process_task.cancel()
                return "active"

            if user_id not in self._waiting_users:
                return "missing"

            self._waiting_users.remove(user_id)

            remaining: list[int] = []
            while not self._queue.empty():
                try:
                    remaining.append(self._queue.get_nowait())
                    self._queue.task_done()
                except asyncio.QueueEmpty:
                    break

            for uid in remaining:
                if uid != user_id:
                    await self._queue.put(uid)

            return "queued"

    @property
    def active_user(self) -> Optional[int]:
        return self._active_user

    @property
    def queue_size(self) -> int:
        return len(self._waiting_users)

    async def _worker(self) -> None:
        logger.info("QueueManager worker running")
        while True:
            user_id = await self._queue.get()

            async with self._lock:
                if user_id not in self._waiting_users:
                    self._queue.task_done()
                    continue
                self._waiting_users.remove(user_id)
                self._active_user = user_id

            logger.info(f"[Queue] START user={user_id} | queue_size={self.queue_size}")
            try:
                if self._process_callback:
                    self._current_process_task = asyncio.create_task(
                        self._process_callback(user_id)
                    )
                    await asyncio.wait_for(
                        self._current_process_task,
                        timeout=PROCESSING_TIMEOUT,
                    )
            except asyncio.TimeoutError:
                logger.error(f"[Queue] TIMEOUT user={user_id}")
            except asyncio.CancelledError:
                logger.info(f"[Queue] CANCELLED user={user_id}")
            except Exception as e:
                logger.exception(f"[Queue] ERROR user={user_id}: {e}")
            finally:
                async with self._lock:
                    self._active_user = None
                    self._current_process_task = None
                self._queue.task_done()
                logger.info(f"[Queue] DONE user={user_id}")
