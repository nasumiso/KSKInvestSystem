"""CNN Fear & Greed Index パーサーのテスト。"""

from datetime import datetime

import pytest

import fng


def _payload(score=66.9, rating="greed", hist_len=25):
    return {
        "fear_and_greed": {
            "score": score,
            "rating": rating,
        },
        "fear_and_greed_historical": {
            "data": [{"x": i, "y": float(i)} for i in range(hist_len)],
        },
    }


def test_parse_current_score_and_rating():
    result = fng.parse_fear_and_greed(
        _payload(score=66.9, rating="Greed"),
        access_date=datetime(2026, 5, 23, 12, 0),
    )
    assert result["score"] == 66.9
    assert result["rating"] == "greed"
    assert result["access_date"] == datetime(2026, 5, 23, 12, 0)


def test_parse_5d_and_20d_scores_from_dict_points():
    result = fng.parse_fear_and_greed(_payload(hist_len=25))
    assert result["score_5d_ago"] == 20.0
    assert result["score_20d_ago"] == 5.0


def test_parse_history_numeric_points():
    payload = _payload(hist_len=25)
    payload["fear_and_greed_historical"]["data"] = [float(i) for i in range(25)]
    result = fng.parse_fear_and_greed(payload)
    assert result["score_5d_ago"] == 20.0
    assert result["score_20d_ago"] == 5.0


def test_parse_rejects_short_history():
    with pytest.raises(ValueError):
        fng.parse_fear_and_greed(_payload(hist_len=19))


def test_fetch_uses_required_headers(monkeypatch):
    captured = {}

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return _payload()

    def fake_get(url, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        return Resp()

    monkeypatch.setattr(fng.requests, "get", fake_get)
    result = fng.fetch_fear_and_greed(timeout=3)
    assert result["score"] == 66.9
    assert captured["url"] == fng.FNG_URL
    assert captured["headers"]["Referer"] == "https://edition.cnn.com/"
    assert captured["headers"]["Origin"] == "https://edition.cnn.com"
    assert "Mozilla/5.0" in captured["headers"]["User-Agent"]
    assert captured["timeout"] == 3
