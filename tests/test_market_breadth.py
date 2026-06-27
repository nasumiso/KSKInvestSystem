"""market_breadth / 信用評価・日経VI・新高値新安値関連のテスト (issue #211, #292)。"""

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
    """評価率は値・変化セルに、倍率は tooltip (title) に統合される (issue #292)。"""
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
    # 1週比 (latest - 1個前): -4.82 - (-5.45) = +0.63
    assert "+0.63" in html
    # 4週比 (latest - 4個前): -4.82 - (-7.67) = +2.85
    assert "+2.85" in html
    # 信用倍率は独立行をやめ tooltip (title 属性) に移設
    assert 'title="' in html
    assert "信用倍率 6.85" in html
    # 倍率の 1週比: 6.85 - 5.74 = +1.11 も tooltip 内
    assert "+1.11" in html
    # -4.82% は中間水準なのでバッジは出ない
    assert "天井圏" not in html
    assert "底値圏" not in html
    # 更新日は独立列 (mi-date) に 26/5/1 形式 (ゼロ埋めなし) で出る
    assert 'class="mi-date"' in html
    assert "26/5/1" in html


@pytest.mark.parametrize("eval_rate, expected_badge, absent_badge", [
    (-2.5, "天井圏", "底値圏"),   # >= -3.0 で天井圏
    (-16.0, "底値圏", "天井圏"),  # <= -15.0 で底値圏
    (-8.0, None, None),          # 中間: バッジなし
])
def test_market_indicators_credit_badge(tmp_path, monkeypatch, eval_rate, expected_badge, absent_badge):
    """信用評価損益率の天井圏/底値圏バッジが閾値で出し分けされる (issue #292)。"""
    _write_credit_json(tmp_path, [
        {"date": "2026-05-01", "credit_eval_rate": eval_rate, "credit_bairitsu": 6.0},
    ])
    monkeypatch.setattr(make_market_db, "DATA_DIR", str(tmp_path))
    html = make_market_db._html_market_indicators({})
    if expected_badge is None:
        assert "天井圏" not in html and "底値圏" not in html
    else:
        assert expected_badge in html
        assert absent_badge not in html


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
    """generated_at が today と同日かつ latest が期待週に追いついていれば fetch しない。"""
    _write_cache(
        tmp_path,
        generated_at="2026-05-27T22:00:00",
        latest_date="2026-05-22",
    )

    monkeypatch.setattr(shintakane, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(shintakane, "get_price_day", lambda dt: date(2026, 5, 27))

    with patch("market_breadth.fetch_credit_balance_weekly") as fake_fetch:
        shintakane.update_credit_balance()
        fake_fetch.assert_not_called()


def test_update_credit_balance_refetches_when_same_day_but_latest_stale(tmp_path, monkeypatch):
    """同日生成キャッシュでも latest が期待金曜より古ければ fetch する。"""
    cache = _write_cache(
        tmp_path,
        generated_at="2026-05-27T03:00:00",
        latest_date="2026-05-15",
    )

    monkeypatch.setattr(shintakane, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(shintakane, "get_price_day", lambda dt: date(2026, 5, 27))

    fake_rows = [
        {"date": "2026-05-15", "credit_eval_rate": -4.47, "credit_bairitsu": 6.84},
        {"date": "2026-05-22", "credit_eval_rate": -4.10, "credit_bairitsu": 6.66},
    ]
    with patch("market_breadth.fetch_credit_balance_weekly", return_value=fake_rows) as fake_fetch:
        shintakane.update_credit_balance()
        fake_fetch.assert_called_once()
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["latest"]["date"] == "2026-05-22"


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


# --- issue #292: 日経VI / 新高値-新安値 ----------------------------------

def test_parse_js_rows_handles_trailing_empty_row():
    """daily2year.json の最終行は連続カンマ ([..,,,]) を含むため null 補正でパースする。

    新高値/新安値が空の最終行は fetch_new_high_low 側でスキップされる。
    """
    text = (
        "var DAILY = [\n"
        "[%d,64999.41,2461,\"\",\"\",720,790,84.14,88,131,1510],\n"
        "[%d,64693.12,2608,\"\",\"\",767,748,86.66,,,1515]\n"
        "];" % (TS_0424, TS_0501)
    )
    rows = market_breadth._parse_js_rows(text, "DAILY")
    assert len(rows) == 2
    assert rows[0][8] == 88 and rows[0][9] == 131
    # 連続カンマ箇所は null (None) に補正される
    assert rows[1][8] is None and rows[1][9] is None


def test_fetch_new_high_low_skips_unconfirmed_row():
    """新高値/新安値が数値でない最終行 (当日未確定) はスキップされる。"""
    text = (
        "var DAILY = [\n"
        "[%d,0,0,0,0,0,0,0,88,131,0],\n"
        "[%d,0,0,0,0,0,0,0,,,0]\n"
        "];" % (TS_0424, TS_0501)
    )
    with patch("market_breadth._fetch_nikkei_json", return_value=text):
        out = market_breadth.fetch_new_high_low()
    assert out == [{"date": "2026-04-24", "new_high": 88, "new_low": 131}]


def test_fetch_nikkei_vi_reads_s161_series():
    """日経VI は S621(VIX) ではなく S161 系列を読む。"""
    text = (
        "var S161 = [\n"
        "[%d,28.37],\n"
        "[%d,27.28]\n"
        "];\n"
        "var S621 = [\n"
        "[%d,21.51],\n"
        "[%d,15.40]\n"
        "];" % (TS_0424, TS_0501, TS_0424, TS_0501)
    )
    with patch("market_breadth._fetch_nikkei_json", return_value=text):
        out = market_breadth.fetch_nikkei_vi()
    assert out == [
        {"date": "2026-04-24", "nikkei_vi": 28.37},
        {"date": "2026-05-01", "nikkei_vi": 27.28},
    ]


@pytest.mark.parametrize("vi, expected", [
    (16.77, "安穏"),
    (24.0, "警戒"),
    (33.0, "恐怖"),
    (45.0, "パニック"),
])
def test_vi_rating_html(vi, expected):
    """日経VI バッジが閾値 (<20/20-30/30-40/>=40) で出し分けされる。"""
    assert expected in make_market_db._vi_rating_html(vi)


@pytest.mark.parametrize("diff, expected_badge, absent_badge", [
    (300, "天井圏", "底値圏"),   # >=251 で天井圏
    (-100, "底値圏", "天井圏"),  # <=-75 で底値圏
    (-50, None, None),          # 中間 (やや弱いが底値圏ではない): バッジなし
])
def test_breadth_rating_html(diff, expected_badge, absent_badge):
    """新高値-新安値 バッジが閾値 (天井>=+251/底<=-75) で出し分けされる (issue #292)。"""
    html = make_market_db._breadth_rating_html(diff)
    if expected_badge is None:
        assert "天井圏" not in html and "底値圏" not in html
    else:
        assert expected_badge in html
        assert absent_badge not in html


def _build_history(records, n, key_fn):
    """index -1=最新 / -6=5日前 / -21=20日前 に狙った値が来る 21 件の history を組む。

    records は {0: 最新, 5: 5日前, 20: 20日前} の dict (キーは「N営業日前」)。
    指定の無い index はダミー値 key_fn(i) で埋める。
    """
    hist = []
    for i in range(20, -1, -1):  # 20日前 → 最新 の順
        hist.append({"date": "2026-05-%02d" % (21 - i), **(records.get(i) or key_fn(i))})
    return hist


def test_fgjp_accordion_components_in_indicators(tmp_path, monkeypatch):
    """日本版 F&G の成分がアコーディオン展開行に出る (issue #212)。

    日経VI・新高値新安値は独立行をやめ、F&G 成分の内訳 (score + 元データ実値) に統合。
    """
    code_rank = tmp_path / "code_rank_data"
    code_rank.mkdir(parents=True)
    fgjp = {
        "history": [{
            "date": "2026-06-05",
            "score": 78.4,
            "components": {
                "momentum": {"score": 87.0, "raw": 19.24, "date": "2026-06-05"},
                "strength": {"score": 72.4, "raw": 39, "date": "2026-06-05"},
                "breadth": {"score": 76.6, "raw": 856, "date": "2026-06-05"},
                "volatility": {"score": 77.9, "raw": 21.51, "date": "2026-06-04"},
            },
        }],
    }
    (code_rank / "fear_greed_jp.json").write_text(
        json.dumps(fgjp), encoding="utf-8")
    monkeypatch.setattr(make_market_db, "DATA_DIR", str(tmp_path))

    html = make_market_db._html_market_indicators({})
    # 親行: 日本版F&G + 合成スコア + アコーディオン toggle
    assert "日本版Fear" in html
    assert "78.4" in html
    assert "mi-fgjp-parent" in html and "mi-fgjp-child" in html
    # 成分の展開行: 各成分名 + 0-100スコア + 元データ実値
    assert "モメンタム" in html and "新高値強さ" in html
    assert "騰落ブレッドス" in html and "日経VI" in html
    assert "21.51" in html          # 日経VI 実値
    assert "+39" in html            # 新高値-新安値 実値
    assert "+19.2%" in html         # モメンタム乖離率
    # 成分ごとの更新日を保持・表示する
    assert "26/6/5" in html
    assert "26/6/4" in html
    # 各成分に 0-100 スコア基準のバッジ (score 87/72/77/78 はいずれも Greed〜Extreme Greed)
    assert "fng-rating-extreme-greed" in html   # momentum 87
    assert "fng-rating-greed" in html           # strength 72 等
    # 各成分に参照リンク (統合前の独立行のリンクを踏襲)
    assert 'href="https://nikkei225jp.com/data/vix.php"' in html
    assert 'href="https://nikkei225jp.com/data/new.php"' in html


def test_update_fear_greed_jp_persists_component_dates(tmp_path, monkeypatch):
    """日本版 F&G は成分ごとの更新日を保持する。"""
    code_rank = tmp_path / "code_rank_data"
    code_rank.mkdir(parents=True)
    monkeypatch.setattr(shintakane, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(shintakane, "_credit_cache_is_fresh", lambda path, today: False)

    breadth_history = [
        {"date": "2026-06-04", "nikkei_close": 38000, "advances": 900, "declines": 700, "new_high": 80, "new_low": 20},
        {"date": "2026-06-05", "nikkei_close": 38100, "advances": 920, "declines": 680, "new_high": 88, "new_low": 18},
    ]
    vi_history = [
        {"date": "2026-06-03", "nikkei_vi": 23.1},
        {"date": "2026-06-04", "nikkei_vi": 21.5},
        {"date": "2026-06-06", "nikkei_vi": 25.0},
    ]

    with patch("market_breadth.fetch_market_breadth_daily", return_value=breadth_history), \
            patch("market_breadth.fetch_nikkei_vi", return_value=vi_history), \
            patch("fear_greed_jp.compute_fear_greed_jp", return_value={
                "score": 78.4,
                "rating": "Extreme Greed",
                "components": {
                    "momentum": {"score": 87.0, "raw": 19.24},
                    "strength": {"score": 72.4, "raw": 39},
                    "breadth": {"score": 76.6, "raw": 856},
                    "volatility": {"score": 77.9, "raw": 21.51},
                },
            }):
        shintakane.update_fear_greed_jp()

    payload = json.loads((code_rank / "fear_greed_jp.json").read_text(encoding="utf-8"))
    components = payload["latest"]["components"]
    assert components["momentum"]["date"] == "2026-06-05"
    assert components["strength"]["date"] == "2026-06-05"
    assert components["breadth"]["date"] == "2026-06-05"
    # VI は breadth 最新日以前の最終日を使う
    assert components["volatility"]["date"] == "2026-06-04"


def test_fgjp_absent_falls_back_to_vi_and_breadth_rows(tmp_path, monkeypatch):
    """fear_greed_jp.json が無い日は 日経VI・新高値新安値を独立行で表示する (退行防止)。

    F&G アコーディオンに統合したことで、F&G を出せない日に従来表示が消えるのを防ぐ。
    """
    code_rank = tmp_path / "code_rank_data"
    code_rank.mkdir(parents=True)
    # fear_greed_jp.json は作らない (取得失敗日を再現)
    vi_hist = _build_history(
        {0: {"nikkei_vi": 16.77}, 5: {"nikkei_vi": 16.5}, 20: {"nikkei_vi": 17.0}},
        20, lambda i: {"nikkei_vi": 16.0})
    (code_rank / "nikkei_vi.json").write_text(
        json.dumps({"history": vi_hist}), encoding="utf-8")
    nhl_hist = _build_history(
        {0: {"new_high": 88, "new_low": 131}},
        20, lambda i: {"new_high": 50, "new_low": 50})
    (code_rank / "new_high_low.json").write_text(
        json.dumps({"history": nhl_hist}), encoding="utf-8")
    monkeypatch.setattr(make_market_db, "DATA_DIR", str(tmp_path))

    html = make_market_db._html_market_indicators({})
    # F&G アコーディオンの親行は出ない (トグルJSのセレクタ文字列とは区別して tr で判定)
    assert 'class="mi-fgjp-parent"' not in html
    assert "日本版Fear" not in html
    # 日経VI・新高値新安値が独立行でフォールバック表示される
    assert "日経VI" in html and "16.77" in html
    assert "新高値-新安値" in html
