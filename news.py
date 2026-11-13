"""主流媒体免费 RSS。新闻只作催化剂和风险过滤，不单独作为开仓理由。"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from httputil import get_text

SH = timezone(timedelta(hours=8))
NEWS_MAX_AGE_HOURS = 4

FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("The Block", "https://www.theblock.co/rss.xml"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
]

# 短代码里容易误伤的不直接当关键词，只用全称。
SYMBOL_ALIASES: dict[str, tuple[str, ...]] = {
    "BTC": ("bitcoin", "btc"),
    "ETH": ("ethereum", "ether"),
    "SOL": ("solana",),
    "UNI": ("uniswap",),
    "BNB": ("binance coin", "binance"),
    "AAVE": ("aave",),
    "ENA": ("ethena",),
    "ZEC": ("zcash", "z cash"),
    "GLD": ("gold etf", "spdr gold", "黄金"),
    "SLV": ("silver etf", "白银"),
    "COIN": ("coinbase",),
    "MSTR": ("microstrategy", "micro strategy"),
    "MSFT": ("microsoft",),
    "TSLA": ("tesla",),
    "NVDA": ("nvidia",),
    "MRNA": ("moderna",),
    "CRCL": ("circle",),
    "SPY": ("s&p 500", "s&p500", "spy etf"),
    "QQQ": ("nasdaq-100", "nasdaq 100"),
    "BABA": ("alibaba",),
    "AMD": ("advanced micro devices",),
    "PLTR": ("palantir",),
    "MU": ("micron",),
    "MRVL": ("marvell",),
}

# 不用太宽的词，如单独的 ETF、regulation。
IMPACT = re.compile(
    r"(FOMC|CPI|PPI|\bNFP\b|non[- ]?farm|interest rate|rate hike|rate cut|rate decision|"
    r"ETF approval|ETF reject(?:ion)?|spot ETF|\bSEC\b|lawsuit|sued|hack(?:ed|er|s)?|"
    r"exploit|breach|withdrawals? paused|insolvency|bankruptcy|default|delist|"
    r"enforcement|subpoena|earnings|revenue miss|guidance cut|liquidation|"
    r"trading halt|sanctions?|"
    r"通胀|加息|降息|美联储|非农|黑客|被盗|漏洞利用|监管处罚|诉讼|禁令|财报|"
    r"暴雷|清算|停提|破产|制裁)",
    re.I,
)

# 会影响大多数品种的宏观事件，命中则暂缓所有自动开仓。
MACRO_EVENT = re.compile(
    r"(FOMC|\bCPI\b|\bPPI\b|\bNFP\b|non[- ]?farm|federal reserve|"
    r"fomc statement|interest rate decision|rate (?:hike|cut|decision)|"
    r"通胀(?:数据|公布)?|非农|美联储(?:决议|利率|议息)?|加息|降息)",
    re.I,
)

# 这些代码即使加词界也容易误伤，不用 ticker 本身。
NO_TICKER_MATCH = {"COIN", "UNI", "SOL", "ENA", "GLD", "SLV", "QQQ", "SPY", "MU"}


def _parse_dt(text: str) -> datetime | None:
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(SH)
    except Exception:
        return None


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _feed_items(source: str, xml_text: str, limit: int = 12) -> list[dict]:
    items = []
    root = ET.fromstring(xml_text)
    for node in root.iter():
        if _local(node.tag).lower() != "item":
            continue
        fields = {_local(c.tag).lower(): (c.text or "").strip() for c in list(node)}
        title = fields.get("title") or ""
        if not title:
            continue
        when = _parse_dt(fields.get("pubdate") or fields.get("date") or fields.get("updated") or "")
        items.append({
            "source": source,
            "title": re.sub(r"\s+", " ", title),
            "summary": re.sub(r"<[^>]+>", "", fields.get("description") or "")[:240],
            "link": fields.get("link") or "",
            "time": when.strftime("%Y-%m-%d %H:%M") + " 上海时区" if when else "",
            "ts": when,
        })
        if len(items) >= limit:
            break
    return items


def fetch_news(proxy: str | None = None, per_feed: int = 10) -> list[dict]:
    rows: list[dict] = []

    def one(src_url):
        src, url = src_url
        try:
            return _feed_items(src, get_text(url, proxy=proxy, timeout=8, retries=1), per_feed)
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(one, f) for f in FEEDS]
        try:
            for fut in as_completed(futs, timeout=10):
                rows.extend(fut.result())
        except Exception:
            # 个别源超时或失败时，用已经拿到的条目继续
            for fut in futs:
                if fut.done() and not fut.cancelled() and fut.exception() is None:
                    rows.extend(fut.result())
    rows.sort(key=lambda x: x.get("ts") or datetime.min.replace(tzinfo=SH), reverse=True)
    return rows


def _base_symbol(symbol: str, name: str = "") -> str:
    raw = (symbol or name or "").strip()
    return raw.split("-")[0].upper() if raw else ""


def _has_key(blob: str, key: str) -> bool:
    if not key:
        return False
    if re.search(r"[\u4e00-\u9fff]", key):
        return key in blob
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(key.lower()) + r"(?![A-Za-z0-9])", blob.lower()) is not None


def _symbol_keys(symbol: str, name: str = "") -> list[str]:
    base = _base_symbol(symbol, name)
    keys = list(SYMBOL_ALIASES.get(base, ()))
    if base and base not in NO_TICKER_MATCH and len(base) >= 3:
        keys.append(base)
    official = (name or "").strip()
    if official and official.upper() not in {base, f"{base}-USD", (symbol or "").upper()}:
        keys.append(official)
    # 去重并保序
    out: list[str] = []
    seen: set[str] = set()
    for k in keys:
        lk = k.lower()
        if lk and lk not in seen:
            seen.add(lk)
            out.append(k)
    return out


def _blob(item: dict) -> str:
    return f"{item.get('title') or ''} {item.get('summary') or ''}"


def _matches_symbol(blob: str, symbol: str, name: str = "") -> bool:
    return any(_has_key(blob, k) for k in _symbol_keys(symbol, name))


def _age_ok(item: dict, now: datetime, max_age_hours: float) -> bool:
    ts = item.get("ts")
    if not ts:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=SH)
    now = now.astimezone(SH) if now.tzinfo else now.replace(tzinfo=SH)
    age = now - ts.astimezone(SH)
    return timedelta(hours=-0.25) <= age <= timedelta(hours=max_age_hours)


def _reason(item: dict, market_wide: bool = False) -> str:
    src = item.get("source") or ""
    when = item.get("time") or ""
    title = item.get("title") or ""
    head = "刚有重大宏观新闻" if market_wide else "刚有相关重大新闻"
    return f"{head}（{src} {when}）：{title}"


def match_news(items: list[dict], symbol: str, name: str, limit: int = 3) -> list[dict]:
    hits = []
    for it in items:
        blob = _blob(it)
        if _matches_symbol(blob, symbol, name):
            hits.append(it)
        elif MACRO_EVENT.search(blob) and _base_symbol(symbol, name) in {"BTC", "ETH"}:
            hits.append(it)
        if len(hits) >= limit:
            break
    return hits


def news_blocks_entry(
    items: list[dict] | None,
    symbol: str,
    name: str = "",
    now: datetime | None = None,
    max_age_hours: float = NEWS_MAX_AGE_HOURS,
) -> str | None:
    """若有刚出的相关重大新闻或全市场宏观事件，返回暂缓开仓的原因；否则返回 None。

    没有新闻或条目没有时间的，不过滤，保持原开仓规则。
    """
    if not items:
        return None
    now = now or datetime.now(SH)
    for it in items:
        if not _age_ok(it, now, max_age_hours):
            continue
        blob = _blob(it)
        if MACRO_EVENT.search(blob):
            return _reason(it, market_wide=True)
        if _matches_symbol(blob, symbol, name) and IMPACT.search(blob):
            return _reason(it)
    return None


def filter_auto_by_news(
    candidates: list[dict],
    items: list[dict] | None,
    now: datetime | None = None,
    max_age_hours: float = NEWS_MAX_AGE_HOURS,
) -> tuple[list[dict], list[tuple[dict, str]]]:
    """在已够格的自动信号上叠加新闻过滤。没有新闻时原样返回，且不把结构标成已处理。"""
    if not items:
        return list(candidates), []
    kept: list[dict] = []
    blocked: list[tuple[dict, str]] = []
    for s in candidates:
        reason = news_blocks_entry(
            items,
            s.get("name") or "",
            (s.get("info") or {}).get("symbol") or "",
            now=now,
            max_age_hours=max_age_hours,
        )
        if reason:
            blocked.append((s, reason))
        else:
            kept.append(s)
    return kept, blocked
