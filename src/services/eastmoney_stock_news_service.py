"""Free Eastmoney news and announcement retrieval for mainland China stocks."""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from data_provider.base import normalize_stock_code
from src.search_service import SearchResponse, SearchResult


logger = logging.getLogger(__name__)


class EastmoneyStockNewsService:
    """Fetch stock-specific news and announcements without a search API key."""

    provider_name = "Eastmoney-Free"
    _cache: Dict[Tuple[str, int, int], Tuple[float, SearchResponse]] = {}
    _cache_lock = threading.Lock()
    _network_slots = threading.BoundedSemaphore(value=4)

    def __init__(self, *, timeout_seconds: float = 8.0, cache_ttl_seconds: int = 900):
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.cache_ttl_seconds = max(0, int(cache_ttl_seconds))

    def fetch_stock_news(
        self,
        stock_code: str,
        stock_name: str,
        *,
        days: int = 3,
        limit: int = 8,
    ) -> SearchResponse:
        """Return recent, directly related news and announcements for one A-share."""
        code = normalize_stock_code(stock_code)
        query = f"{stock_name} {code} 个股新闻与公告"
        if not self._is_mainland_stock_code(code):
            return SearchResponse(
                query=query,
                results=[],
                provider=self.provider_name,
                success=False,
                error_message="免费个股资讯仅支持沪深京 A 股",
            )

        days = max(1, int(days))
        limit = max(1, int(limit))
        cache_key = (code, days, limit)
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        started = time.monotonic()
        errors: List[str] = []
        results: List[SearchResult] = []
        try:
            results.extend(self._fetch_news(code, stock_name, days=days))
        except Exception as exc:
            errors.append(f"个股新闻获取失败: {type(exc).__name__}")
            logger.warning("%s(%s) Eastmoney news fetch failed: %s", stock_name, code, exc)

        try:
            results.extend(self._fetch_announcements(code, stock_name, days=days))
        except Exception as exc:
            errors.append(f"公司公告获取失败: {type(exc).__name__}")
            logger.warning("%s(%s) Eastmoney announcement fetch failed: %s", stock_name, code, exc)

        deduped = self._dedupe_and_sort(results)[:limit]
        response = SearchResponse(
            query=query,
            results=deduped,
            provider=self.provider_name,
            success=bool(deduped),
            error_message="; ".join(errors) if errors else None,
            search_time=time.monotonic() - started,
        )
        if deduped or not errors:
            self._set_cached(cache_key, response)
        return self._clone_response(response)

    def _fetch_news(self, code: str, stock_name: str, *, days: int) -> List[SearchResult]:
        frame = self._call_akshare("stock_news_em", symbol=code)
        results: List[SearchResult] = []
        if frame is None or getattr(frame, "empty", True):
            return results
        for _, row in frame.iterrows():
            title = self._clean_text(row.get("新闻标题"))
            snippet = self._clean_text(row.get("新闻内容"))
            if not title or not self._is_direct_match(title, code, stock_name):
                continue
            published = self._parse_datetime(row.get("发布时间"))
            if not self._within_window(published, days):
                continue
            results.append(
                SearchResult(
                    title=title,
                    snippet=snippet[:500],
                    url=self._clean_text(row.get("新闻链接")),
                    source=self._clean_text(row.get("文章来源")) or "东方财富",
                    published_date=self._format_datetime(published),
                    relevance_score=95,
                    relevance_category="direct_stock_news",
                    relevance_reasons=["标题直接命中股票名称/代码"],
                )
            )
        return results

    def _fetch_announcements(self, code: str, stock_name: str, *, days: int) -> List[SearchResult]:
        start_date = datetime.now().date() - timedelta(days=max(0, days - 1))
        end_date = datetime.now().date()
        try:
            frame = self._call_akshare(
                "stock_individual_notice_report",
                security=code,
                symbol="全部",
                begin_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
            )
        except KeyError as exc:
            # AkShare currently raises KeyError('code') when the upstream page
            # has zero announcement rows instead of returning an empty frame.
            if str(exc).strip("'") in {"代码", "code"}:
                return []
            raise
        results: List[SearchResult] = []
        if frame is None or getattr(frame, "empty", True):
            return results
        for _, row in frame.iterrows():
            title = self._clean_text(row.get("公告标题"))
            published = self._parse_datetime(row.get("公告日期"))
            if not title or not self._within_window(published, days):
                continue
            category = self._clean_text(row.get("公告类型"))
            results.append(
                SearchResult(
                    title=title,
                    snippet=f"公告类型：{category}" if category else f"{stock_name}公司公告",
                    url=self._clean_text(row.get("网址")),
                    source="东方财富公告",
                    published_date=self._format_datetime(published),
                    relevance_score=100,
                    relevance_category="company_announcement",
                    relevance_reasons=[f"公告接口按证券代码 {code} 精确查询"],
                )
            )
        return results

    def _call_akshare(self, method_name: str, **kwargs: Any) -> Any:
        """Call one AkShare adapter with a bounded daemon-thread timeout."""
        if not self._network_slots.acquire(blocking=False):
            raise TimeoutError("free stock news concurrency limit reached")
        result: List[Any] = []
        errors: List[Exception] = []

        def run() -> None:
            try:
                import akshare as ak

                method = getattr(ak, method_name)
                result.append(method(**kwargs))
            except Exception as exc:  # propagate adapter errors to fail-open caller
                errors.append(exc)
            finally:
                self._network_slots.release()

        worker = threading.Thread(
            target=run,
            name=f"eastmoney-{method_name}",
            daemon=True,
        )
        worker.start()
        worker.join(timeout=self.timeout_seconds)
        if worker.is_alive():
            raise TimeoutError(f"{method_name} timed out after {self.timeout_seconds:g}s")
        if errors:
            raise errors[0]
        return result[0] if result else None

    @staticmethod
    def _clean_text(value: Any) -> str:
        text = html.unescape(str(value or ""))
        text = re.sub(r"<[^>]+>", "", text)
        return " ".join(text.replace("\u3000", " ").split())

    @classmethod
    def _is_direct_match(cls, title: str, code: str, stock_name: str) -> bool:
        # Search results often mention the queried company only in a long article
        # body. Requiring a title hit avoids feeding tangential fund/sector news
        # into a stock-specific analysis.
        combined = title.lower()
        name = cls._clean_text(stock_name).lower()
        return bool(code in combined or (len(name) >= 2 and name in combined))

    @staticmethod
    def _is_mainland_stock_code(code: str) -> bool:
        return bool(re.fullmatch(r"\d{6}", code))

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time())
        text = str(value or "").strip()
        if not text:
            return None
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed
        except ValueError:
            pass
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
            try:
                return datetime.strptime(text, pattern)
            except ValueError:
                continue
        return None

    @staticmethod
    def _format_datetime(value: Optional[datetime]) -> Optional[str]:
        return value.strftime("%Y-%m-%d %H:%M:%S") if value else None

    @staticmethod
    def _within_window(value: Optional[datetime], days: int) -> bool:
        if value is None:
            return True
        today = datetime.now().date()
        earliest = today - timedelta(days=max(0, days - 1))
        return earliest <= value.date() <= today + timedelta(days=1)

    @classmethod
    def _dedupe_and_sort(cls, results: Iterable[SearchResult]) -> List[SearchResult]:
        seen = set()
        deduped: List[SearchResult] = []
        for item in results:
            key = item.url or (item.title, item.published_date)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        deduped.sort(key=lambda item: item.published_date or "", reverse=True)
        return deduped

    @classmethod
    def _clone_response(cls, response: SearchResponse) -> SearchResponse:
        return replace(response, results=[replace(item) for item in response.results])

    def _get_cached(self, key: Tuple[str, int, int]) -> Optional[SearchResponse]:
        if self.cache_ttl_seconds <= 0:
            return None
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is None:
                return None
            cached_at, response = cached
            if time.monotonic() - cached_at > self.cache_ttl_seconds:
                self._cache.pop(key, None)
                return None
            return self._clone_response(response)

    def _set_cached(self, key: Tuple[str, int, int], response: SearchResponse) -> None:
        if self.cache_ttl_seconds <= 0:
            return
        with self._cache_lock:
            now = time.monotonic()
            expired = [
                cached_key
                for cached_key, (cached_at, _) in self._cache.items()
                if now - cached_at > self.cache_ttl_seconds
            ]
            for cached_key in expired:
                self._cache.pop(cached_key, None)
            if len(self._cache) >= 512:
                oldest_key = min(self._cache, key=lambda cached_key: self._cache[cached_key][0])
                self._cache.pop(oldest_key, None)
            self._cache[key] = (now, self._clone_response(response))
