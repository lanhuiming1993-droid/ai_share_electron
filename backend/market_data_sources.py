from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any


def stock_code(query: str) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", query)
    return match.group(1) if match else ""


def exchange_code(code: str, *, separator: str = "") -> str:
    if code.startswith(("4", "8")):
        exchange = "bj"
    elif code.startswith(("5", "6", "9")):
        exchange = "sh"
    else:
        exchange = "sz"
    return f"{exchange}{separator}{code}"


def tushare_code(code: str) -> str:
    exchange = exchange_code(code)[:2].upper()
    return f"{code}.{exchange}"


def frame_records(frame: Any, limit: int = 600) -> list[dict[str, Any]]:
    if frame is None:
        return []
    return frame.head(limit).to_dict(orient="records")


def add_dataset(result: dict[str, Any], name: str, records: list[dict[str, Any]]) -> None:
    result["datasets"][name] = records
    if records:
        result["valid_datasets"].append(name)


def collect_akshare(window: dict[str, str], query: str) -> dict[str, Any]:
    import akshare as ak

    code = stock_code(query)
    start_date = window["window_start"][:10].replace("-", "")
    end_date = window["window_end"][:10].replace("-", "")
    result: dict[str, Any] = {"component": "akshare", "datasets": {}, "valid_datasets": [], "errors": {}}
    if code:
        try:
            add_dataset(result, "stock_individual_info_em", frame_records(ak.stock_individual_info_em(symbol=code)))
        except Exception as exc:
            result["errors"]["stock_individual_info_em"] = str(exc)
        for name, load in (
            (
                "stock_zh_a_hist",
                lambda: ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust="",
                ),
            ),
            (
                "stock_zh_a_hist_tx",
                lambda: ak.stock_zh_a_hist_tx(
                    symbol=exchange_code(code),
                    start_date=start_date,
                    end_date=end_date,
                    adjust="",
                ),
            ),
            (
                "stock_zh_a_daily",
                lambda: ak.stock_zh_a_daily(
                    symbol=exchange_code(code),
                    start_date=start_date,
                    end_date=end_date,
                    adjust="",
                ),
            ),
        ):
            if any(item in result["valid_datasets"] for item in ("stock_zh_a_hist", "stock_zh_a_hist_tx", "stock_zh_a_daily")):
                break
            try:
                add_dataset(result, name, frame_records(load()))
            except Exception as exc:
                result["errors"][name] = str(exc)
    else:
        for name, load in (
            ("stock_zh_a_spot_em", ak.stock_zh_a_spot_em),
            ("stock_zh_a_spot", ak.stock_zh_a_spot),
        ):
            try:
                add_dataset(result, name, frame_records(load()))
                if result["valid_datasets"]:
                    break
            except Exception as exc:
                result["errors"][name] = str(exc)
    return result


def baostock_records(result_set: Any, limit: int = 600) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    while result_set.error_code == "0" and result_set.next() and len(records) < limit:
        records.append(dict(zip(result_set.fields, result_set.get_row_data())))
    if result_set.error_code != "0":
        raise RuntimeError(result_set.error_msg)
    return records


def collect_baostock(window: dict[str, str], query: str) -> dict[str, Any]:
    import baostock as bs

    code = stock_code(query)
    result: dict[str, Any] = {"component": "baostock", "datasets": {}, "valid_datasets": [], "errors": {}}
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(login.error_msg)
    try:
        if code:
            symbol = exchange_code(code, separator=".")
            try:
                add_dataset(result, "query_stock_basic", baostock_records(bs.query_stock_basic(code=symbol)))
            except Exception as exc:
                result["errors"]["query_stock_basic"] = str(exc)
            try:
                add_dataset(
                    result,
                    "query_history_k_data_plus",
                    baostock_records(
                        bs.query_history_k_data_plus(
                            symbol,
                            "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST",
                            start_date=window["window_start"][:10],
                            end_date=window["window_end"][:10],
                            frequency="d",
                            adjustflag="3",
                        )
                    ),
                )
            except Exception as exc:
                result["errors"]["query_history_k_data_plus"] = str(exc)
        else:
            try:
                dates = baostock_records(
                    bs.query_trade_dates(
                        start_date=window["window_start"][:10],
                        end_date=window["window_end"][:10],
                    ),
                    limit=40,
                )
                trading_dates = [row["calendar_date"] for row in dates if row.get("is_trading_day") == "1"]
                if not trading_dates:
                    raise RuntimeError("BaoStock returned no trading day in the requested window")
                add_dataset(result, "query_all_stock", baostock_records(bs.query_all_stock(day=trading_dates[-1])))
            except Exception as exc:
                result["errors"]["query_all_stock"] = str(exc)
    finally:
        bs.logout()
    return result


def collect_tushare(window: dict[str, str], query: str) -> dict[str, Any]:
    import tushare as ts

    token = os.environ.get("ALPHADESK_TUSHARE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TuShare token is not configured")
    code = stock_code(query)
    start_date = window["window_start"][:10].replace("-", "")
    end_date = window["window_end"][:10].replace("-", "")
    pro = ts.pro_api(token)
    result: dict[str, Any] = {"component": "tushare", "datasets": {}, "valid_datasets": [], "errors": {}}
    if code:
        symbol = tushare_code(code)
        for name, load in (
            (
                "stock_basic",
                lambda: pro.stock_basic(
                    ts_code=symbol,
                    fields="ts_code,symbol,name,area,industry,market,list_date",
                ),
            ),
            ("daily", lambda: pro.daily(ts_code=symbol, start_date=start_date, end_date=end_date)),
            (
                "daily_basic",
                lambda: pro.daily_basic(
                    ts_code=symbol,
                    start_date=start_date,
                    end_date=end_date,
                    fields="ts_code,trade_date,close,turnover_rate,volume_ratio,pe,pb,total_share,float_share,total_mv,circ_mv",
                ),
            ),
        ):
            try:
                add_dataset(result, name, frame_records(load()))
            except Exception as exc:
                result["errors"][name] = str(exc)
    else:
        try:
            add_dataset(
                result,
                "stock_basic",
                frame_records(
                    pro.stock_basic(
                        exchange="",
                        list_status="L",
                        fields="ts_code,symbol,name,area,industry,market,list_date",
                    )
                ),
            )
        except Exception as exc:
            result["errors"]["stock_basic"] = str(exc)
    return result


def collect(source: str, window: dict[str, str], query: str) -> dict[str, Any]:
    collectors = {
        "akshare": collect_akshare,
        "baostock": collect_baostock,
        "tushare": collect_tushare,
    }
    return collectors[source](window, query)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", choices=("akshare", "baostock", "tushare"))
    parser.add_argument("--window-json", required=True)
    parser.add_argument("--query", default="")
    args = parser.parse_args()
    print(json.dumps(collect(args.source, json.loads(args.window_json), args.query), ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
