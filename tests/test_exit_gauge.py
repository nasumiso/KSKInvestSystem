"""出口ゲージの数値・描画・既存判定との接続を検証する。"""

import xml.etree.ElementTree as ET
import re

import pytest

from exit_line import evaluate_exit_signal, exit_line_values
from webapp import helpers


@pytest.mark.parametrize("stop,ma,expected_kinds", [
    (100, None, ["stop"]), (None, 100, ["ma"]), (100, 100, ["stop", "ma"]),
    (None, None, []), (0, float("nan"), []), (True, float("inf"), []),
])
def test_gauge_tracks(stop, ma, expected_kinds):
    payload = helpers.exit_line_gauge_svg({
        "close": 100, "stop_loss_line": stop, "ma_value": ma, "ma_label": "日足50MA",
    }, "防", "<理由>&")
    if not expected_kinds:
        assert payload == {"svg": "", "tooltip": "<理由>&"}
        return
    root = ET.fromstring(payload["svg"])
    assert (root.get("width"), root.get("height")) == ("40", "14")
    markers = [p for p in root.findall("{*}path") if "exit-gauge-marker" in p.get("class", "").split()]
    coords = [re.fullmatch(r"M([\d.]+) (\d+)V\d+", p.get("d")).groups() for p in markers]
    assert [p.get("class").split()[1] for p in markers] == expected_kinds
    assert all((float(x), y) == (10.5, "0") for x, y in coords)
    if "stop" in expected_kinds:
        assert next(p for p in markers if "stop" in p.get("class", "").split()).get("stroke-dasharray") == "2,2"
    if "ma" in expected_kinds:
        assert next(p for p in markers if "ma" in p.get("class", "").split()).get("stroke-dasharray") is None
    # halo と縦線に加えて乖離0%の基準線1本
    baseline = [p for p in root.findall("{*}path") if p.get("class") == "exit-gauge-baseline"]
    assert len(baseline) == 1
    assert re.fullmatch(r"M10.50 0V\d+", baseline[0].get("d"))
    assert len(root.findall("{*}path")) == len(markers) * 2 + 1
    assert root.find("{*}title").text == payload["tooltip"]
    assert "(+0.0%)" in payload["tooltip"]
    assert "<理由>" not in payload["svg"]


@pytest.mark.parametrize("close,x,pct", [
    (50, 1, "-50.0%"),        # 下方クリップ(左端 -8.33% 未満)
    (95, 4.8, "-5.0%"),       # 防衛ライン割れは左側25%に圧縮される
    (100, 10.5, "+0.0%"),     # 乖離0% = 基準線位置
    (125, 39, "+25.0%"),      # 右端ちょうど
    (150, 39, "+50.0%"),      # 上方クリップ
])
def test_gauge_clips_marker_but_not_tooltip(close, x, pct):
    payload = helpers.exit_line_gauge_svg({"close": close, "stop_loss_line": 100})
    root = ET.fromstring(payload["svg"])
    marker = next(p for p in root.findall("{*}path") if "exit-gauge-marker" in p.get("class", "").split())
    assert float(re.fullmatch(r"M([\d.]+) \d+V\d+", marker.get("d")).group(1)) == x
    assert pct in payload["tooltip"]


@pytest.mark.parametrize("kind,window", [("day", 50), ("week", 30), ("week", 40)])
@pytest.mark.parametrize("state,violation,level", [
    ({}, {}, None), ({"triggered": True}, {}, "防歴"),
    ({}, {"pending": True}, "防予"), ({}, {"confirmed": True}, "防"),
])
def test_values_available_without_changing_signal(kind, window, state, violation, level):
    rule = {"ma_kind": kind, "ma_window": window}
    key = "ma50_violation" if kind == "day" else f"wma{window}_violation"
    stock = {"price_log": [("2026-09-04", 110)], key: {"ma_value": 100, **violation}}
    position = {"stop_loss_line": 90}
    signal = evaluate_exit_signal(rule, stock, position, state)
    assert (signal["level"] if signal else None) == level
    values = exit_line_values(rule, stock, position)
    payload = helpers.exit_line_gauge_svg(values, level or "")
    assert "損切りライン 90 (+22.2%)" in payload["tooltip"]
    assert f"{'日足' if kind == 'day' else '週足'}{window}MA 100 (+10.0%)" in payload["tooltip"]


@pytest.mark.parametrize("close", [None, 0, -1, True, float("nan"), float("inf")])
def test_missing_close_hides_gauge(close):
    assert helpers.exit_line_gauge_svg({"close": close, "stop_loss_line": 100})["svg"] == ""
