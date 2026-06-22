#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_twse.py — 抓取台股估值資料,輸出 data.json 供 index.html 使用。

資料來源:臺灣證券交易所 OpenAPI (https://openapi.twse.com.tw/)
  - BWIBBU_ALL   : 上市個股 本益比 / 殖利率 / 股價淨值比
  - STOCK_DAY_ALL: 上市個股 當日收盤價
  - t187ap03_L   : 上市公司基本資料(產業別、已發行股數)→ 用來算 P/S
  - t187ap05_L   : 上市公司每月營收 → 用來算 P/S

P/S(股價營收比)= 總市值 / 近12月營收
  - 總市值      = 收盤價 × 已發行股數
  - 近12月營收   = 由 rev_history.json 累積最近12個月(不足12月時以「當月營收×12」估算,並標記為近似)

用法:
  python fetch_twse.py                 # 輸出 data.json 到目前目錄
  python fetch_twse.py --out public/data.json
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import urllib.request
import urllib.error

BASE = "https://openapi.twse.com.tw/v1"
ENDPOINTS = {
    "valuation": f"{BASE}/exchangeReport/BWIBBU_ALL",
    "price":     f"{BASE}/exchangeReport/STOCK_DAY_ALL",
    "company":   f"{BASE}/opendata/t187ap03_L",
    "revenue":   f"{BASE}/opendata/t187ap05_L",
}

# 證交所「產業別」代碼 → 中文名稱(t187ap03_L 的產業別欄位有時為代碼)
INDUSTRY_MAP = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業",
    "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "14": "建材營造",
    "15": "航運業", "16": "觀光餐旅", "17": "金融保險業", "18": "貿易百貨",
    "19": "綜合企業", "20": "其他業", "21": "化學工業", "22": "生技醫療業",
    "23": "油電燃氣業", "24": "半導體業", "25": "電腦及週邊設備業",
    "26": "光電業", "27": "通信網路業", "28": "電子零組件業",
    "29": "電子通路業", "30": "資訊服務業", "31": "其他電子業",
    "32": "文化創意業", "33": "農業科技", "34": "電子商務", "35": "綠能環保",
    "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
}

TPE = timezone(timedelta(hours=8))  # Asia/Taipei


def fetch_json(url, retries=3, timeout=40):
    """抓取 JSON,失敗時重試。"""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "twse-valuation-bot/1.0", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
            last = e
            print(f"  ! {url} 第 {i+1} 次失敗: {e}", file=sys.stderr)
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"無法取得 {url}: {last}")


def num(x):
    """把證交所字串轉成 float,無效則回傳 None。"""
    if x is None:
        return None
    s = str(x).strip().replace(",", "")
    if s in ("", "-", "--", "N/A", "null", "不適用", "除息"):
        return None
    try:
        v = float(s)
        return v
    except ValueError:
        return None


def get(d, *keys):
    """從 dict 取第一個存在的 key(欄位名稱可能因版本不同)。"""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def normalize_industry(raw):
    if raw is None:
        return "其他業"
    s = str(raw).strip()
    if s in INDUSTRY_MAP:                       # 代碼
        return INDUSTRY_MAP[s]
    if s.zfill(2) in INDUSTRY_MAP:
        return INDUSTRY_MAP[s.zfill(2)]
    return s or "其他業"                         # 已是名稱


def load_rev_history(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"months": {}}   # { "YYYYMM": { code: 當月營收(千元) } }


