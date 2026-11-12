"""HTTP 抓取层：带超时与有限重试。"""
import logging
import threading
import time

import httpx

from . import config

logger = logging.getLogger(__name__)

_client: httpx.Client | None = None
_client_lock = threading.Lock()


class FetchError(Exception):
    """页面抓取失败（网络错误或非 2xx 响应）。"""


def _shared_client() -> httpx.Client:
    """复用连接池的共享客户端。

    一次同步会连续抓取成百上千条详情页；每条都新建 TCP 连接会显著拖慢长
    引用链的采集，因此复用同一个 Client（httpx.Client 可跨线程共享）。
    """
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(follow_redirects=True)
        return _client


def fetch(url: str) -> str:
    """抓取页面文本。对网络错误和 5xx 做有限次重试，4xx 立即失败。"""
    last_error: Exception | None = None
    for attempt in range(config.FETCH_RETRIES + 1):
        try:
            resp = _shared_client().get(url, timeout=config.FETCH_TIMEOUT)
            if resp.status_code >= 500:
                raise FetchError(f"服务端错误 HTTP {resp.status_code}")
            if resp.status_code != 200:
                # 404 等客户端错误不重试
                raise FetchError(f"HTTP {resp.status_code}")
            resp.encoding = resp.charset_encoding or "utf-8"
            return resp.text
        except (httpx.HTTPError, FetchError) as exc:
            last_error = exc
            retryable = isinstance(exc, httpx.HTTPError) or "5" in str(exc)
            if not retryable or attempt == config.FETCH_RETRIES:
                break
            sleep_for = config.RETRY_BACKOFF * (attempt + 1)
            logger.warning("抓取 %s 第 %d 次失败：%s，%.1fs 后重试",
                           url, attempt + 1, exc, sleep_for)
            time.sleep(sleep_for)

    raise FetchError(f"抓取 {url} 失败：{last_error}")
