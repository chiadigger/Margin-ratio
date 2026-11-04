#!/usr/bin/env python3
"""
全市場(上市+上櫃)融資維持率 估算

公式(市場整體估算, 非官方整戶維持率):
    維持率 = Σ(各股融資餘額張數 x 1000 x 收盤價) / 融資餘額金額
    融資餘額金額 取自交易所/櫃買公布的信用交易統計(仟元)

用法:
    python margin_ratio.py                 # 自動找最近一個有資料的交易日
    python margin_ratio.py --date 2026-11-03
    python margin_ratio.py --debug         # 印出解析到的欄位, 方便除錯
    python margin_ratio.py --tpex-amount-k 12345678   # 櫃買融資金額抓不到時手動給(仟元)
"""
import argparse
import csv
import datetime as dt
import os
import time

import requests

UA = {"User-Agent": "Mozilla/5.0"}
TWSE_MARGIN = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
TWSE_QUOTE = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TPEX_MARGIN = "https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php"
TPEX_QUOTE = "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php"

DEBUG = False


def get(url, params, retries=5):
    """連線被中斷/逾時就重試(指數退避), 全部失敗才丟出例外"""
    headers = {
        **UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9",
        "Referer": url.split("/web/")[0].split("/rwd/")[0] + "/",
        "Connection": "close",
    }
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            r.raise_for_status()
            time.sleep(3)  # 對交易所客氣一點, 避免被擋
            return r.json()
        except (requests.exceptions.RequestException, ValueError) as e:
            last = e
            wait = 5 * (2 ** i)
            print(f"[retry {i + 1}/{retries}] {url.split('/')[-1]}: {type(e).__name__} -> {wait}s 後重試")
            time.sleep(wait)
    raise last


def safe(fn, *args, default=None):
    """上櫃資料抓不到時不要讓整支程式掛掉, 改成只算上市"""
    try:
        return fn(*args)
    except Exception as e:
        print(f"[warn] {fn.__name__} 失敗: {type(e).__name__}: {e}")
        return default


def num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return 0.0


def roc(d):
    return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


def tables_of(j):
    """統一 TWSE / TPEx 兩種回傳格式 -> [(fields, rows, summary_rows)]"""
    out = []
    for t in j.get("tables") or []:
        out.append((t.get("fields") or [], t.get("data") or [], t.get("summary") or []))
    if not out and j.get("aaData"):
        out.append(([], j["aaData"], []))
    return out


def find_amount_k(obj):
    """遞迴找 '融資金額' 那一列, 取『今日餘額』(通常是該列最後一個數字)"""
    if isinstance(obj, list):
        if obj and isinstance(obj[0], str) and obj[0].startswith("融資金額"):
            nums = [num(x) for x in obj[1:] if str(x).replace(",", "").strip().replace(".", "").isdigit()]
            if nums:
                return nums[-1]
        for x in obj:
            v = find_amount_k(x)
            if v:
                return v
    elif isinstance(obj, dict):
        for x in obj.values():
            v = find_amount_k(x)
            if v:
                return v
    return None


