from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
STRATEGY_PATH = ROOT / "config" / "strategy.json"
QUOTES_PATH = ROOT / "data" / "quotes.json"
DIVIDENDS_PATH = ROOT / "data" / "dividends.json"
OUTPUT_PATH = ROOT / "dist" / "assets" / "stocks.js"


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def js_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def main() -> None:
    strategy = load_json(STRATEGY_PATH)
    quote_data = load_json(QUOTES_PATH)
    dividend_data = load_json(DIVIDENDS_PATH) if DIVIDENDS_PATH.exists() else {"dividends": {}}
    quotes = quote_data["quotes"]
    dividends = dividend_data.get("dividends", {})
    merged = []
    market_dates: dict[str, str] = {}
    stale_codes = []

    for stock in strategy["stocks"]:
        public_stock = {key: value for key, value in stock.items() if key not in {"provider", "providerSymbol"}}
        quote = quotes[stock["code"]]
        public_stock["price"] = quote["price"]
        public_stock["tradeDate"] = quote["tradeDate"]
        public_stock["quoteSource"] = quote.get("source", "unknown")
        public_stock["quoteStatus"] = quote.get("status", "unknown")
        dividend_record = dividends.get(stock["code"])
        if dividend_record and dividend_record.get("history"):
            public_stock["history"] = dividend_record["history"]
            public_stock["historySource"] = dividend_record.get("source", "unknown")
            public_stock["historyStatus"] = dividend_record.get("status", "unknown")
            public_stock["historyUpdatedAt"] = dividend_record.get("updatedAt")
        merged.append(public_stock)
        date = quote["tradeDate"]
        market = stock["market"]
        if market not in market_dates or date > market_dates[market]:
            market_dates[market] = date
        if quote.get("status") != "ok":
            stale_codes.append(stock["code"])

    meta = {
        "generatedAt": quote_data.get("generatedAt") or datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "status": "partial" if stale_codes or quote_data.get("status") == "partial" else "ok",
        "marketDates": market_dates,
        "staleCodes": stale_codes,
        "dividendGeneratedAt": dividend_data.get("generatedAt"),
        "dividendStatus": dividend_data.get("status", "pending"),
    }
    content = (
        "// 自动生成，请勿手工编辑。策略修改 config/strategy.json；行情修改 data/quotes.json。\n"
        f"window.QUOTE_META={js_json(meta)};\n"
        f"window.STOCKS_DATA={js_json(merged)};\n"
    )
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(content, encoding="utf-8")
    print(f"OK: 已生成 {OUTPUT_PATH.relative_to(ROOT)}，共 {len(merged)} 只股票")


if __name__ == "__main__":
    main()
