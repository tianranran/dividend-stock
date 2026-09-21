from __future__ import annotations

import json
import math
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STRATEGY_PATH = ROOT / "config" / "strategy.json"
QUOTES_PATH = ROOT / "data" / "quotes.json"
DIVIDENDS_PATH = ROOT / "data" / "dividends.json"
PUBLIC_TEXT_PATHS = [
    ROOT / "dist" / "index.html",
    ROOT / "dist" / "assets" / "stocks.js",
    ROOT / "config" / "strategy.json",
]
PRIVATE_MARKERS = (
    "正式持仓",
    "审计权重",
    "持仓/成本",
    "账户权重",
    "个人成本",
    "资金规模",
)


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    strategy = load_json(STRATEGY_PATH)
    quotes = load_json(QUOTES_PATH)
    dividends = load_json(DIVIDENDS_PATH) if DIVIDENDS_PATH.exists() else {"dividends": {}}
    stocks = strategy.get("stocks", [])
    quote_map = quotes.get("quotes", {})
    dividend_map = dividends.get("dividends", {})
    if not stocks:
        fail("strategy.json 没有股票数据")

    codes: set[str] = set()
    required = {
        "order", "name", "code", "market", "provider", "providerSymbol",
        "currency", "dividend", "firstYield", "addYield", "heavyYield",
        "thesis", "history", "priceMap",
    }
    for stock in stocks:
        missing = required - set(stock)
        if missing:
            fail(f"{stock.get('code', '?')} 缺少字段: {sorted(missing)}")
        code = stock["code"]
        if code in codes:
            fail(f"股票代码重复: {code}")
        codes.add(code)
        if stock["market"] not in {"A股", "港股", "美股"}:
            fail(f"{code} 市场无效: {stock['market']}")
        for field in ("dividend", "firstYield", "addYield", "heavyYield"):
            value = stock[field]
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                fail(f"{code} 的 {field} 无效")
        quote = quote_map.get(code)
        if not quote:
            fail(f"{code} 缺少行情")
        price = quote.get("price")
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            fail(f"{code} 行情价格无效")
        if not quote.get("tradeDate"):
            fail(f"{code} 缺少交易日期")
        dividend_record = dividend_map.get(code)
        if dividend_record:
            history = dividend_record.get("history") or []
            for point in history:
                if len(point) != 2 or not isinstance(point[1], (int, float)) or point[1] <= 0:
                    fail(f"{code} 的自动DPS历史无效: {point}")

    extras = set(quote_map) - codes
    if extras:
        fail(f"行情文件包含未配置代码: {sorted(extras)}")

    public_text = "\n".join(
        path.read_text(encoding="utf-8") for path in PUBLIC_TEXT_PATHS if path.exists()
    )
    for marker in PRIVATE_MARKERS:
        if marker in public_text:
            fail(f"公开文件发现疑似私人信息: {marker}")

    print(f"OK: {len(stocks)} 只股票，策略与行情数据通过校验")


if __name__ == "__main__":
    main()
