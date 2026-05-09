"""
Rate_Limiter 单元测试

测试令牌桶限流器的核心行为：
- acquire() 正常获取令牌
- pause() 暂停后 acquire() 阻塞
- stop() 后 acquire() 抛出异常
- get_queue_size() 返回正确的队列大小
- 队列超过阈值时自动降速
"""

import threading
import time

import pytest

from src.infra.rate_limiter import RateLimiter, RateLimitStoppedError


class TestRateLimiterBasic:
    """基础功能测试"""

    def test_acquire_succeeds(self):
        """正常情况下 acquire() 应成功返回"""
        limiter = RateLimiter()
        # 初始令牌充足，应立即返回
        limiter.acquire()

    def test_multiple_acquires_succeed(self):
        """连续多次 acquire() 应成功（令牌充足时）"""
        limiter = RateLimiter()
        for _ in range(10):
            limiter.acquire()

    def test_get_queue_size_initial(self):
        """初始队列大小应为 0"""
        limiter = RateLimiter()
        assert limiter.get_queue_size() == 0

    def test_get_queue_size_during_acquire(self):
        """acquire() 等待期间队列大小应增加"""
        limiter = RateLimiter()
        # 耗尽令牌
        limiter._tokens = 0.0
        queue_sizes = []

        def slow_acquire():
            queue_sizes.append(limiter.get_queue_size())
            limiter.acquire()

        # 先设置少量令牌让线程能完成
        limiter._tokens = 0.0

        t = threading.Thread(target=slow_acquire)
        t.start()
        time.sleep(0.05)
        # 线程应在等待令牌，队列大小应为 1
        assert limiter.get_queue_size() == 1
        # 补充令牌让线程完成
        with limiter._lock:
            limiter._tokens = 10.0
        t.join(timeout=5)


class TestRateLimiterStop:
    """stop() 行为测试"""

    def test_stop_then_acquire_raises(self):
        """stop() 后调用 acquire() 应抛出 RateLimitStoppedError"""
        limiter = RateLimiter()
        limiter.stop()
        with pytest.raises(RateLimitStoppedError):
            limiter.acquire()

    def test_stop_during_waiting_acquire(self):
        """在 acquire() 等待期间调用 stop() 应使其抛出异常"""
        limiter = RateLimiter()
        # 使用 pause 来阻塞 acquire，这样令牌补充不会让它提前完成
        limiter.pause(seconds=60)
        errors = []

        def waiting_acquire():
            try:
                limiter.acquire()
            except RateLimitStoppedError:
                errors.append("stopped")

        t = threading.Thread(target=waiting_acquire)
        t.start()
        time.sleep(0.2)
        limiter.stop()
        t.join(timeout=5)
        assert "stopped" in errors

    def test_stop_is_permanent(self):
        """stop() 后多次调用 acquire() 都应抛出异常"""
        limiter = RateLimiter()
        limiter.stop()
        for _ in range(3):
            with pytest.raises(RateLimitStoppedError):
                limiter.acquire()


class TestRateLimiterPause:
    """pause() 行为测试"""

    def test_pause_blocks_acquire(self):
        """pause() 后 acquire() 应阻塞直到暂停结束"""
        limiter = RateLimiter()
        limiter.pause(seconds=1)

        start = time.monotonic()
        limiter.acquire()
        elapsed = time.monotonic() - start

        # 应至少等待约 1 秒（允许一些误差）
        assert elapsed >= 0.8, f"暂停期间 acquire 应阻塞，实际等待 {elapsed:.2f}s"

    def test_pause_with_default_seconds(self):
        """pause() 默认暂停 30 秒"""
        limiter = RateLimiter()
        limiter.pause()
        # 验证暂停截止时间大约在 30 秒后
        now = time.monotonic()
        assert limiter._pause_until > now + 25


class TestRateLimiterDegradation:
    """自动降速测试"""

    def test_normal_rate_when_queue_below_threshold(self):
        """队列低于阈值时使用正常速率"""
        limiter = RateLimiter()
        limiter._queue_size = 100
        assert limiter._get_current_rate() == RateLimiter.NORMAL_RATE

    def test_degraded_rate_when_queue_above_threshold(self):
        """队列超过阈值时自动降速"""
        limiter = RateLimiter()
        limiter._queue_size = 801
        assert limiter._get_current_rate() == RateLimiter.DEGRADED_RATE

    def test_normal_rate_at_threshold(self):
        """队列恰好等于阈值时使用正常速率"""
        limiter = RateLimiter()
        limiter._queue_size = 800
        assert limiter._get_current_rate() == RateLimiter.NORMAL_RATE

    def test_rate_constants(self):
        """验证速率常量值"""
        assert RateLimiter.NORMAL_RATE == 1000
        assert RateLimiter.DEGRADED_RATE == 500
        assert RateLimiter.QUEUE_THRESHOLD == 800


