"""
Rate_Limiter API 限流器模块

基于令牌桶算法实现，确保 Binance fapi 请求频率不超过限制。
支持正常速率（1000 次/分钟）和降级速率（500 次/分钟），
当待发送请求队列超过 800 时自动降速。
"""

import threading
import time
import logging

log = logging.getLogger(__name__)


class RateLimitStoppedError(Exception):
    """当 Rate_Limiter 被 stop() 停止后，再调用 acquire() 时抛出。"""
    pass


class RateLimiter:
    """
    基于令牌桶算法的 API 限流器。

    - acquire(): 获取一个请求令牌，令牌不足时阻塞等待
    - pause(): 暂停所有请求发送指定秒数（用于 HTTP 429）
    - stop(): 立即停止所有请求（用于 HTTP 418）
    - get_queue_size(): 返回当前待发送请求数量
    """

    NORMAL_RATE = 1000       # 正常速率：1000 次/分钟
    DEGRADED_RATE = 500      # 降级速率：500 次/分钟
    QUEUE_THRESHOLD = 800    # 队列阈值：超过 800 自动降速

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # 令牌桶状态
        self._tokens: float = self.NORMAL_RATE  # 当前可用令牌数
        self._last_refill_time: float = time.monotonic()  # 上次补充令牌的时间

        # 队列计数器：跟踪当前等待获取令牌的请求数
        self._queue_size: int = 0

        # 暂停控制（HTTP 429）
        self._pause_until: float = 0.0  # 暂停截止时间（monotonic）

        # 停止标志（HTTP 418）
        self._stopped: bool = False

    def _get_current_rate(self) -> int:
        """根据队列大小返回当前有效速率上限。"""
        if self._queue_size > self.QUEUE_THRESHOLD:
            return self.DEGRADED_RATE
        return self.NORMAL_RATE

    def _refill_tokens(self) -> None:
        """根据经过的时间补充令牌。必须在持有锁时调用。"""
        now = time.monotonic()
        elapsed = now - self._last_refill_time
        rate = self._get_current_rate()

        # 每秒补充 rate/60 个令牌
        tokens_per_second = rate / 60.0
        new_tokens = elapsed * tokens_per_second

        # 令牌上限为当前速率值（即一分钟的量）
        self._tokens = min(self._tokens + new_tokens, float(rate))
        self._last_refill_time = now

    def acquire(self) -> None:
        """
        获取一个请求令牌。

        - 若已被 stop() 停止，抛出 RateLimitStoppedError
        - 若处于 pause() 暂停期间，阻塞等待直到暂停结束
        - 若令牌不足，阻塞等待直到令牌可用
        - 队列超过 800 时自动降速至 500/min
        """
        with self._lock:
            # 检查停止标志
            if self._stopped:
                raise RateLimitStoppedError("Rate_Limiter 已停止，所有请求被拒绝")

            # 增加队列计数
            self._queue_size += 1

        try:
            while True:
                with self._lock:
                    # 再次检查停止标志（可能在等待期间被调用）
                    if self._stopped:
                        raise RateLimitStoppedError("Rate_Limiter 已停止，所有请求被拒绝")

                    # 检查暂停状态
                    now = time.monotonic()
                    if now < self._pause_until:
                        wait_time = self._pause_until - now
                    else:
                        wait_time = None

                # 如果处于暂停期间，释放锁后等待
                if wait_time is not None:
                    time.sleep(min(wait_time, 0.5))
                    continue

                with self._lock:
                    # 补充令牌
                    self._refill_tokens()

                    # 尝试获取令牌
                    if self._tokens >= 1.0:
                        self._tokens -= 1.0
                        return  # 成功获取令牌

                    # 令牌不足，计算需要等待的时间
                    rate = self._get_current_rate()
                    tokens_per_second = rate / 60.0
                    wait_for_token = (1.0 - self._tokens) / tokens_per_second

                # 释放锁后等待
                time.sleep(min(wait_for_token, 0.1))

        finally:
            with self._lock:
                self._queue_size -= 1

    def pause(self, seconds: int = 30) -> None:
        """
        暂停所有请求发送指定秒数（用于 HTTP 429 响应）。

        参数:
            seconds: 暂停秒数，默认 30 秒
        """
        with self._lock:
            self._pause_until = time.monotonic() + seconds
            log.warning(f"Rate_Limiter 暂停 {seconds} 秒（HTTP 429）")

    def stop(self) -> None:
        """
        立即停止所有请求（用于 HTTP 418 响应）。

        调用后，所有后续 acquire() 调用将抛出 RateLimitStoppedError。
        """
        with self._lock:
            self._stopped = True
            log.critical("Rate_Limiter 已停止所有请求（HTTP 418 IP 封禁）")

    def get_queue_size(self) -> int:
        """返回当前待发送请求数量。"""
        with self._lock:
            return self._queue_size
