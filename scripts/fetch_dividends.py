from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
STRATEGY_PATH = ROOT / "config" / "strategy.json"
DIVIDENDS_PATH = ROOT / "data" / "dividends.json"
BEIJING = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")
CURRENT_YEAR = datetime.now(BEIJING).year


def load_json(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return default or {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def request_json(url: str, params: dict[str, str], attempts: int = 3) -> dict:
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        full_url,
        headers={"User-Agent": "Mozilla/5.0 dividend-insight/1.0", "Referer": "https://www.eastmoney.com/"},
    )
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"分红接口连续{attempts}次连接失败：{last_error}") from last_error


def compact_history(values: dict[int, float], *, include_current_ytd: bool) -> list[list[object]]:
    rows = []
    for year, amount in sorted(values.items()):
        if amount <= 0:
            continue
        label: object = f"{str(year)[-2:]}YTD" if include_current_ytd and year == CURRENT_YEAR else year
        rows.append([label, round(amount, 4)])
    return rows[-6:]


def fetch_a_share(stock: dict) -> tuple[list[list[object]], list[str]]:
    params = {
        "reportName": "RPT_SHAREBONUS_DET",
        "columns": "ALL",
        "filter": f'(SECURITY_CODE="{stock["code"]}")',
        "pageNumber": "1",
        "pageSize": "100",
        "sortTypes": "-1",
        "sortColumns": "EX_DIVIDEND_DATE",
        "source": "WEB",
        "client": "WEB",
    }
    payload = request_json("https://datacenter-web.eastmoney.com/api/data/v1/get", params)
    records = ((payload.get("result") or {}).get("data") or [])
    annual: dict[int, float] = defaultdict(float)
    for record in records:
        if "实施" not in str(record.get("ASSIGN_PROGRESS") or ""):
            continue
        report_date = str(record.get("REPORT_DATE") or "")
        amount_per_ten = record.get("PRETAX_BONUS_RMB")
        if not report_date[:4].isdigit() or amount_per_ten in (None, ""):
            continue
        annual[int(report_date[:4])] += float(amount_per_ten) / 10
    history = compact_history(annual, include_current_ytd=False)
    if not history:
        raise ValueError(f"东方财富未返回 {stock['code']} 的已实施现金分红")
    return history, []


def hk_cash_hkd(plan: str) -> float | None:
    converted = re.findall(r"相当于港(?:币|元)\s*([0-9.]+)\s*元?", plan)
    if converted:
        return float(converted[-1])
    direct = re.findall(r"每股派(?:港币|港元)\s*([0-9.]+)\s*元?", plan)
    if direct:
        return float(direct[-1])
    return None


def fetch_h_share(stock: dict) -> tuple[list[list[object]], list[str]]:
    params = {
        "reportName": "RPT_HKF10_MAIN_DIVBASIC",
        "columns": "ALL",
        "filter": f'(SECURITY_CODE="{stock["code"]}")(IS_BFP="0")',
        "pageNumber": "1",
        "pageSize": "200",
        "sortTypes": "-1,-1",
        "sortColumns": "NOTICE_DATE,EX_DIVIDEND_DATE",
        "source": "F10",
        "client": "PC",
    }
    payload = request_json("https://datacenter.eastmoney.com/securities/api/data/v1/get", params)
    records = ((payload.get("result") or {}).get("data") or [])
    annual: dict[int, float] = defaultdict(float)
    warnings: list[str] = []
    for record in records:
        report_type = str(record.get("REPORT_TYPE") or "")
        if "特别" in report_type:
            continue
        year_text = str(record.get("YEAR") or "")
        plan = str(record.get("PLAN_EXPLAIN") or "")
        if not year_text[:4].isdigit():
            continue
        amount = hk_cash_hkd(plan)
        if amount is None:
            if plan and "派" in plan:
                warnings.append(f"{year_text}存在非港币或无法解析的派息：{plan}")
            continue
        annual[int(year_text[:4])] += amount
    history = compact_history(annual, include_current_ytd=True)
    if not history:
        raise ValueError(f"东方财富未返回 {stock['code']} 的港币现金分红")
    return history, warnings


def fetch_us_share(stock: dict) -> tuple[list[list[object]], list[str]]:
    symbol = stock["providerSymbol"]
    params = {"range": "10y", "interval": "1d", "events": "div,splits"}
    payload = request_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}", params)
    result = payload["chart"]["result"][0]
    events = (result.get("events") or {}).get("dividends") or {}
    annual: dict[int, float] = defaultdict(float)
    for event in events.values():
        timestamp = event.get("date")
        amount = event.get("amount")
        if timestamp is None or amount is None:
            continue
        year = datetime.fromtimestamp(int(timestamp), NEW_YORK).year
        annual[year] += float(amount)
    history = compact_history(annual, include_current_ytd=True)
    if not history:
        raise ValueError(f"Yahoo未返回 {symbol} 的现金分红事件")
    return history, []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="更新股票池历年实际每股股息，不修改正常化DPS。")
    parser.add_argument("--markets", default="A股,港股,美股")
    parser.add_argument("--codes", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    selected_markets = {item.strip() for item in args.markets.split(",") if item.strip()}
    selected_codes = {item.strip().upper() for item in args.codes.split(",") if item.strip()}
    strategy = load_json(STRATEGY_PATH)
    document = load_json(DIVIDENDS_PATH, {"schemaVersion": 1, "dividends": {}})
    records = document.setdefault("dividends", {})
    targets = [
        stock for stock in strategy["stocks"]
        if stock["market"] in selected_markets and (not selected_codes or stock["code"].upper() in selected_codes)
    ]
    now = datetime.now(BEIJING).isoformat(timespec="seconds")
    errors: list[str] = []
    updated = 0
    for stock in targets:
        try:
            if stock["market"] == "A股":
                history, warnings = fetch_a_share(stock)
                source = "eastmoney-a-dividend"
            elif stock["market"] == "港股":
                history, warnings = fetch_h_share(stock)
                source = "eastmoney-hk-dividend"
            else:
                history, warnings = fetch_us_share(stock)
                source = "yahoo-dividend-events"
            records[stock["code"]] = {
                "history": history,
                "source": source,
                "status": "ok" if not warnings else "partial",
                "updatedAt": now,
                "warnings": warnings,
            }
            updated += 1
        except Exception as exc:
            errors.append(f"{stock['code']}: {exc}")
            if stock["code"] in records:
                records[stock["code"]]["lastAttemptAt"] = now
                records[stock["code"]]["lastAttemptStatus"] = "fetch-failed"
    document["generatedAt"] = now
    document["status"] = "partial" if errors else "ok"
    document["errors"] = errors
    DIVIDENDS_PATH.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"DPS更新完成：目标 {len(targets)}，成功 {updated}，失败 {len(errors)}")
    for error in errors:
        print(f"WARN: {error}", file=sys.stderr)
    missing = [stock["code"] for stock in targets if stock["code"] not in records]
    if selected_codes and missing:
        raise SystemExit(f"新增股票DPS获取失败：{', '.join(missing)}；已保留正常化DPS作为回退。")


if __name__ == "__main__":
    main()
