"""market_breadth / 信用評価関連のテスト (issue #211)。"""

import json
import os
from datetime import date
from unittest.mock import patch

import pytest

import market_breadth
import make_market_db
import shintakane


# --- market_breadth.parse_credit_balance ----------------------------------

# 2026-04-24 12:00 JST と 2026-05-01 12:00 JST に相当する unix ms
TS_0424 = 1776999600000
TS_0501 = 1777604400000


def _make_daily_js(rows):
    """parse_credit_balance に食わせる JS テキストを組み立て。"""
    items = []
    for r in rows:
        items.append(
            "[" + ",".join(("\"\"" if v == "" else str(v)) for v in r) + "]"
        )
    return "var DAILY = [" + ",".join(items) + "];"


def test_parse_credit_balance_skips_empty_eval():
    """信用評価カラム [7] が空文字の行はスキップされる。"""
    rows = [
        [TS_0424, 0, 0, 0, 0, 0, 0, -5.45, 5.74],
        [TS_0501, 0, 0, 0, 0, 0, 0, "", ""],
    ]
    out = market_breadth.parse_credit_balance(_make_daily_js(rows))
    assert len(out) == 1
    assert out[0]["date"] == "2026-04-24"
    assert out[0]["credit_eval_rate"] == -5.45
    assert out[0]["credit_bairitsu"] == 5.74


def test_parse_credit_balance_bairitsu_can_be_none():
    """信用倍率カラムが空でも評価率さえあれば bairitsu=None で取り込む。"""
    rows = [[TS_0501, 0, 0, 0, 0, 0, 0, -4.82, ""]]
    out = market_breadth.parse_credit_balance(_make_daily_js(rows))
    assert out == [{
        "date": "2026-05-01",
        "credit_eval_rate": -4.82,
        "credit_bairitsu": None,
    }]


# --- make_market_db._html_market_indicators (信用評価損益率パート) -------

def _write_credit_json(tmp_path, history):
    """credit_balance.json を $KS_DATA_DIR/code_rank_data/ に書く。"""
    code_rank = tmp_path / "code_rank_data"
    code_rank.mkdir(parents=True, exist_ok=True)
    path = code_rank / "credit_balance.json"
    path.write_text(json.dumps({"history": history}), encoding="utf-8")
    return path


def test_market_indicators_renders_eval_and_bairitsu(tmp_path, monkeypatch):
    """評価率・倍率いずれも 1週/4週前比 pt 差付きで統合表に出る。"""
    history = [
        {"date": "2026-04-03", "credit_eval_rate": -7.67, "credit_bairitsu": 6.07},
        {"date": "2026-04-10", "credit_eval_rate": -6.59, "credit_bairitsu": 5.30},
        {"date": "2026-04-17", "credit_eval_rate": -4.46, "credit_bairitsu": 5.81},
        {"date": "2026-04-24", "credit_eval_rate": -5.45, "credit_bairitsu": 5.74},
        {"date": "2026-05-01", "credit_eval_rate": -4.82, "credit_bairitsu": 6.85},
    ]
    _write_credit_json(tmp_path, history)
    monkeypatch.setattr(make_market_db, "DATA_DIR", str(tmp_path))

    html = make_market_db._html_market_indicators({})
    assert 'class="market-indicators"' in html
    assert "信用評価損益率" in html
    assert 'href="https://nikkei225jp.com/data/sinyou.php"' in html
    assert "-4.82%" in html  # 最新 evaluation rate
    assert "6.85" in html    # 最新 bairitsu
    # 1週比 (latest - 1個前): -4.82 - (-5.45) = +0.63
    assert "+0.63" in html
    # 4週比 (latest - 4個前): -4.82 - (-7.67) = +2.85
    assert "+2.85" in html
    # 倍率の 1週比: 6.85 - 5.74 = +1.11
    assert "+1.11" in html


def test_market_indicators_returns_empty_when_no_data(tmp_path, monkeypatch):
    """credit_balance.json も Fear & Greed も無ければ空文字 (HTML を壊さない)。"""
    monkeypatch.setattr(make_market_db, "DATA_DIR", str(tmp_path))
    assert make_market_db._html_market_indicators({}) == ""


@pytest.mark.parametrize("latest, prev, expected_text, expected_cls", [
    (-4.82, -5.45, "+0.63", "fng-delta-pos"),
    (-5.45, -4.46, "-0.99", "fng-delta-neg"),
    (-4.82, None, "—", ""),
])
def test_format_credit_delta(latest, prev, expected_text, expected_cls):
    text, cls = make_market_db._format_credit_delta(latest, prev)
    assert text == expected_text
    assert cls == expected_cls


# --- shintakane.update_credit_balance キャッシュ判定 ----------------------

def _write_cache(tmp_path, generated_at, latest_date="2026-05-15"):
    code_rank = tmp_path / "code_rank_data"
    code_rank.mkdir(parents=True)
    cache = code_rank / "credit_balance.json"
    cache.write_text(json.dumps({
        "generated_at": generated_at,
        "latest": {"date": latest_date, "credit_eval_rate": -4.47, "credit_bairitsu": 6.84},
        "history": [{"date": latest_date, "credit_eval_rate": -4.47, "credit_bairitsu": 6.84}],
    }), encoding="utf-8")
    return cache


def test_update_credit_balance_skips_when_cache_fresh(tmp_path, monkeypatch):
    """generated_at が today と同日なら fetch を呼ばずに return する。"""
    _write_cache(tmp_path, generated_at="2026-05-26T22:00:00")

    monkeypatch.setattr(shintakane, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(shintakane, "get_price_day", lambda dt: date(2026, 5, 26))

    with patch("market_breadth.fetch_credit_balance_weekly") as fake_fetch:
        shintakane.update_credit_balance()
        fake_fetch.assert_not_called()


def test_update_credit_balance_fetches_when_cache_stale(tmp_path, monkeypatch):
    """generated_at が today より古ければ fetch する。"""
    cache = _write_cache(tmp_path, generated_at="2026-05-25T10:00:00")

    monkeypatch.setattr(shintakane, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(shintakane, "get_price_day", lambda dt: date(2026, 5, 26))

    fake_rows = [
        {"date": "2026-05-15", "credit_eval_rate": -4.47, "credit_bairitsu": 6.84},
        {"date": "2026-05-22", "credit_eval_rate": -4.10, "credit_bairitsu": 6.66},
    ]
    with patch("market_breadth.fetch_credit_balance_weekly", return_value=fake_rows) as fake_fetch:
        shintakane.update_credit_balance()
        fake_fetch.assert_called_once()
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["latest"]["date"] == "2026-05-22"