# ---------------- 上市 ----------------
def twse_margin(d):
    j = get(TWSE_MARGIN, {"date": d.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"})
    if j.get("stat") != "OK":
        return None, None
    shares = {}
    for f, rows, _ in tables_of(j):
        if "代號" in f and "今日餘額" in f:
            i = f.index("今日餘額")  # 第一個 = 融資
            for r in rows:
                shares[str(r[0]).strip()] = num(r[i])
    return shares, find_amount_k(j)


def twse_close(d):
    j = get(TWSE_QUOTE, {"date": d.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json"})
    px = {}
    for f, rows, _ in tables_of(j):
        if "證券代號" in f and "收盤價" in f:
            ic, ip = f.index("證券代號"), f.index("收盤價")
            for r in rows:
                p = num(r[ip])
                if p > 0:
                    px[str(r[ic]).strip()] = p
    return px


# ---------------- 上櫃 ----------------
def tpex_margin(d):
    j = get(TPEX_MARGIN, {"l": "zh-tw", "d": roc(d), "o": "json"})
    shares = {}
    for f, rows, _ in tables_of(j):
        idx = 6  # 預設: 代號,名稱,前資餘額,資買,資賣,現償,資餘額
        for k, name in enumerate(f):
            if "資餘額" in name and "前" not in name:
                idx = k
                break
        if DEBUG:
            print("[tpex margin] fields:", f, "-> idx", idx)
        for r in rows:
            if len(r) > idx:
                shares[str(r[0]).strip()] = num(r[idx])
    return shares, find_amount_k(j)


def tpex_close(d):
    j = get(TPEX_QUOTE, {"l": "zh-tw", "d": roc(d), "o": "json"})
    px = {}
    for f, rows, _ in tables_of(j):
        ip = 2  # 預設: 代號,名稱,收盤
        for k, name in enumerate(f):
            if "收盤" in name:
                ip = k
                break
        for r in rows:
            p = num(r[ip]) if len(r) > ip else 0
            if p > 0:
                px[str(r[0]).strip()] = p
    return px


# ---------------- 計算 ----------------
EXCLUDE_ETF = True
ETF_LOAN_RATIO = 0.6  # 估算 ETF 融資金額 = ETF 融資餘額市值 x 此比例(融資成數假設)


def is_etf(code):
    """代號 00 開頭 = ETF(0050, 00878, 00981A, 00679B...)"""
    return code.startswith("00")


def ratio(shares, px, amount_k):
    """回傳 (維持率%, 擔保品市值, 缺價檔數, 扣除ETF後融資金額(元))"""
    if not shares or not amount_k:
        return None, 0.0, 0, 0.0
    collateral, missing, etf_mv = 0.0, 0, 0.0
    for code, z in shares.items():
        if z <= 0:
            continue
        if code not in px:
            missing += 1
            continue
        mv = z * 1000 * px[code]
        if EXCLUDE_ETF and is_etf(code):
            etf_mv += mv
            continue
        collateral += mv
    # 融資金額只有市場總額, 沒有逐檔金額, 所以 ETF 部分用市值 x 成數估算後扣除
    amount = amount_k * 1000 - etf_mv * ETF_LOAN_RATIO
    if amount <= 0:
        return None, collateral, missing, amount
    return collateral / amount * 100, collateral, missing, amount


def run(d, tpex_amount_override=None):
    sh_l, amt_l = twse_margin(d)
    if sh_l is None:
        return None  # 非交易日或資料未出
    px_l = twse_close(d)
    sh_o, amt_o = safe(tpex_margin, d, default=({}, None))
    px_o = safe(tpex_close, d, default={})
    if tpex_amount_override:
        amt_o = tpex_amount_override

    r_l, col_l, miss_l, adj_l = ratio(sh_l, px_l, amt_l)
    if px_o and sh_o:
        r_o, col_o, miss_o, adj_o = ratio(sh_o, px_o, amt_o)
    else:  # 上櫃資料沒抓到, 不要算出誤導性的 0%
        r_o, col_o, miss_o, adj_o, amt_o = None, 0.0, 0, 0.0, None
    r_all = None
    if amt_l and amt_o and adj_l > 0 and adj_o > 0:
        r_all = (col_l + col_o) / (adj_l + adj_o) * 100
    return {
        "date": d.isoformat(),
        "twse_ratio": r_l, "tpex_ratio": r_o, "all_ratio": r_all,
        "twse_amount_k": amt_l, "tpex_amount_k": amt_o,
        "missing_price": miss_l + miss_o,
    }


def fmt(x):
    return "N/A" if x is None else f"{x:.2f}%"


def _p(v):
    try:
        return f"{float(v):.2f}%"
    except (TypeError, ValueError):
        return "N/A"


def write_readme(rows, path="README.md", keep=30):
    """把最新結果寫進 README.md, repo 的 Code 首頁就會直接顯示"""
    rows = sorted(rows, key=lambda r: r["date"], reverse=True)
    last = rows[0]
    lines = [
        "# 台股全市場融資維持率",
        "",
        f"**最新資料日期：{last['date']}**",
        "",
        "| 市場 | 融資維持率 |",
        "|---|---|",
        f"| 上市 | {_p(last['twse_ratio'])} |",
        f"| 上櫃 | {_p(last['tpex_ratio'])} |",
        f"| **全市場** | **{_p(last['all_ratio'])}** |",
        "",
        f"## 近 {keep} 個交易日",
        "",
        "| 日期 | 上市 | 上櫃 | 全市場 |",
        "|---|---|---|---|",
    ]
    for r in rows[:keep]:
        lines.append(
            f"| {r['date']} | {_p(r['twse_ratio'])} | {_p(r['tpex_ratio'])} | {_p(r['all_ratio'])} |"
        )
    lines += [
        "",
        "> 算法：Σ(各股融資餘額張數 x 1000 x 收盤價) ÷ 融資餘額金額，為市場整體估算值，非官方整戶維持率。",
        "> 已排除 ETF（代號 00 開頭）：分子扣除 ETF 擔保品，分母以「ETF 融資市值 x 6 成」估算後扣除。",
        "> 資料來源：證交所、櫃買中心。每個交易日晚上自動更新。",
        "",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    global DEBUG, EXCLUDE_ETF, ETF_LOAN_RATIO
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-etf", action="store_true", help="不排除 ETF")
    ap.add_argument("--etf-ratio", type=float, default=ETF_LOAN_RATIO, help="ETF 融資成數假設, 預設 0.6")
    ap.add_argument("--date")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--tpex-amount-k", type=float)
    ap.add_argument("--csv", default="data/margin_ratio.csv")
    a = ap.parse_args()
    DEBUG = a.debug
    EXCLUDE_ETF = not a.keep_etf
    ETF_LOAN_RATIO = a.etf_ratio

    if a.date:
        days = [dt.date.fromisoformat(a.date)]
    else:
        today = dt.datetime.utcnow() + dt.timedelta(hours=8)
        days = [(today - dt.timedelta(days=i)).date() for i in range(8)]

    res = None
    for d in days:
        if d.weekday() >= 5:
            continue
        res = run(d, a.tpex_amount_k)
        if res:
            break
    if not res:
        print("找不到資料(非交易日或交易所尚未公布)")
        return

    print(f"日期: {res['date']}")
    print(f"上市 融資維持率: {fmt(res['twse_ratio'])}")
    print(f"上櫃 融資維持率: {fmt(res['tpex_ratio'])}")
    print(f"全市場 融資維持率: {fmt(res['all_ratio'])}")
    print(f"(缺收盤價檔數: {res['missing_price']})")

    os.makedirs(os.path.dirname(a.csv) or ".", exist_ok=True)
    rows, seen = [], set()
    if os.path.exists(a.csv):
        with open(a.csv, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            seen = {r["date"] for r in rows}
    if res["date"] not in seen:
        rows.append({k: ("" if v is None else v) for k, v in res.items()})
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(res.keys()))
            w.writeheader()
            w.writerows(rows)
    write_readme(rows)


if __name__ == "__main__":
    main()
