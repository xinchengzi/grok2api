"""调用日志服务 - 记录和查询API调用日志"""

import uuid
import time
import asyncio
import orjson
import aiofiles
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Dict, Any, List, Optional, Tuple

from app.core.logger import logger


@dataclass
class CallLog:
    """调用日志数据模型"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    sso: str = ""  # SSO标识（脱敏：前6位+****+后4位）
    model: str = ""  # 调用模型
    success: bool = True  # 是否成功
    status_code: int = 0  # HTTP状态码
    token_consumed: int = 1  # Token消耗量
    response_time: float = 0.0  # 响应时间（秒）
    error_message: str = ""  # 错误信息
    proxy_used: str = ""  # 使用的代理
    media_urls: List[str] = field(default_factory=list)  # 生成媒体URL列表
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（持久化安全）"""
        data = asdict(self)
        # 确保不保存完整SSO（二次脱敏保护）
        if len(data.get("sso", "")) > 20:
            sso = data["sso"]
            data["sso"] = f"{sso[:6]}****{sso[-4:]}" if len(sso) > 10 else sso[:6] + "****"
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'CallLog':
        """从字典创建"""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class CallLogService:
    """调用日志服务（单例）"""
    
    _instance: Optional['CallLogService'] = None
    
    def __new__(cls) -> 'CallLogService':
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        
        self.log_file = Path(__file__).parents[2] / "data" / "call_logs.json"
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._logs: List[CallLog] = []
        self._loaded = False
        self._save_pending = False
        self._save_task: Optional[asyncio.Task] = None
        self._shutdown = False
        self._max_logs = 10000  # 默认最大日志数
        self._pending_queue: List[CallLog] = []  # 待处理日志队列（线程安全追加）
        
        self._initialized = True
        logger.debug(f"[CallLog] 初始化完成: {self.log_file}")
    
    def set_max_logs(self, max_count: int) -> None:
        """设置最大日志数"""
        self._max_logs = max_count
    
    async def _load_logs(self) -> None:
        """加载日志数据"""
        if self._loaded:
            return
        
        try:
            if self.log_file.exists():
                async with aiofiles.open(self.log_file, "r", encoding="utf-8") as f:
                    content = await f.read()
                    data = orjson.loads(content)
                    self._logs = [CallLog.from_dict(log) for log in data.get("logs", [])]
                    logger.info(f"[CallLog] 加载 {len(self._logs)} 条日志")
            else:
                self._logs = []
                logger.debug("[CallLog] 日志文件不存在，创建空列表")
            self._loaded = True
        except Exception as e:
            logger.error(f"[CallLog] 加载日志失败: {e}")
            self._logs = []
            self._loaded = True
    
    async def _save_logs(self) -> None:
        """保存日志数据"""
        try:
            async with self._lock:
                data = {
                    "logs": [log.to_dict() for log in self._logs],
                    "meta": {
                        "total_count": len(self._logs),
                        "last_save": int(time.time() * 1000)
                    }
                }
                content = orjson.dumps(data, option=orjson.OPT_INDENT_2).decode()
                async with aiofiles.open(self.log_file, "w", encoding="utf-8") as f:
                    await f.write(content)
                logger.debug(f"[CallLog] 保存 {len(self._logs)} 条日志")
        except Exception as e:
            logger.error(f"[CallLog] 保存日志失败: {e}")
    
    def _mark_dirty(self) -> None:
        """标记有待保存的数据"""
        self._save_pending = True
    
    async def _batch_save_worker(self) -> None:
        """批量保存后台任务"""
        interval = 2.0  # 2秒保存一次
        logger.info(f"[CallLog] 存储任务已启动，间隔: {interval}s")
        
        while not self._shutdown:
            await asyncio.sleep(interval)
            
            # 处理待处理队列
            if self._pending_queue:
                async with self._lock:
                    pending = self._pending_queue
                    self._pending_queue = []
                    self._logs.extend(pending)
                    
                    # 自动清理超限日志
                    if len(self._logs) > self._max_logs:
                        excess = len(self._logs) - self._max_logs
                        self._logs = self._logs[excess:]
                        logger.debug(f"[CallLog] 自动清理 {excess} 条旧日志")
                
                self._save_pending = True
            
            if self._save_pending and not self._shutdown:
                try:
                    await self._save_logs()
                    self._save_pending = False
                except Exception as e:
                    logger.error(f"[CallLog] 存储失败: {e}")
    
    async def start(self) -> None:
        """启动服务"""
        await self._load_logs()
        if self._save_task is None:
            self._save_task = asyncio.create_task(self._batch_save_worker())
            logger.info("[CallLog] 服务已启动")
    
    async def shutdown(self) -> None:
        """关闭服务"""
        self._shutdown = True
        
        if self._save_task:
            self._save_task.cancel()
            try:
                await self._save_task
            except asyncio.CancelledError:
                pass
        
        # 处理剩余队列
        if self._pending_queue:
            async with self._lock:
                self._logs.extend(self._pending_queue)
                self._pending_queue = []
            self._save_pending = True
        
        # 最终保存
        if self._save_pending:
            await self._save_logs()
            logger.info("[CallLog] 关闭时保存完成")

    async def record(self, log: CallLog) -> None:
        """记录调用日志"""
        if not self._loaded:
            await self._load_logs()
        
        async with self._lock:
            self._logs.append(log)
            
            # 自动清理超限日志
            if len(self._logs) > self._max_logs:
                excess = len(self._logs) - self._max_logs
                self._logs = self._logs[excess:]
                logger.info(f"[CallLog] 自动清理 {excess} 条旧日志")
        
        self._mark_dirty()
        logger.debug(f"[CallLog] 记录日志: {log.sso[:10]}... {log.model} {log.success}")
    
    def queue_call(
        self,
        sso: str,
        model: str,
        success: bool,
        status_code: int,
        response_time: float,
        token_consumed: int = 1,
        error_message: str = "",
        proxy_used: str = "",
        media_urls: Optional[List[str]] = None
    ) -> None:
        """同步方法：将日志加入队列（不阻塞，由后台任务处理）"""
        # SSO脱敏处理（前6位+****+后4位，不可逆）
        if len(sso) > 14:
            sso_masked = f"{sso[:6]}****{sso[-4:]}"
        elif len(sso) > 6:
            sso_masked = sso[:6] + "****"
        else:
            sso_masked = sso
        urls = media_urls or []
        
        log = CallLog(
            sso=sso_masked,
            model=model,
            success=success,
            status_code=status_code,
            response_time=response_time,
            token_consumed=token_consumed,
            error_message=error_message,
            proxy_used=proxy_used,
            media_urls=urls
        )
        # 直接追加到队列，由 _batch_save_worker 处理
        self._pending_queue.append(log)
    
    async def record_call(
        self,
        sso: str,
        model: str,
        success: bool,
        status_code: int,
        response_time: float,
        token_consumed: int = 1,
        error_message: str = "",
        proxy_used: str = "",
        media_urls: Optional[List[str]] = None
    ) -> None:
        """便捷方法：记录API调用"""
        # SSO脱敏处理（前6位+****+后4位，不可逆）
        if len(sso) > 14:
            sso_masked = f"{sso[:6]}****{sso[-4:]}"
        elif len(sso) > 6:
            sso_masked = sso[:6] + "****"
        else:
            sso_masked = sso
        urls = media_urls or []
        
        log = CallLog(
            sso=sso_masked,
            model=model,
            success=success,
            status_code=status_code,
            response_time=response_time,
            token_consumed=token_consumed,
            error_message=error_message,
            proxy_used=proxy_used,
            media_urls=urls
        )
        await self.record(log)
    
    async def query(
        self,
        sso: Optional[str] = None,
        success: Optional[bool] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        model: Optional[str] = None,
        page: int = 1,
        page_size: int = 50
    ) -> Tuple[List[CallLog], int]:
        """查询日志
        
        Args:
            sso: SSO筛选（模糊匹配）
            success: 成功状态筛选
            start_time: 开始时间（毫秒时间戳）
            end_time: 结束时间（毫秒时间戳）
            model: 模型筛选
            page: 页码（从1开始）
            page_size: 每页数量
        
        Returns:
            (日志列表, 总数)
        """
        if not self._loaded:
            await self._load_logs()
        
        # 筛选
        filtered = self._logs.copy()
        
        if sso:
            sso_lower = sso.lower()
            filtered = [
                log for log in filtered
                if sso_lower in log.sso.lower()
            ]
        
        if success is not None:
            filtered = [log for log in filtered if log.success == success]
        
        if start_time is not None:
            filtered = [log for log in filtered if log.timestamp >= start_time]
        
        if end_time is not None:
            filtered = [log for log in filtered if log.timestamp <= end_time]
        
        if model:
            filtered = [log for log in filtered if model.lower() in log.model.lower()]
        
        # 按时间倒序
        filtered.sort(key=lambda x: x.timestamp, reverse=True)
        
        total = len(filtered)
        
        # 分页
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated = filtered[start_idx:end_idx]
        
        return paginated, total
    
    async def cleanup(self, max_count: Optional[int] = None) -> int:
        """清理超限日志
        
        Args:
            max_count: 保留的最大日志数，None则使用默认值
        
        Returns:
            删除的日志数量
        """
        if not self._loaded:
            await self._load_logs()
        
        limit = max_count or self._max_logs
        
        async with self._lock:
            if len(self._logs) <= limit:
                return 0
            
            # 按时间排序，保留最新的
            self._logs.sort(key=lambda x: x.timestamp, reverse=True)
            deleted = len(self._logs) - limit
            self._logs = self._logs[:limit]
        
        self._mark_dirty()
        logger.info(f"[CallLog] 清理 {deleted} 条日志，保留 {len(self._logs)} 条")
        return deleted
    
    async def clear_all(self) -> int:
        """清空所有日志
        
        Returns:
            删除的日志数量
        """
        if not self._loaded:
            await self._load_logs()
        
        async with self._lock:
            count = len(self._logs)
            self._logs = []
        
        self._mark_dirty()
        logger.info(f"[CallLog] 清空 {count} 条日志")
        return count
    
    def get_stats(self) -> Dict[str, Any]:
        """获取日志统计信息"""
        if not self._logs:
            return {
                "total": 0,
                "success": 0,
                "failed": 0,
                "success_rate": 0.0,
                "today_total": 0
            }
        
        total = len(self._logs)
        success = sum(1 for log in self._logs if log.success)
        failed = total - success
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        start_today = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        today_total = sum(1 for log in self._logs if log.timestamp >= start_today)
        
        return {
            "total": total,
            "success": success,
            "failed": failed,
            "success_rate": round(success / total * 100, 2) if total > 0 else 0.0,
            "today_total": today_total
        }

    def get_models(self) -> List[str]:
        """获取全部模型列表（去重）"""
        if not self._logs:
            return []
        return sorted({log.model for log in self._logs if log.model})


# 全局实例
call_log_service = CallLogService()