def save_json(obj, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def build(out_path, hist_path):
    print("→ 抓取證交所 OpenAPI …")
    valuation = fetch_json(ENDPOINTS["valuation"])
    price     = fetch_json(ENDPOINTS["price"])
    company   = fetch_json(ENDPOINTS["company"])
    revenue   = fetch_json(ENDPOINTS["revenue"])
    print(f"  本益比/殖利率/淨值比 {len(valuation)} 筆,收盤價 {len(price)} 筆,"
          f"公司基本資料 {len(company)} 筆,月營收 {len(revenue)} 筆")

    # --- index by code ---
    val_by = {get(r, "Code", "證券代號", "公司代號"): r for r in valuation}
    px_by  = {get(r, "Code", "證券代號"): r for r in price}

    comp_by = {}
    for r in company:
        code = get(r, "公司代號", "Code")
        if code:
            comp_by[str(code).strip()] = r

    # --- revenue history: append this month's 當月營收 ---
    hist = load_rev_history(hist_path)
    this_month_rev = {}
    period = None
    for r in revenue:
        code = get(r, "公司代號", "Code")
        if not code:
            continue
        code = str(code).strip()
        rev = num(get(r, "營業收入-當月營收", "當月營收"))
        if rev is not None:
            this_month_rev[code] = rev          # 單位:千元
        if period is None:
            period = str(get(r, "資料年月", "出表日期") or "").strip()[:6]
    if period:
        hist["months"][period] = this_month_rev
        # 只保留最近 13 個月,避免檔案無限長
        for old in sorted(hist["months"])[:-13]:
            del hist["months"][old]
    months_sorted = sorted(hist["months"].keys())
    have_full_year = len(months_sorted) >= 12

    def ttm_revenue(code):
        """近12月營收(NTD);不足12月時以當月×12近似。回傳 (值, 是否近似)。"""
        if have_full_year:
            total = 0.0
            cnt = 0
            for m in months_sorted[-12:]:
                v = hist["months"][m].get(code)
                if v is not None:
                    total += v
                    cnt += 1
            if cnt >= 10:                       # 至少有10個月才算可靠
                return total * 1000.0, False
        # fallback:當月×12
        cur = this_month_rev.get(code)
        if cur is not None:
            return cur * 12 * 1000.0, True
        return None, True

    # --- merge ---
    stocks = []
    for code, v in val_by.items():
        if not code:
            continue
        code = str(code).strip()
        if not re.fullmatch(r"[1-9]\d{3}", code):   # 只取一般上市個股,排除 ETF(00開頭)
            continue

        pe   = num(get(v, "PEratio", "本益比"))
        pb   = num(get(v, "PBratio", "股價淨值比"))
        dy   = num(get(v, "DividendYield", "殖利率(%)", "殖利率"))
        name = get(v, "Name", "證券名稱") or (px_by.get(code, {}).get("Name")) or code

        px = px_by.get(code, {})
        close = num(get(px, "ClosingPrice", "收盤價"))

        comp = comp_by.get(code, {})
        industry = normalize_industry(get(comp, "產業別"))
        shares = num(get(comp, "已發行普通股數或TDR原股發行股數", "已發行普通股數"))
        if not shares:
            cap = num(get(comp, "實收資本額(元)", "實收資本額"))
            fv_raw = str(get(comp, "普通股每股面額") or "10").replace(",", "")
            fv = re.search(r"(\d+(?:\.\d+)?)", fv_raw)
            face = float(fv.group(1)) if fv and float(fv.group(1)) > 0 else 10.0
            if cap:
                shares = cap / face

        ps = None
        ps_approx = False
        if close is not None and shares:
            mktcap = close * shares
            rev_ttm, approx = ttm_revenue(code)
            if rev_ttm and rev_ttm > 0:
                ps = round(mktcap / rev_ttm, 2)
                ps_approx = approx

        # 至少要有價格 + 一個估值指標才納入
        if close is None or (pe is None and pb is None and dy is None and ps is None):
            continue

        stocks.append({
            "code": code,
            "name": str(name).strip(),
            "industry": industry,
            "price": round(close, 2) if close is not None else None,
            "pe": round(pe, 2) if pe is not None else None,
            "pb": round(pb, 2) if pb is not None else None,
            "yield": round(dy, 2) if dy is not None else None,
            "ps": ps,
            **({"ps_approx": True} if ps_approx and ps is not None else {}),
        })

    stocks.sort(key=lambda s: (s["industry"], s["code"]))

    now = datetime.now(TPE)
    data = {
        "updated": now.strftime("%Y-%m-%d %H:%M (台北)"),
        "market": "TWSE",
        "sample": False,
        "count": len(stocks),
        "ps_basis": "近12月營收" if have_full_year else "當月營收×12(近似,累積滿一年後自動轉為近12月)",
        "stocks": stocks,
    }

    save_json(data, out_path)
    save_json(hist, hist_path)
    print(f"✓ 完成:{len(stocks)} 檔 → {out_path}")
    print(f"  P/S 基準:{data['ps_basis']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data.json", help="輸出檔路徑(預設 data.json)")
    ap.add_argument("--history", default="rev_history.json", help="月營收歷史檔(用於近12月營收)")
    args = ap.parse_args()
    build(args.out, args.history)


if __name__ == "__main__":
    main()
