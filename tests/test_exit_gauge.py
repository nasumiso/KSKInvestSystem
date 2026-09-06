"""出口ゲージの数値・描画・既存判定との接続を検証する。"""

import xml.etree.ElementTree as ET

import pytest

from exit_line import evaluate_exit_signal, exit_line_values
from webapp import helpers


@pytest.mark.parametrize("stop,ma,expected_y", [
    (100, None, [6]), (None, 100, [18]), (100, 100, [6, 18]),
    (None, None, []), (0, float("nan"), []), (True, float("inf"), []),
])
def test_gauge_tracks(stop, ma, expected_y):
    payload = helpers.exit_line_gauge_svg({
        "close": 100, "stop_loss_line": stop, "ma_value": ma, "ma_label": "日足50MA",
    }, "防", "<理由>&")
    if not expected_y:
        assert payload == {"svg": "", "tooltip": "<理由>&"}
        return
    root = ET.fromstring(payload["svg"])
    assert (root.get("width"), root.get("height")) == ("56", "24")
    circles = root.findall("{*}circle")
    assert [int(c.get("cy")) for c in circles] == expected_y
    assert all(float(c.get("cx")) == 28 for c in circles)
    assert root.find("{*}title").text == payload["tooltip"]
    assert "(+0.0%)" in payload["tooltip"]
    assert "<理由>" not in payload["svg"]


@pytest.mark.parametrize("close,x,pct", [(50, 2, "-50.0%"), (100, 28, "+0.0%"), (150, 54, "+50.0%")])
def test_gauge_clips_marker_but_not_tooltip(close, x, pct):
    payload = helpers.exit_line_gauge_svg({"close": close, "stop_loss_line": 100})
    root = ET.fromstring(payload["svg"])
    assert float(root.find("{*}circle").get("cx")) == x
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
