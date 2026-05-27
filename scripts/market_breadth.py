"""市場ブレッドス・信用評価データの取得モジュール (issue #209 / #211)。

nikkei225jp.com が公開している JS データを HTTP 取得し、JSON 配列としてパースする。

このファイルでは issue #211 のスコープ (信用評価損益率・信用倍率の週次取得) のみ実装している。
issue #209 (新高値・新安値) は同モジュールに `fetch_market_breadth_daily()` として
後追いで追加する想定。
"""

import json
import re
from datetime import datetime, timezone, timedelta

import requests

JST = timezone(timedelta(hours=9))

CREDIT_BALANCE_URL = "https://nikkei225jp.com/_data/_nfsWEB/DAY/dailyweek2.json"
# Referer が無いと 404 を返すサーバー設定 (2026-05 時点で確認)
CREDIT_BALANCE_REFERER = "https://nikkei225jp.com/data/sinyou.php"


def fetch_credit_balance_weekly():
    """dailyweek2.json を取得し、信用評価損益率の週次時系列を返す。

    出典: nikkei225jp.com 経由、2市場（東証＋名証）信用評価損益率。
          日経新聞が JPX 公表の信用残高から平均建値を推計して算出した値。

    Returns:
        list of dict: [{date: 'YYYY-MM-DD', credit_eval_rate: float,
                        credit_bairitsu: float|None}, ...]
                      日付昇順、信用評価率が空文字の行はスキップする。
    """
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": CREDIT_BALANCE_REFERER,
    }
    res = requests.get(CREDIT_BALANCE_URL, headers=headers, timeout=15)
    res.raise_for_status()
    return parse_credit_balance(res.text)


def parse_credit_balance(text):
    """dailyweek2.json のレスポンス本文 (var DAILY = [...];) をパース。

    Args:
        text: HTTP レスポンス本文 (JS 配列リテラル)

    Returns:
        list of dict: 日付昇順、信用評価率が空文字の行はスキップ。
    """
    m = re.search(r"var DAILY\s*=\s*(\[.*\])\s*;", text, re.S)
    if not m:
        raise ValueError("DAILY 配列が見つからない")
    rows = json.loads(m.group(1))
    # 列構成 (2026-05 時点で実データ突き合わせ確認済):
    #   [0] unix timestamp (ms)
    #   [7] 信用評価損益率 (%)   ← 負値あり、最新行は公表前で "" のことがある
    #   [8] 信用倍率 (買い残/売り残)
    out = []
    for r in rows:
        eval_rate = r[7]
        if eval_rate == "":
            continue
        ts_ms = r[0]
        d = datetime.fromtimestamp(ts_ms / 1000, tz=JST).date().isoformat()
        bairitsu = r[8] if len(r) > 8 and r[8] != "" else None
        out.append({
            "date": d,
            "credit_eval_rate": float(eval_rate),
            "credit_bairitsu": float(bairitsu) if bairitsu is not None else None,
        })
    return out
