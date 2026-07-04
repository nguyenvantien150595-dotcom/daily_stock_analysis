"""Tests for the keyless Eastmoney A-share news fallback."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

from src.core.pipeline import StockAnalysisPipeline
from src.search_service import SearchResponse, SearchResult
from src.services.eastmoney_stock_news_service import EastmoneyStockNewsService


class EastmoneyStockNewsServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        EastmoneyStockNewsService._cache.clear()

    @staticmethod
    def _news_frame(rows) -> pd.DataFrame:
        return pd.DataFrame(rows)

    @staticmethod
    def _notice_frame(rows) -> pd.DataFrame:
        return pd.DataFrame(rows)

    def test_fetches_direct_news_and_announcements_only(self) -> None:
        now = datetime.now()
        service = EastmoneyStockNewsService(timeout_seconds=6, cache_ttl_seconds=0)
        service._call_akshare = MagicMock(
            side_effect=[
            self._news_frame(
                [
                    {
                        "新闻标题": "<em>贵州茅台</em>完成年度分红",
                        "新闻内容": "贵州茅台披露最新分红方案",
                        "发布时间": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "文章来源": "测试财经",
                        "新闻链接": "https://example.com/news-1",
                    },
                    {
                        "新闻标题": "与目标公司无关的基金新闻",
                        "新闻内容": "正文偶然提到贵州茅台，不应进入个股分析",
                        "发布时间": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "文章来源": "测试财经",
                        "新闻链接": "https://example.com/news-2",
                    },
                    {
                        "新闻标题": "贵州茅台旧闻",
                        "新闻内容": "过期新闻",
                        "发布时间": (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S"),
                        "文章来源": "测试财经",
                        "新闻链接": "https://example.com/news-old",
                    },
                ]
            ),
            self._notice_frame(
                [
                    {
                        "公告标题": "贵州茅台权益分派实施公告",
                        "公告日期": now.strftime("%Y-%m-%d"),
                        "公告类型": "重大事项",
                        "网址": "https://example.com/notice-1",
                    }
                ]
            ),
        ])

        response = service.fetch_stock_news("600519", "贵州茅台", days=3, limit=8)

        self.assertTrue(response.success)
        self.assertEqual(len(response.results), 2)
        self.assertEqual(
            {item.relevance_category for item in response.results},
            {"direct_stock_news", "company_announcement"},
        )
        self.assertEqual(service._call_akshare.call_count, 2)
        self.assertEqual(service._call_akshare.call_args_list[0].args[0], "stock_news_em")
        self.assertEqual(service._call_akshare.call_args_list[1].args[0], "stock_individual_notice_report")

    def test_news_failure_keeps_announcement_fallback(self) -> None:
        now = datetime.now()
        service = EastmoneyStockNewsService(cache_ttl_seconds=0)
        service._call_akshare = MagicMock(side_effect=[
            TimeoutError("timeout"),
            self._notice_frame(
                [
                    {
                        "公告标题": "宁德时代董事会公告",
                        "公告日期": now.strftime("%Y-%m-%d"),
                        "公告类型": "重大事项",
                        "网址": "https://example.com/notice-2",
                    }
                ]
            ),
        ])

        response = service.fetch_stock_news("300750", "宁德时代", days=3)

        self.assertTrue(response.success)
        self.assertEqual(len(response.results), 1)
        self.assertIn("个股新闻获取失败", response.error_message or "")

    def test_cache_avoids_repeated_network_calls(self) -> None:
        now = datetime.now()
        service = EastmoneyStockNewsService(cache_ttl_seconds=900)
        service._call_akshare = MagicMock(side_effect=[
            self._news_frame(
                [{
                    "新闻标题": "比亚迪发布新产品",
                    "新闻内容": "比亚迪新闻",
                    "发布时间": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "文章来源": "测试财经",
                    "新闻链接": "https://example.com/news-3",
                }]
            ),
            self._notice_frame([]),
        ])

        first = service.fetch_stock_news("002594", "比亚迪", days=3)
        second = service.fetch_stock_news("002594", "比亚迪", days=3)

        self.assertEqual(len(first.results), 1)
        self.assertEqual(len(second.results), 1)
        self.assertEqual(service._call_akshare.call_count, 2)


class FreeNewsPipelineIntegrationTestCase(unittest.TestCase):
    def test_pipeline_persists_and_formats_free_stock_news(self) -> None:
        response = SearchResponse(
            query="贵州茅台 600519 个股新闻与公告",
            provider="Eastmoney-Free",
            results=[
                SearchResult(
                    title="贵州茅台分红公告",
                    snippet="公告摘要",
                    url="https://example.com/notice",
                    source="东方财富公告",
                    published_date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
            ],
        )
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.config = SimpleNamespace(get_effective_news_window_days=lambda: 3)
        pipeline.eastmoney_stock_news_service = MagicMock()
        pipeline.eastmoney_stock_news_service.fetch_stock_news.return_value = response
        pipeline.db = MagicMock()
        pipeline._build_query_context = MagicMock(return_value={"query_id": "q-1"})

        context, count = pipeline._load_free_a_share_news_context(
            code="600519",
            stock_name="贵州茅台",
            market="cn",
            query_id="q-1",
        )

        self.assertEqual(count, 1)
        self.assertIn("贵州茅台分红公告", context or "")
        pipeline.db.save_news_intel.assert_called_once()

    def test_pipeline_skips_non_mainland_market(self) -> None:
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.eastmoney_stock_news_service = MagicMock()

        context, count = pipeline._load_free_a_share_news_context(
            code="AAPL",
            stock_name="Apple",
            market="us",
            query_id="q-1",
        )

        self.assertIsNone(context)
        self.assertEqual(count, 0)
        pipeline.eastmoney_stock_news_service.fetch_stock_news.assert_not_called()


if __name__ == "__main__":
    unittest.main()
