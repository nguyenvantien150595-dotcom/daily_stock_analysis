# -*- coding: utf-8 -*-
"""选股结果归档与 T+1/T+5 表现回填。

目标：把每次选股（每日定时 + 手动触发）的候选追加存档，随后自动回填
T+1/T+5 实际涨跌，形成各策略可验证的长期成绩单，并对比
「LLM 重排 Top3」与「因子分 Top3」的真实表现差异。

设计约束：
- 独立 SQLite（data/screen_archive.db），不动主库 schema，失败绝不影响主流程。
- 云端 GitHub Actions 容器磁盘不持久，归档仅在本地开启（SCREEN_ARCHIVE_ENABLED）。
- 收益基准统一为入选日收盘价（T0 close），T+N 收益 = T+N close / T0 close - 1。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "screen_archive.db"
_BACKFILL_MAX_CODES_PER_RUN = 80
_DAILY_MARKET = "cn"
_DAILY_MAX_RESULTS = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS screen_picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    run_ts TEXT NOT NULL,
    trigger TEXT NOT NULL,
    strategy TEXT NOT NULL,
    market TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    rank INTEGER,
    factor_rank INTEGER,
    score REAL,
    screen_score REAL,
    llm_score REAL,
    llm_confidence REAL,
    price REAL,
    base_close REAL,
    t1_close REAL,
    t1_ret REAL,
    t5_close REAL,
    t5_ret REAL,
    UNIQUE(run_date, trigger, strategy, code)
);
CREATE INDEX IF NOT EXISTS idx_screen_picks_pending
    ON screen_picks(run_date) WHERE t5_ret IS NULL;
"""


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=30)
    conn.executescript(_SCHEMA)
    return conn


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def archive_screen_result(result: Dict[str, Any], trigger: str) -> int:
    """把一次选股结果的候选追加入档，返回新增行数。

    同一 (run_date, trigger, strategy, code) 只记第一次，重复运行不产生重复样本。
    """
    candidates = result.get("candidates") or []
    if not candidates:
        return 0
    strategy = str(result.get("strategy") or "unknown")
    market = str(result.get("market") or _DAILY_MARKET)
    now = datetime.now()
    run_date = now.strftime("%Y-%m-%d")
    run_ts = now.strftime("%Y-%m-%d %H:%M:%S")

    # 因子分排名：按 screen_score 降序；缺失时沿用最终 rank，保证可对比。
    by_factor = sorted(
        range(len(candidates)),
        key=lambda i: -(_to_float(candidates[i].get("screen_score")) or float("-inf")),
    )
    factor_rank_of = {idx: pos + 1 for pos, idx in enumerate(by_factor)}

    inserted = 0
    with _connect() as conn:
        for i, cand in enumerate(candidates):
            code = str(cand.get("code") or "").strip()
            if not code:
                continue
            cur = conn.execute(
                """INSERT OR IGNORE INTO screen_picks
                   (run_date, run_ts, trigger, strategy, market, code, name,
                    rank, factor_rank, score, screen_score, llm_score, llm_confidence, price)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_date, run_ts, trigger, strategy, market, code,
                    str(cand.get("name") or ""),
                    int(cand.get("rank") or (i + 1)),
                    (
                        factor_rank_of[i]
                        if _to_float(cand.get("screen_score")) is not None
                        else int(cand.get("rank") or (i + 1))
                    ),
                    _to_float(cand.get("score")),
                    _to_float(cand.get("screen_score")),
                    _to_float(cand.get("llm_score")),
                    _to_float(cand.get("llm_confidence")),
                    _to_float(cand.get("price")),
                ),
            )
            inserted += cur.rowcount
    if inserted:
        logger.info("[选股归档] %s/%s 新增 %d 条 (trigger=%s)", strategy, market, inserted, trigger)
    return inserted


def backfill_returns() -> Dict[str, int]:
    """为缺 T+1/T+5 的存档行回填实际收益（按代码批量取日线）。"""
    from data_provider.base import DataFetcherManager

    today = date.today().strftime("%Y-%m-%d")
    with _connect() as conn:
        rows = conn.execute(
            """SELECT DISTINCT code FROM screen_picks
               WHERE t5_ret IS NULL AND run_date < ? LIMIT ?""",
            (today, _BACKFILL_MAX_CODES_PER_RUN),
        ).fetchall()
    codes = [r[0] for r in rows]
    if not codes:
        return {"codes": 0, "updated": 0}

    manager = DataFetcherManager()
    updated = 0
    for code in codes:
        try:
            start = (date.today() - timedelta(days=45)).strftime("%Y%m%d")
            df, _source = manager.get_daily_data(code, start_date=start, days=45)
            if df is None or df.empty or "close" not in df.columns:
                continue
            df = df.dropna(subset=["close"]).copy()
            df["date"] = df["date"].astype(str).str.slice(0, 10).str.replace("/", "-")
            dates = df["date"].tolist()
            closes = [float(v) for v in df["close"].tolist()]
        except Exception as exc:  # noqa: BLE001 - 单代码失败不阻塞其余回填
            logger.debug("[选股归档] 回填取数失败 %s: %s", code, exc)
            continue

        with _connect() as conn:
            pending = conn.execute(
                """SELECT id, run_date FROM screen_picks
                   WHERE code = ? AND t5_ret IS NULL AND run_date < ?""",
                (code, today),
            ).fetchall()
            for row_id, run_date in pending:
                # T0 = 入选日或其前最近一个交易日收盘
                base_idx = None
                for i, d in enumerate(dates):
                    if d <= run_date:
                        base_idx = i
                    else:
                        break
                if base_idx is None or closes[base_idx] <= 0:
                    continue
                base_close = closes[base_idx]
                after = closes[base_idx + 1:]
                t1_close = after[0] if len(after) >= 1 else None
                t5_close = after[4] if len(after) >= 5 else None
                t1_ret = (t1_close / base_close - 1) * 100 if t1_close else None
                t5_ret = (t5_close / base_close - 1) * 100 if t5_close else None
                if t1_close is None and t5_close is None:
                    continue
                conn.execute(
                    """UPDATE screen_picks
                       SET base_close = ?,
                           t1_close = COALESCE(?, t1_close),
                           t1_ret   = COALESCE(?, t1_ret),
                           t5_close = COALESCE(?, t5_close),
                           t5_ret   = COALESCE(?, t5_ret)
                       WHERE id = ?""",
                    (base_close, t1_close, t1_ret, t5_close, t5_ret, row_id),
                )
                updated += 1
    logger.info("[选股归档] 回填完成: %d 代码, %d 行更新", len(codes), updated)
    return {"codes": len(codes), "updated": updated}


def _fmt_pct(value: Optional[float]) -> str:
    return f"{value:+.2f}%" if value is not None else "-"


def build_report_card_text(days: int = 90) -> str:
    """生成策略成绩单文本（近 N 天，已回填样本）。"""
    since = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    lines: List[str] = []
    with _connect() as conn:
        stats = conn.execute(
            """SELECT strategy,
                      COUNT(t1_ret), AVG(t1_ret),
                      AVG(CASE WHEN t1_ret > 0 THEN 100.0 ELSE 0 END),
                      COUNT(t5_ret), AVG(t5_ret),
                      AVG(CASE WHEN t5_ret > 0 THEN 100.0 ELSE 0 END)
               FROM screen_picks
               WHERE run_date >= ? AND t1_ret IS NOT NULL
               GROUP BY strategy ORDER BY AVG(t5_ret) DESC""",
            (since,),
        ).fetchall()
        if stats:
            lines.append(f"🏆 策略成绩单（近{days}天，T+N收益基准=入选日收盘）")
            for s, n1, avg1, win1, n5, avg5, win5 in stats:
                lines.append(
                    f"- {s}: T1 {_fmt_pct(avg1)}/胜{win1:.0f}% (n={n1}) | "
                    f"T5 {_fmt_pct(avg5)}/胜{win5:.0f}% (n={n5})"
                )
            llm_vs = conn.execute(
                """SELECT AVG(CASE WHEN rank <= 3 THEN t5_ret END),
                          AVG(CASE WHEN factor_rank <= 3 THEN t5_ret END),
                          COUNT(CASE WHEN rank <= 3 THEN t5_ret END)
                   FROM screen_picks WHERE run_date >= ? AND t5_ret IS NOT NULL""",
                (since,),
            ).fetchone()
            if llm_vs and llm_vs[2]:
                lines.append(
                    f"🤖 LLM重排Top3 T5均 {_fmt_pct(llm_vs[0])} vs 因子Top3 {_fmt_pct(llm_vs[1])}"
                    f" (n={llm_vs[2]})"
                )
        else:
            lines.append(f"🏆 策略成绩单：样本回填中（选股入档后 1/5 个交易日起可评分）")
    return "\n".join(lines)


def _today_picks_text() -> str:
    today = date.today().strftime("%Y-%m-%d")
    lines: List[str] = []
    with _connect() as conn:
        rows = conn.execute(
            """SELECT strategy, code, name, rank, score FROM screen_picks
               WHERE run_date = ? AND trigger = 'daily'
               ORDER BY strategy, rank""",
            (today,),
        ).fetchall()
    current = None
    for strategy, code, name, rank, score in rows:
        if strategy != current:
            lines.append(f"◆ {strategy}:")
            current = strategy
        score_txt = f" {score:.0f}分" if score is not None else ""
        lines.append(f"   {rank}. {name}({code}){score_txt}")
    return "\n".join(lines) if lines else "（今日无候选）"


def run_daily_screen_archive(config: Any) -> None:
    """每日选股归档主流程：全策略选股 → 入档 → 回填 → 成绩单推送。"""
    from src.services.alphasift_service import AlphaSiftService

    service = AlphaSiftService(config)
    try:
        raw = service.strategies()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[选股归档] 获取策略列表失败，跳过本日归档: %s", exc)
        return
    items = raw.get("strategies") if isinstance(raw, dict) else raw
    strategy_ids = [
        str(it.get("id") or it.get("name"))
        for it in (items or [])
        if isinstance(it, dict) and (it.get("id") or it.get("name"))
    ]
    if not strategy_ids:
        logger.warning("[选股归档] 无可用策略，跳过")
        return

    ok, failed = 0, 0
    for sid in strategy_ids:
        try:
            result = service.screen(strategy=sid, market=_DAILY_MARKET, max_results=_DAILY_MAX_RESULTS)
            archive_screen_result(result, trigger="daily")
            ok += 1
        except Exception as exc:  # noqa: BLE001 - 单策略失败不影响其余
            failed += 1
            logger.warning("[选股归档] 策略 %s 选股失败: %s", sid, exc)

    try:
        backfill_returns()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[选股归档] 回填失败: %s", exc)

    # 组装并推送：今日选股 + 成绩单
    try:
        content = "\n".join([
            f"📋 每日选股归档 {date.today().strftime('%Y-%m-%d')}"
            f"（{ok}/{len(strategy_ids)} 策略成功）",
            "",
            _today_picks_text(),
            "",
            build_report_card_text(),
            "",
            "⚠️ 仅为策略跟踪实验数据，不构成投资建议。",
        ])
        from src.notification import get_notification_service

        get_notification_service().send(content, dedup_key=f"screen_archive_{date.today()}")
        logger.info("[选股归档] 完成并已推送 (成功 %d, 失败 %d)", ok, failed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[选股归档] 推送失败(数据已入档): %s", exc)
