"""代理池管理器 - 支持多代理URL和SSO绑定"""

import asyncio
import aiohttp
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
from app.core.logger import logger


@dataclass
class ProxyInfo:
    """代理信息"""
    url: str  # 代理URL
    healthy: bool = True  # 健康状态
    fail_count: int = 0  # 连续失败次数
    last_used: int = 0  # 最后使用时间（毫秒）
    assigned_sso: List[str] = field(default_factory=list)  # 绑定的SSO列表
    total_requests: int = 0  # 总请求数
    success_requests: int = 0  # 成功请求数
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ProxyInfo':
        """从字典创建"""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# 常量
MAX_FAIL_COUNT = 3  # 最大连续失败次数
HEALTH_CHECK_INTERVAL = 60  # 健康检查间隔（秒）


class ProxyPool:
    """代理池管理器"""
    
    def __init__(self):
        self._pool_url: Optional[str] = None
        self._static_proxy: Optional[str] = None
        self._current_proxy: Optional[str] = None
        self._last_fetch_time: float = 0
        self._fetch_interval: int = 300  # 5分钟刷新一次
        self._enabled: bool = False
        self._lock = asyncio.Lock()  # 用于代理获取
        self._state_lock = asyncio.Lock()  # 用于状态变更（SSO绑定、代理健康等）
        self._save_lock = asyncio.Lock()
        self._storage = None
        self._suspend_persist = False
        self._state_loaded = False
        
        # 多代理支持
        self._proxies: Dict[str, ProxyInfo] = {}  # URL -> ProxyInfo
        self._sso_assignments: Dict[str, str] = {}  # SSO -> Proxy URL
        self._round_robin_index: int = 0

    def set_storage(self, storage: Any) -> None:
        """设置存储实例（用于代理绑定持久化）"""
        self._storage = storage
    
    def configure(self, proxy_url: str, proxy_pool_url: str = "", proxy_pool_interval: int = 300):
        """配置代理池
        
        Args:
            proxy_url: 静态代理URL（socks5h://xxx 或 http://xxx）
            proxy_pool_url: 代理池API URL，返回单个代理地址
            proxy_pool_interval: 代理池刷新间隔（秒）
        """
        self._static_proxy = self._normalize_proxy(proxy_url) if proxy_url else None
        pool_url = proxy_pool_url.strip() if proxy_pool_url else None
        if pool_url and self._looks_like_proxy_url(pool_url):
            normalized_proxy = self._normalize_proxy(pool_url)
            if not self._static_proxy:
                self._static_proxy = normalized_proxy
                logger.warning("[ProxyPool] proxy_pool_url看起来是代理地址，已作为静态代理使用，请改用proxy_url")
            else:
                logger.warning("[ProxyPool] proxy_pool_url看起来是代理地址，已忽略（使用proxy_url）")
            pool_url = None
        self._pool_url = pool_url
        self._fetch_interval = proxy_pool_interval
        self._enabled = bool(self._pool_url)
        
        # 如果有静态代理，添加到代理列表
        if self._static_proxy:
            self.add_proxy(self._static_proxy)
            self._current_proxy = self._static_proxy
        
        if self._enabled:
            logger.info(f"[ProxyPool] 代理池已启用: {self._pool_url}, 刷新间隔: {self._fetch_interval}s")
        elif self._static_proxy:
            logger.info(f"[ProxyPool] 使用静态代理: {self._static_proxy}")
        else:
            logger.info("[ProxyPool] 未配置代理")
    
    def add_proxy(self, url: str) -> bool:
        """添加代理（线程安全）
        
        Args:
            url: 代理URL
        
        Returns:
            是否添加成功
        """
        normalized = self._normalize_proxy(url)
        if not self._validate_proxy(normalized):
            logger.warning(f"[ProxyPool] 代理格式无效: {url}")
            return False
        
        if normalized in self._proxies:
            logger.debug(f"[ProxyPool] 代理已存在: {normalized}")
            return True
        
        self._proxies[normalized] = ProxyInfo(url=normalized)
        logger.info(f"[ProxyPool] 添加代理: {normalized}")
        self._schedule_persist()
        return True
    
    def remove_proxy(self, url: str) -> bool:
        """移除代理（线程安全）
        
        Args:
            url: 代理URL
        
        Returns:
            是否移除成功
        """
        normalized = self._normalize_proxy(url)
        if normalized not in self._proxies:
            return False
        
        # 清除相关的SSO绑定
        proxy_info = self._proxies[normalized]
        for sso in proxy_info.assigned_sso:
            if sso in self._sso_assignments:
                del self._sso_assignments[sso]
        
        del self._proxies[normalized]
        logger.info(f"[ProxyPool] 移除代理: {normalized}")
        self._schedule_persist()
        return True
    
    async def assign_to_sso(self, proxy_url: str, sso: str) -> bool:
        """将代理分配给SSO（线程安全）
        
        Args:
            proxy_url: 代理URL
            sso: SSO标识
        
        Returns:
            是否分配成功
        """
        async with self._state_lock:
            normalized = self._normalize_proxy(proxy_url)
            if normalized not in self._proxies:
                logger.warning(f"[ProxyPool] 代理不存在: {normalized}")
                return False
            
            # 如果SSO已绑定其他代理，先解绑
            if sso in self._sso_assignments:
                old_proxy = self._sso_assignments[sso]
                if old_proxy in self._proxies:
                    self._proxies[old_proxy].assigned_sso = [
                        s for s in self._proxies[old_proxy].assigned_sso if s != sso
                    ]
            
            self._sso_assignments[sso] = normalized
            if sso not in self._proxies[normalized].assigned_sso:
                self._proxies[normalized].assigned_sso.append(sso)
            
            logger.debug(f"[ProxyPool] 绑定SSO: {sso[:10]}... -> {normalized}")
        self._schedule_persist()
        return True
    
    async def unassign_from_sso(self, sso: str) -> bool:
        """取消SSO的代理绑定（线程安全）
        
        Args:
            sso: SSO标识
        
        Returns:
            是否取消成功
        """
        async with self._state_lock:
            if sso not in self._sso_assignments:
                return False
            
            proxy_url = self._sso_assignments[sso]
            if proxy_url in self._proxies:
                self._proxies[proxy_url].assigned_sso = [
                    s for s in self._proxies[proxy_url].assigned_sso if s != sso
                ]
            
            del self._sso_assignments[sso]
            logger.debug(f"[ProxyPool] 解绑SSO: {sso[:10]}...")
        self._schedule_persist()
        return True

    async def get_proxy_for_sso(self, sso: str = "") -> Optional[str]:
        """获取SSO对应的代理（线程安全）
        
        优先返回绑定的代理，否则自动分配
        
        Args:
            sso: SSO标识
        
        Returns:
            代理URL或None
        """
        async with self._state_lock:
            # 1. 检查SSO绑定
            if sso and sso in self._sso_assignments:
                proxy_url = self._sso_assignments[sso]
                if proxy_url in self._proxies and self._proxies[proxy_url].healthy:
                    return proxy_url
                # 解绑无效/不健康代理（在锁内操作）
                if proxy_url in self._proxies:
                    self._proxies[proxy_url].assigned_sso = [
                        s for s in self._proxies[proxy_url].assigned_sso if s != sso
                    ]
                del self._sso_assignments[sso]
            
            # 2. 自动分配（轮询健康代理）
            proxy = await self._select_proxy_round_robin_locked()
            if sso and proxy:
                # 直接在锁内绑定
                self._sso_assignments[sso] = proxy
                if sso not in self._proxies[proxy].assigned_sso:
                    self._proxies[proxy].assigned_sso.append(sso)
                self._schedule_persist()
            return proxy
    
    async def _select_proxy_round_robin_locked(self) -> Optional[str]:
        """轮询选择健康代理（需在_state_lock内调用）"""
        healthy_proxies = [url for url, info in self._proxies.items() if info.healthy]
        
        if not healthy_proxies:
            # 没有健康代理，尝试从代理池获取
            if self._enabled:
                await self._fetch_proxy()
                healthy_proxies = [url for url, info in self._proxies.items() if info.healthy]
            
            if not healthy_proxies:
                return self._static_proxy
        
        # 轮询选择
        self._round_robin_index = self._round_robin_index % len(healthy_proxies)
        selected = healthy_proxies[self._round_robin_index]
        self._round_robin_index += 1
        
        # 更新使用时间
        self._proxies[selected].last_used = int(time.time() * 1000)
        self._proxies[selected].total_requests += 1
        
        return selected
    
    async def _select_proxy_round_robin(self) -> Optional[str]:
        """轮询选择健康代理（外部调用，自动加锁）"""
        async with self._state_lock:
            return await self._select_proxy_round_robin_locked()
    
    async def mark_failure(self, proxy_url: str) -> None:
        """标记代理失败（线程安全）
        
        Args:
            proxy_url: 代理URL
        """
        async with self._state_lock:
            normalized = self._normalize_proxy(proxy_url)
            if normalized not in self._proxies:
                return
            
            info = self._proxies[normalized]
            info.fail_count += 1
            
            if info.fail_count >= MAX_FAIL_COUNT:
                info.healthy = False
                logger.warning(f"[ProxyPool] 代理标记为不健康: {normalized} (连续失败{info.fail_count}次)")
                # 解绑所有SSO
                for sso in list(info.assigned_sso):
                    if sso in self._sso_assignments:
                        del self._sso_assignments[sso]
                info.assigned_sso = []
        self._schedule_persist()
    
    async def mark_success(self, proxy_url: str) -> None:
        """标记代理成功（线程安全）
        
        Args:
            proxy_url: 代理URL
        """
        async with self._state_lock:
            normalized = self._normalize_proxy(proxy_url)
            if normalized not in self._proxies:
                return
            
            info = self._proxies[normalized]
            info.fail_count = 0
            info.success_requests += 1
            
            # 如果之前不健康，恢复健康状态
            if not info.healthy:
                info.healthy = True
                logger.info(f"[ProxyPool] 代理恢复健康: {normalized}")
        self._schedule_persist()
    
    def get_all_proxies(self) -> List[Dict[str, Any]]:
        """获取所有代理信息"""
        return [info.to_dict() for info in self._proxies.values()]
    
    def get_sso_assignments(self) -> Dict[str, str]:
        """获取所有SSO绑定关系"""
        return self._sso_assignments.copy()

    async def load_state(self) -> None:
        """从存储恢复代理状态（仅FileStorage支持时生效）"""
        if not self._storage or not hasattr(self._storage, "load_proxy_state"):
            self._state_loaded = True
            return
        try:
            self._suspend_persist = True
            data = await self._storage.load_proxy_state()
            proxies = data.get("proxies", {}) or {}
            assignments = data.get("assignments", {}) or {}

            # 恢复代理信息
            for url, info_data in proxies.items():
                normalized = self._normalize_proxy(url)
                self.add_proxy(normalized)
                info = self._proxies.get(normalized)
                if not info:
                    continue
                for key in ["healthy", "fail_count", "last_used", "total_requests", "success_requests"]:
                    if key in info_data:
                        setattr(info, key, info_data[key])
                if "assigned_sso" in info_data and isinstance(info_data["assigned_sso"], list):
                    info.assigned_sso = info_data["assigned_sso"]

            # 恢复绑定关系（以 assignments 为准）
            self._sso_assignments = {}
            if assignments:
                for sso, url in assignments.items():
                    normalized = self._normalize_proxy(url)
                    if normalized not in self._proxies:
                        self.add_proxy(normalized)
                    self._sso_assignments[sso] = normalized
            else:
                # fallback：从代理信息中还原
                for url, info in self._proxies.items():
                    for sso in info.assigned_sso:
                        self._sso_assignments[sso] = url

            # 反向同步 assigned_sso
            for info in self._proxies.values():
                info.assigned_sso = []
            for sso, url in self._sso_assignments.items():
                if url in self._proxies:
                    self._proxies[url].assigned_sso.append(sso)

            logger.info("[ProxyPool] 代理状态已恢复")
        except Exception as e:
            logger.warning(f"[ProxyPool] 恢复代理状态失败: {e}")
        finally:
            self._state_loaded = True
            self._suspend_persist = False
            self._schedule_persist()

    async def _persist_state(self) -> None:
        if not self._storage or not hasattr(self._storage, "save_proxy_state"):
            return
        async with self._save_lock:
            data = {
                "proxies": {url: info.to_dict() for url, info in self._proxies.items()},
                "assignments": self._sso_assignments.copy(),
            }
            await self._storage.save_proxy_state(data)

    def _schedule_persist(self) -> None:
        if not self._storage or not hasattr(self._storage, "save_proxy_state"):
            return
        if not self._state_loaded:
            return
        if self._suspend_persist:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._persist_state())
    
    async def get_proxy(self) -> Optional[str]:
        """获取代理地址（兼容旧接口）
        
        Returns:
            代理URL或None
        """
        # 如果未启用代理池且没有多代理，返回静态代理
        if not self._enabled and not self._proxies:
            return self._static_proxy
        
        # 如果有多代理，使用轮询
        if self._proxies:
            return await self._select_proxy_round_robin()
        
        # 检查是否需要刷新
        now = time.time()
        if not self._current_proxy or (now - self._last_fetch_time) >= self._fetch_interval:
            async with self._lock:
                # 双重检查
                if not self._current_proxy or (now - self._last_fetch_time) >= self._fetch_interval:
                    await self._fetch_proxy()
        
        return self._current_proxy
    
    async def force_refresh(self) -> Optional[str]:
        """强制刷新代理（用于403错误重试）
        
        Returns:
            新的代理URL或None
        """
        if not self._enabled:
            # 如果有多代理，尝试切换到下一个
            if len(self._proxies) > 1:
                return await self._select_proxy_round_robin()
            return self._static_proxy
        
        async with self._lock:
            await self._fetch_proxy()
        
        return self._current_proxy
    
    async def _fetch_proxy(self):
        """从代理池URL获取新的代理"""
        try:
            logger.debug(f"[ProxyPool] 正在从代理池获取新代理: {self._pool_url}")
            
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self._pool_url) as response:
                    if response.status == 200:
                        proxy_text = await response.text()
                        proxy = self._normalize_proxy(proxy_text.strip())
                        
                        # 验证代理格式
                        if self._validate_proxy(proxy):
                            self._current_proxy = proxy
                            self._last_fetch_time = time.time()
                            # 添加到代理列表
                            self.add_proxy(proxy)
                            logger.info(f"[ProxyPool] 成功获取新代理: {proxy}")
                        else:
                            logger.error(f"[ProxyPool] 代理格式无效: {proxy}")
                            # 降级到静态代理
                            if not self._current_proxy:
                                self._current_proxy = self._static_proxy
                    else:
                        logger.error(f"[ProxyPool] 获取代理失败: HTTP {response.status}")
                        # 降级到静态代理
                        if not self._current_proxy:
                            self._current_proxy = self._static_proxy
        
        except asyncio.TimeoutError:
            logger.error("[ProxyPool] 获取代理超时")
            if not self._current_proxy:
                self._current_proxy = self._static_proxy
        
        except Exception as e:
            logger.error(f"[ProxyPool] 获取代理异常: {e}")
            # 降级到静态代理
            if not self._current_proxy:
                self._current_proxy = self._static_proxy
    
    def _validate_proxy(self, proxy: str) -> bool:
        """验证代理格式
        
        Args:
            proxy: 代理URL
        
        Returns:
            是否有效
        """
        if not proxy:
            return False
        
        # 支持的协议
        valid_protocols = ['http://', 'https://', 'socks5://', 'socks5h://']
        
        return any(proxy.startswith(proto) for proto in valid_protocols)

    def _normalize_proxy(self, proxy: str) -> str:
        """标准化代理URL（sock5/socks5 → socks5h://）"""
        if not proxy:
            return proxy

        proxy = proxy.strip()
        if proxy.startswith("sock5h://"):
            proxy = proxy.replace("sock5h://", "socks5h://", 1)
        if proxy.startswith("sock5://"):
            proxy = proxy.replace("sock5://", "socks5://", 1)
        if proxy.startswith("socks5://"):
            return proxy.replace("socks5://", "socks5h://", 1)
        return proxy

    def _looks_like_proxy_url(self, url: str) -> bool:
        """判断URL是否像代理地址（避免误把代理池API当代理）"""
        return url.startswith(("sock5://", "sock5h://", "socks5://", "socks5h://"))
    
    def get_current_proxy(self) -> Optional[str]:
        """获取当前使用的代理（同步方法）
        
        Returns:
            当前代理URL或None
        """
        return self._current_proxy or self._static_proxy


# 全局代理池实例
proxy_pool = ProxyPool()