# 需求 7.3: HTTP 429 响应处理——pause(30) 后 acquire 阻塞
class TestRateLimiterHTTP429:
    """HTTP 429 场景测试：pause(30) 后 acquire() 应阻塞等待。"""

    def test_pause_30_blocks_acquire(self):
        """HTTP 429 场景：pause(30) 后 acquire() 应阻塞，不会立即返回"""
        limiter = RateLimiter()
        limiter.pause(30)

        acquired = threading.Event()

        def try_acquire():
            try:
                limiter.acquire()
                acquired.set()
            except RateLimitStoppedError:
                pass  # 清理时 stop() 触发的异常，忽略即可

        t = threading.Thread(target=try_acquire)
        t.start()

        # 等待 0.5 秒，acquire 应仍在阻塞（因为暂停了 30 秒）
        assert not acquired.wait(timeout=0.5), (
            "pause(30) 后 acquire() 不应在 0.5 秒内返回"
        )

        # 清理：stop 让线程退出，避免测试挂起
        limiter.stop()
        t.join(timeout=2)

    def test_pause_short_then_acquire_succeeds(self):
        """HTTP 429 场景：短暂 pause 后 acquire() 应在暂停结束后成功返回"""
        limiter = RateLimiter()
        limiter.pause(1)  # 暂停 1 秒

        start = time.monotonic()
        limiter.acquire()
        elapsed = time.monotonic() - start

        # 应至少等待约 1 秒
        assert elapsed >= 0.8, (
            f"pause(1) 后 acquire 应至少等待 ~1 秒，实际等待 {elapsed:.2f}s"
        )

    def test_pause_updates_pause_until(self):
        """pause(30) 应正确设置 _pause_until 时间戳"""
        limiter = RateLimiter()
        before = time.monotonic()
        limiter.pause(30)
        after = time.monotonic()

        # _pause_until 应在 [before+30, after+30] 范围内
        assert limiter._pause_until >= before + 30 - 0.1
        assert limiter._pause_until <= after + 30 + 0.1


# 需求 7.4: HTTP 418 响应处理——stop() 后 acquire 抛出 RateLimitStoppedError
class TestRateLimiterHTTP418:
    """HTTP 418 场景测试：stop() 后 acquire() 应抛出 RateLimitStoppedError。"""

    def test_stop_raises_rate_limit_stopped_error(self):
        """HTTP 418 场景：stop() 后 acquire() 应抛出 RateLimitStoppedError"""
        limiter = RateLimiter()
        limiter.stop()

        with pytest.raises(RateLimitStoppedError, match="已停止"):
            limiter.acquire()

    def test_stop_sets_stopped_flag(self):
        """stop() 应将 _stopped 标志设为 True"""
        limiter = RateLimiter()
        assert limiter._stopped is False
        limiter.stop()
        assert limiter._stopped is True

    def test_stop_interrupts_paused_acquire(self):
        """HTTP 418 场景：即使处于 pause 状态，stop() 也应中断 acquire()"""
        limiter = RateLimiter()
        limiter.pause(60)  # 先暂停 60 秒

        errors = []

        def try_acquire():
            try:
                limiter.acquire()
            except RateLimitStoppedError:
                errors.append("stopped")

        t = threading.Thread(target=try_acquire)
        t.start()
        time.sleep(0.3)  # 等待线程进入 pause 等待

        # stop() 应中断等待
        limiter.stop()
        t.join(timeout=2)

        assert "stopped" in errors, (
            "stop() 应中断处于 pause 等待中的 acquire()"
        )

    def test_stop_all_subsequent_acquires_fail(self):
        """HTTP 418 场景：stop() 后所有后续 acquire() 调用都应失败"""
        limiter = RateLimiter()
        limiter.stop()

        for i in range(5):
            with pytest.raises(RateLimitStoppedError):
                limiter.acquire()
