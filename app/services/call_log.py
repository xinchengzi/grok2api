"""调用日志服务 - 记录和查询API调用日志（移植自旧版）。

存储位置：data/call_logs.json（由 docker-compose 挂载到宿主机 /opt/grok2api/data）。

设计目标：
- 不阻塞请求路径：提供 queue_call() 同步入队
- 后台批量落盘：默认每 2 秒
- SSO 脱敏：前6位****后4位
"""

import uuid
import time
import asyncio
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Dict, Any, List, Optional, Tuple

import orjson
import aiofiles

from app.core.logger import logger


@dataclass
class CallLog:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    sso: str = ""
    model: str = ""
    success: bool = True
    status_code: int = 0
    token_consumed: int = 1
    response_time: float = 0.0
    error_message: str = ""
    proxy_used: str = ""
    media_urls: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if len(data.get("sso", "")) > 20:
            sso = data["sso"]
            data["sso"] = f"{sso[:6]}****{sso[-4:]}" if len(sso) > 10 else sso[:6] + "****"
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CallLog":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class CallLogService:
    _instance: Optional["CallLogService"] = None

    def __new__(cls) -> "CallLogService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return

        self.log_file = Path(__file__).parents[2] / "data" / "call_logs.json"
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

        self._lock = asyncio.Lock()
        self._logs: List[CallLog] = []
        self._loaded = False
        self._save_pending = False
        self._save_task: Optional[asyncio.Task] = None
        self._shutdown = False
        self._max_logs = 10000
        self._pending_queue: List[CallLog] = []

        self._initialized = True

    async def _load_logs(self) -> None:
        if self._loaded:
            return
        try:
            if self.log_file.exists():
                async with aiofiles.open(self.log_file, "r", encoding="utf-8") as f:
                    content = await f.read()
                    data = orjson.loads(content)
                    self._logs = [CallLog.from_dict(x) for x in data.get("logs", [])]
            else:
                self._logs = []
            self._loaded = True
        except Exception as e:
            logger.error(f"[CallLog] load failed: {e}")
            self._logs = []
            self._loaded = True

    async def _save_logs(self) -> None:
        async with self._lock:
            data = {
                "logs": [x.to_dict() for x in self._logs],
                "meta": {"total_count": len(self._logs), "last_save": int(time.time() * 1000)},
            }
            content = orjson.dumps(data, option=orjson.OPT_INDENT_2).decode()
            async with aiofiles.open(self.log_file, "w", encoding="utf-8") as f:
                await f.write(content)

    async def _worker(self) -> None:
        interval = 2.0
        while not self._shutdown:
            await asyncio.sleep(interval)

            if self._pending_queue:
                async with self._lock:
                    pending = self._pending_queue
                    self._pending_queue = []
                    self._logs.extend(pending)
                    if len(self._logs) > self._max_logs:
                        excess = len(self._logs) - self._max_logs
                        self._logs = self._logs[excess:]
                self._save_pending = True

            if self._save_pending and not self._shutdown:
                try:
                    await self._save_logs()
                    self._save_pending = False
                except Exception as e:
                    logger.error(f"[CallLog] save failed: {e}")

    async def start(self) -> None:
        await self._load_logs()
        if self._save_task is None:
            self._save_task = asyncio.create_task(self._worker())

    def queue_call(
        self,
        *,
        sso: str,
        model: str,
        success: bool,
        status_code: int,
        response_time: float,
        token_consumed: int = 1,
        error_message: str = "",
        proxy_used: str = "",
        media_urls: Optional[List[str]] = None,
    ) -> None:
        if len(sso) > 14:
            sso_masked = f"{sso[:6]}****{sso[-4:]}"
        elif len(sso) > 6:
            sso_masked = sso[:6] + "****"
        else:
            sso_masked = sso

        log = CallLog(
            sso=sso_masked,
            model=model,
            success=success,
            status_code=status_code,
            response_time=response_time,
            token_consumed=token_consumed,
            error_message=error_message,
            proxy_used=proxy_used,
            media_urls=media_urls or [],
        )
        self._pending_queue.append(log)
        self._save_pending = True

    async def query(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        model: str = "",
        success: Optional[bool] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        await self._load_logs()
        logs = self._logs
        if model:
            logs = [x for x in logs if x.model == model]
        if success is not None:
            logs = [x for x in logs if x.success is success]
        total = len(logs)
        start = max(0, (page - 1) * page_size)
        end = start + page_size
        return [x.to_dict() for x in logs[start:end]], total

    async def clear(self, max_count: Optional[int] = None) -> int:
        await self._load_logs()
        async with self._lock:
            before = len(self._logs)
            if max_count is None:
                self._logs = []
            else:
                max_count = max(0, int(max_count))
                if len(self._logs) > max_count:
                    self._logs = self._logs[-max_count:]
            self._save_pending = True
            return before - len(self._logs)


call_log_service = CallLogService()

