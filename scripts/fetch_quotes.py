from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
STRATEGY_PATH = ROOT / "config" / "strategy.json"
QUOTES_PATH = ROOT / "data" / "quotes.json"
BEIJING = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")
MAX_MOVE = 0.20


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def request_bytes(url: str, *, referer: str | None = None, attempts: int = 3) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0 dividend-insight/1.0"}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.read()
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"行情接口连续{attempts}次连接失败：{last_error}") from last_error


def fetch_tencent(stocks: list[dict]) -> dict[str, tuple[float, str]]:
    if not stocks:
        return {}
    symbols = ",".join(stock["providerSymbol"] for stock in stocks)
    url = "https://qt.gtimg.cn/q=" + urllib.parse.quote(symbols, safe=",")
    text = request_bytes(url, referer="https://gu.qq.com/").decode("gb18030", errors="replace")
    by_symbol = {}
    for symbol, payload in re.findall(r'v_([A-Za-z0-9]+)="([^"]*)";', text):
        fields = payload.split("~")
        if len(fields) < 4:
            continue
        price = float(fields[3])
        date_token = next(
            (
                item
                for item in fields
                if re.fullmatch(r"20\d{12}", item)
                or re.fullmatch(r"20\d{2}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}", item)
            ),
            None,
        )
        if not date_token:
            raise ValueError(f"腾讯行情 {symbol} 缺少交易时间")
        if "/" in date_token:
            trade_date = date_token[:10].replace("/", "-")
        else:
            trade_date = f"{date_token[:4]}-{date_token[4:6]}-{date_token[6:8]}"
        by_symbol[symbol.lower()] = (price, trade_date)
    result = {}
    for stock in stocks:
        symbol = stock["providerSymbol"].lower()
        if symbol not in by_symbol:
            raise ValueError(f"腾讯行情未返回 {symbol}")
        result[stock["code"]] = by_symbol[symbol]
    return result


def fetch_yahoo(stock: dict) -> tuple[float, str]:
    symbol = urllib.parse.quote(stock["providerSymbol"], safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=10d&interval=1d&events=div%2Csplits"
    payload = json.loads(request_bytes(url).decode("utf-8"))
    result = payload["chart"]["result"][0]
    timestamps = result.get("timestamp", [])
    closes = result["indicators"]["quote"][0].get("close", [])
    valid = [(ts, close) for ts, close in zip(timestamps, closes) if close is not None]
    if not valid:
        raise ValueError(f"Yahoo 行情未返回 {stock['providerSymbol']} 的有效收盘价")
    timestamp, close = valid[-1]
    trade_date = datetime.fromtimestamp(timestamp, NEW_YORK).date().isoformat()
    return round(float(close), 4), trade_date


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="更新公开版最新收盘价；策略参数不会被修改。")
    parser.add_argument("--markets", default="A股,港股,美股", help="逗号分隔，例如 A股,港股")
    parser.add_argument("--codes", default="", help="只更新指定代码，使用逗号分隔；留空则更新所选市场全部股票")
    parser.add_argument("--dry-run", action="store_true", help="只抓取和校验，不写入 quotes.json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    selected = {item.strip() for item in args.markets.split(",") if item.strip()}
    unknown = selected - {"A股", "港股", "美股"}
    if unknown:
        raise SystemExit(f"未知市场: {sorted(unknown)}")
    selected_codes = {item.strip().upper() for item in args.codes.split(",") if item.strip()}

    strategy = load_json(STRATEGY_PATH)
    quote_data = load_json(QUOTES_PATH)
    quote_map = quote_data["quotes"]
    targets = [
        stock
        for stock in strategy["stocks"]
        if stock["market"] in selected and (not selected_codes or stock["code"].upper() in selected_codes)
    ]
    if selected_codes:
        found_codes = {stock["code"].upper() for stock in targets}
        missing_codes = selected_codes - found_codes
        if missing_codes:
            raise SystemExit(f"配置中找不到代码: {sorted(missing_codes)}")
    fetched: dict[str, tuple[float, str, str]] = {}
    errors: list[str] = []

    tencent_targets = [stock for stock in targets if stock["provider"] == "tencent"]
    if tencent_targets:
        try:
            for code, (price, trade_date) in fetch_tencent(tencent_targets).items():
                fetched[code] = (price, trade_date, "tencent")
        except Exception as exc:
            errors.append(str(exc))

    for stock in (item for item in targets if item["provider"] == "yahoo"):
        try:
            price, trade_date = fetch_yahoo(stock)
            fetched[stock["code"]] = (price, trade_date, "yahoo")
        except Exception as exc:
            errors.append(str(exc))

    now = datetime.now(BEIJING).isoformat(timespec="seconds")
    updated = 0
    suspicious = []
    for stock in targets:
        code = stock["code"]
        if code not in fetched:
            if code in quote_map:
                quote_map[code]["lastAttemptAt"] = now
                quote_map[code]["lastAttemptStatus"] = "fetch-failed"
            else:
                errors.append(f"{code} 首次行情获取失败")
            continue
        new_price, trade_date, source = fetched[code]
        old_price = float(quote_map.get(code, {}).get("price", 0))
        move = abs(new_price / old_price - 1) if old_price > 0 else 0
        if new_price <= 0 or move > MAX_MOVE:
            quote_map[code]["lastAttemptAt"] = now
            quote_map[code]["lastAttemptStatus"] = "suspicious"
            quote_map[code]["candidatePrice"] = new_price
            suspicious.append(f"{code}: {old_price} -> {new_price} ({move:.1%})")
            continue
        quote_map[code] = {
            "price": new_price,
            "tradeDate": trade_date,
            "source": source,
            "status": "ok",
            "updatedAt": now,
        }
        updated += 1

    quote_data["generatedAt"] = now
    quote_data["status"] = "partial" if errors or suspicious or updated < len(targets) else "ok"
    if errors:
        quote_data["errors"] = errors
    else:
        quote_data.pop("errors", None)
    if suspicious:
        quote_data["warnings"] = suspicious
    else:
        quote_data.pop("warnings", None)

    if not args.dry_run:
        QUOTES_PATH.write_text(json.dumps(quote_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"完成：目标 {len(targets)}，更新 {updated}，失败 {len(errors)}，异常 {len(suspicious)}")
    for message in errors:
        print(f"WARN: {message}", file=sys.stderr)
    for message in suspicious:
        print(f"WARN: 价格波动超过20%，已保留旧值：{message}", file=sys.stderr)
    missing_quotes = [stock["code"] for stock in targets if stock["code"] not in quote_map]
    if selected_codes and missing_quotes:
        raise SystemExit(f"新增股票行情获取失败：{', '.join(missing_quotes)}。请稍后重试；原股票池不会被修改。")


if __name__ == "__main__":
    main()
