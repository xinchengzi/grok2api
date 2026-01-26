"""调试请求日志：保存完整请求（脱敏/截断）到本地文件。

默认关闭（global.debug_request_log）。

设计目标：
- 方便定位多模态/参数传递问题
- URL 优先；data:*;base64 的内容只保留前缀+长度
- 总量上限（默认 100MB），超过后删除最旧文件

注意：不写入任何 Cookie/Token。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Dict, Optional

import aiofiles

from app.core.config import setting
from app.core.logger import logger


def _now_ms() -> int:
    return int(time.time() * 1000)


def _is_base64_data_url(s: str) -> bool:
    if not isinstance(s, str):
        return False
    if not s.startswith("data:"):
        return False
    return "base64," in s[:128]


def _truncate(s: str, keep: int) -> str:
    if len(s) <= keep:
        return s
    return s[:keep] + f"...<truncated len={len(s)}>"


def _sanitize(obj: Any, *, base64_keep: int) -> Any:
    """递归脱敏/截断。

    - data:*;base64,... 会转成 {__type__, prefix, length}
    - 移除 Authorization/Cookie
    """
    if obj is None:
        return None

    if isinstance(obj, (int, float, bool)):
        return obj

    if isinstance(obj, str):
        if _is_base64_data_url(obj):
            return {
                "__type__": "data_url",
                "prefix": _truncate(obj, base64_keep),
                "length": len(obj),
            }
        return obj

    if isinstance(obj, list):
        return [_sanitize(x, base64_keep=base64_keep) for x in obj]

    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in {"authorization", "cookie", "set-cookie"}:
                continue
            out[k] = _sanitize(v, base64_keep=base64_keep)
        return out

    try:
        return str(obj)
    except Exception:
        return "<unserializable>"


def _dir_total_bytes(path: str) -> int:
    total = 0
    try:
        for entry in os.scandir(path):
            if not entry.is_file():
                continue
            try:
                total += entry.stat().st_size
            except Exception:
                continue
    except FileNotFoundError:
        return 0
    return total


def _list_files_oldest_first(path: str):
    files = []
    try:
        for entry in os.scandir(path):
            if not entry.is_file():
                continue
            try:
                st = entry.stat()
                files.append((st.st_mtime, entry.path, st.st_size))
            except Exception:
                continue
    except FileNotFoundError:
        return []
    files.sort(key=lambda x: x[0])
    return files


def _enforce_max_total_bytes(path: str, max_total: int) -> None:
    try:
        total = _dir_total_bytes(path)
        if total <= max_total:
            return
        for _, fp, sz in _list_files_oldest_first(path):
            try:
                os.remove(fp)
                total -= sz
            except Exception:
                continue
            if total <= max_total:
                break
    except Exception as e:
        logger.debug(f"[ReqLog] 清理失败: {e}")


class RequestDebugLogService:
    async def log(self, *, openai_request: Optional[dict], grok_payload: Optional[dict], meta: Optional[dict] = None) -> None:
        cfg = setting.global_config
        if not cfg.get("debug_request_log", False):
            return

        log_dir = cfg.get("debug_request_log_dir", "/app/logs/requests")
        max_total = int(cfg.get("debug_request_log_max_total_bytes", 104857600))
        base64_keep = int(cfg.get("debug_request_log_base64_keep", 512))

        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception as e:
            logger.debug(f"[ReqLog] 创建目录失败: {e}")
            return

        _enforce_max_total_bytes(log_dir, max_total)

        data = {
            "ts_ms": _now_ms(),
            "id": str(uuid.uuid4()),
            "meta": _sanitize(meta or {}, base64_keep=base64_keep),
            "openai_request": _sanitize(openai_request or {}, base64_keep=base64_keep),
            "grok_payload": _sanitize(grok_payload or {}, base64_keep=base64_keep),
        }

        fp = os.path.join(log_dir, f"{data['ts_ms']}_{data['id']}.json")

        try:
            async with aiofiles.open(fp, "w", encoding="utf-8") as f:
                await f.write(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.debug(f"[ReqLog] 写入失败: {e}")
            return

        _enforce_max_total_bytes(log_dir, max_total)


request_debug_log_service = RequestDebugLogService()
