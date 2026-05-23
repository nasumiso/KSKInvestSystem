#!/usr/bin/env python3
"""CNN Fear & Greed Index の取得・パース。"""

from datetime import datetime

import requests


FNG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
FNG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://edition.cnn.com/",
    "Origin": "https://edition.cnn.com",
}


def _score_from_point(point):
    """CNN履歴1点から score を取り出す。"""
    if isinstance(point, dict):
        for key in ("y", "score", "value"):
            if key in point:
                return float(point[key])
    return float(point)


def parse_fear_and_greed(payload, access_date=None):
    """CNN Fear & Greed JSON から保存用 dict を作る。

    Args:
        payload: CNN API の JSON dict
        access_date: 取得日時。Noneなら現在時刻
    Returns:
        dict: market_db["fear_and_greed"] に保存する値
    """
    fg = payload.get("fear_and_greed") or {}
    hist = ((payload.get("fear_and_greed_historical") or {}).get("data") or [])
    if len(hist) < 20:
        raise ValueError("Fear & Greed historical data is too short")

    score = float(fg["score"])
    rating = str(fg.get("rating", "")).strip().lower()
    score_5d_ago = _score_from_point(hist[-5])
    score_20d_ago = _score_from_point(hist[-20])
    return {
        "score": score,
        "rating": rating,
        "score_5d_ago": score_5d_ago,
        "score_20d_ago": score_20d_ago,
        "access_date": access_date or datetime.now(),
    }


def fetch_fear_and_greed(timeout=10):
    """CNN API から Fear & Greed Index を取得する。"""
    resp = requests.get(FNG_URL, headers=FNG_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return parse_fear_and_greed(resp.json(), access_date=datetime.now())
