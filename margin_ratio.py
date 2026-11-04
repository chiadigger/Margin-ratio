#!/usr/bin/env python3
"""
全市場(上市+上櫃)融資維持率 - 同時輸出兩種算法

舊算法 (7/30 改版前, 媒體/財經平台常見口徑, 含 ETF):
    維持率 = Σ(全部股票 融資餘額張數 x 1000 x 收盤價) ÷ 融資餘額金額
新算法 (排除 ETF, 代號 00 開頭):
    分子 = 只算非 ETF 的擔保品市值
    分母 = 融資餘額金額 - 估算的 ETF 融資金額 (ETF 融資市值 x 融資成數假設)

融資餘額金額(仟元)取自交易所/櫃買的信用交易統計。
注意: 這是市場整體估算值, 不是證交所公布的「整戶擔保維持率」(兩者口徑不同)。

用法:
    python margin_ratio.py                       # 最近一個有資料的交易日
    python margin_ratio.py --date 2026-07-29     # 指定日期(可拿來對照舊數字)
    python margin_ratio.py --etf-ratio 0.5       # 調整 ETF 融資成數假設(預設 0.6)
    python margin_ratio.py --tpex-amount-k 123   # 櫃買融資金額抓不到時手動給(仟元)
    python margin_ratio.py --debug
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
FIELDS = ["date",
          "old_twse", "old_tpex", "old_all",
          "new_twse", "new_tpex", "new_all",
          "twse_amount_k", "tpex_amount_k", "missing_price"]


# ---------------- 基礎工具 ----------------
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
            time.sleep(3)  # 對交易所客氣一點
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


def _isnum(x):
    return str(x).replace(",", "").strip().lstrip("-").replace(".", "", 1).isdigit()


def describe(tag, j):
    """印出回傳結構, 解析不到資料時用來診斷"""
    try:
        print(f"[診斷] {tag}: keys={list(j.keys())}")
        for i, (f, rows, summ) in enumerate(tables_of(j)):
            print(f"[診斷] {tag} table{i}: fields={f[:14]} 筆數={len(rows)} "
                  f"第1筆={rows[0][:10] if rows else None} summary={summ[:3]}")
        if not tables_of(j):
            print(f"[診斷] {tag}: 內容前300字={str(j)[:300]}")
    except Exception as e:
        print(f"[診斷] {tag} 失敗: {e}")


def find_amount_k(obj):
    """遞迴找含 '融資金額' 的那一列, 取該字串之後的最後一個數字(通常是今日餘額)"""
    if isinstance(obj, list):
        idx = [i for i, x in enumerate(obj) if isinstance(x, str) and "融資金額" in x]
        if idx:
            nums = [num(x) for x in obj[idx[0] + 1:] if _isnum(x)]
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
    amount = find_amount_k(j)
    print(f"[tpex] 融資餘額 檔數={len(shares)} 融資金額(仟元)={amount}")
    if not shares or amount is None:
        describe("tpex_margin", j)
    return shares, amount


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
    print(f"[tpex] 收盤價 檔數={len(px)}")
    if not px:
        describe("tpex_close", j)
    return px


# ---------------- 計算 ----------------
def is_etf(code):
    """代號 00 開頭 = ETF(0050, 00878, 00981A, 00679B...)"""
    return code.startswith("00")


def calc(shares, px, amount_k, etf_loan_ratio):
    """回傳各項加總(元). 一次算出『含ETF』與『排除ETF』兩組分子分母"""
    if not shares or not px or not amount_k:
        return None
    col_all = col_ex = etf_mv = 0.0
    missing = 0
    for code, z in shares.items():
        if z <= 0:
            continue
        if code not in px:
            missing += 1
            continue
        mv = z * 1000 * px[code]
        col_all += mv
        if is_etf(code):
            etf_mv += mv
        else:
            col_ex += mv
    amt_raw = amount_k * 1000
    # 融資金額只有市場總額, 沒有逐檔金額, ETF 部分用市值 x 成數估算後扣除
    amt_adj = amt_raw - etf_mv * etf_loan_ratio
    return {"col_all": col_all, "col_ex": col_ex, "etf_mv": etf_mv,
            "amt_raw": amt_raw, "amt_adj": amt_adj, "missing": missing}


def pct(a, b):
    return a / b * 100 if b and b > 0 else None


def run(d, tpex_amount_override=None, etf_ratio=0.6):
    sh_l, amt_l = twse_margin(d)
    if sh_l is None:
        return None  # 非交易日或資料未出
    px_l = twse_close(d)
    sh_o, amt_o = safe(tpex_margin, d, default=({}, None))
    px_o = safe(tpex_close, d, default={})
    if tpex_amount_override:
        amt_o = tpex_amount_override

    L = calc(sh_l, px_l, amt_l, etf_ratio)
    O = calc(sh_o, px_o, amt_o, etf_ratio)

    def yi(x):
        return f"{x / 1e8:,.1f}億"
    for name, X in (("上市", L), ("上櫃", O)):
        if X:
            print(f"[檢查] {name} 擔保品市值(含ETF)={yi(X['col_all'])} 其中ETF={yi(X['etf_mv'])} "
                  f"融資金額={yi(X['amt_raw'])} 扣ETF後={yi(X['amt_adj'])} 缺價={X['missing']}檔")
        else:
            print(f"[檢查] {name} 資料不足, 略過")

    row = {"date": d.isoformat(),
           "old_twse": pct(L["col_all"], L["amt_raw"]) if L else None,
           "old_tpex": pct(O["col_all"], O["amt_raw"]) if O else None,
           "new_twse": pct(L["col_ex"], L["amt_adj"]) if L else None,
           "new_tpex": pct(O["col_ex"], O["amt_adj"]) if O else None,
           "old_all": None, "new_all": None,
           "twse_amount_k": amt_l, "tpex_amount_k": amt_o if O else None,
           "missing_price": (L["missing"] if L else 0) + (O["missing"] if O else 0)}
    if L and O:
        row["old_all"] = pct(L["col_all"] + O["col_all"], L["amt_raw"] + O["amt_raw"])
        row["new_all"] = pct(L["col_ex"] + O["col_ex"], L["amt_adj"] + O["amt_adj"])
    return row


# ---------------- 輸出 ----------------
def fmt(x):
    try:
        return f"{float(x):.2f}%"
    except (TypeError, ValueError):
        return "N/A"


def diff(a, b):
    try:
        return f"{float(b) - float(a):+.2f}"
    except (TypeError, ValueError):
        return "N/A"


def write_readme(rows, path="README.md", keep=30):
    """把最新結果寫進 README.md, repo 的 Code 首頁就會直接顯示"""
    rows = sorted(rows, key=lambda r: r["date"], reverse=True)
    r = rows[0]
    lines = [
        "# 台股全市場融資維持率",
        "",
        f"**最新資料日期：{r['date']}**",
        "",
        "| 市場 | 舊算法（含 ETF，7/30 改版前） | 新算法（排除 ETF） | 差 |",
        "|---|---|---|---|",
        f"| 上市 | {fmt(r['old_twse'])} | {fmt(r['new_twse'])} | {diff(r['old_twse'], r['new_twse'])} |",
        f"| 上櫃 | {fmt(r['old_tpex'])} | {fmt(r['new_tpex'])} | {diff(r['old_tpex'], r['new_tpex'])} |",
        f"| **全市場** | **{fmt(r['old_all'])}** | **{fmt(r['new_all'])}** | {diff(r['old_all'], r['new_all'])} |",
        "",
        f"## 近 {keep} 個交易日（全市場）",
        "",
        "| 日期 | 舊算法 | 新算法 | 差 |",
        "|---|---|---|---|",
    ]
    for x in rows[:keep]:
        lines.append(f"| {x['date']} | {fmt(x['old_all'])} | {fmt(x['new_all'])} | {diff(x['old_all'], x['new_all'])} |")
    lines += [
        "",
        "> **舊算法**：Σ(各股融資餘額張數 x 1000 x 收盤價) ÷ 融資餘額金額，含 ETF。",
        "> **新算法**：分子排除 ETF（代號 00 開頭）；分母以「ETF 融資市值 x 融資成數假設」估算後扣除。",
        "> 皆為市場整體估算值，非證交所公布的整戶擔保維持率。資料來源：證交所、櫃買中心，每個交易日晚上自動更新。",
        "",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    global DEBUG
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--tpex-amount-k", type=float)
    ap.add_argument("--etf-ratio", type=float, default=0.6, help="ETF 融資成數假設, 預設 0.6")
    ap.add_argument("--csv", default="data/margin_ratio_v2.csv")
    a = ap.parse_args()
    DEBUG = a.debug

    if a.date:
        days = [dt.date.fromisoformat(a.date)]
    else:
        today = dt.datetime.utcnow() + dt.timedelta(hours=8)
        days = [(today - dt.timedelta(days=i)).date() for i in range(8)]

    res = None
    for d in days:
        if d.weekday() >= 5:
            continue
        res = run(d, a.tpex_amount_k, a.etf_ratio)
        if res:
            break
    if not res:
        print("找不到資料(非交易日或交易所尚未公布)")
        return

    print(f"\n日期: {res['date']}")
    print(f"{'':8}{'舊算法(含ETF)':>14}{'新算法(排除ETF)':>16}")
    print(f"{'上市':<8}{fmt(res['old_twse']):>14}{fmt(res['new_twse']):>16}")
    print(f"{'上櫃':<8}{fmt(res['old_tpex']):>14}{fmt(res['new_tpex']):>16}")
    print(f"{'全市場':<7}{fmt(res['old_all']):>14}{fmt(res['new_all']):>16}")
    print(f"(缺收盤價檔數: {res['missing_price']})")

    os.makedirs(os.path.dirname(a.csv) or ".", exist_ok=True)
    rows = []
    if os.path.exists(a.csv):
        with open(a.csv, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r["date"] != res["date"]]
    rows.append({k: ("" if res[k] is None else res[k]) for k in FIELDS})
    rows.sort(key=lambda r: r["date"])
    with open(a.csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    write_readme(rows)


if __name__ == "__main__":
    main()
