"""テーマ別のブレイク集計のテスト (issue #509)。"""

import os
import shutil

import pytest

import theme_breakout as tb

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "shintakane_result_509")


@pytest.mark.parametrize("date,expected", [
    ("2026-09-15", True),   # テーマランクも同日に 11位 → 4位
    ("2026-09-24", False),  # 1〜5 銘柄に落ち着いた日
    ("2026-10-06", True),   # 28 銘柄に広がった日
])
def test_実データでセキュリティが該当する日(tmp_path, monkeypatch, date, expected):
    """運用機の日次レポートからセキュリティ系の行だけを残した fixture で、該当日を固定する。

    30日を過ぎた CSV は history/ に移るので、古い半分をそちらに置いて両方から読めることも見る。
    """
    names = sorted(os.listdir(FIXTURE_DIR))
    os.makedirs(tmp_path / "history")
    for i, name in enumerate(names):
        dst = tmp_path / "history" / name if i < len(names) // 2 else tmp_path / name
        shutil.copy(os.path.join(FIXTURE_DIR, name), dst)
    monkeypatch.setattr(tb, "RESULT_DIR", str(tmp_path))

    paths = tb.recent_result_csvs(n=len(names))
    assert len(paths) == len(names)
    days = [(d, tb.load_result_csv(p)) for d, p in paths if d <= date]
    result = tb.build_breakout(days[-(tb.WINDOW + 1):], {})

    assert result["date"] == date
    hit = any("サイバーセキュリティ" in [r["theme"]] + r["aliases"] for r in result["rows"])
    assert hit is expected


def _row(code_s, themes, *, kind="高", first=True, score=50.0):
    return {"code_s": code_s, "name": f"銘柄{code_s}", "kind": kind, "first": first,
            "score": score, "themes": themes, "change": "+1.00%", "overview": "", "news": ""}


def test_重複テーマを畳み_管理対象を先に_未フォローは初出の新高値だけ(monkeypatch):
    monkeypatch.setattr(tb, "MAX_ROWS", 2)
    quiet = [("2026-10-%02d" % d, [_row("9999", ["A"])]) for d in range(1, 21)]
    today = (
        # A: 7銘柄。B は A と 5/6 が重なるので A に畳む
        [_row("1001", ["A", "B"], first=False), _row("1002", ["A", "B"]),
         _row("1003", ["A", "B"], score=90.0), _row("1004", ["A", "B"], score=80.0),
         _row("1005", ["A", "B"], score=70.0), _row("1006", ["A"], kind="出"),
         _row("1007", ["A"], first=False)]
        + [_row("1008", ["B"])]
        # C・D は別の銘柄群。MAX_ROWS=2 なので D (5銘柄) は省かれる
        + [_row("20%02d" % i, ["C"]) for i in range(6)]
        + [_row("30%02d" % i, ["D"]) for i in range(5)]
    )
    managed = {"1007": "3監", "1001": "2準", "1002": "1保"}

    result = tb.build_breakout(quiet + [("2026-10-21", today)], managed)

    assert [r["theme"] for r in result["rows"]] == ["A", "C"]
    assert result["more"] == 1
    a = result["rows"][0]
    assert a["aliases"] == ["B"]
    assert a["count"] == 8  # A の7銘柄 + B だけの 1008
    assert [m["code_s"] for m in a["managed"]] == ["1002", "1001", "1007"]
    # 初出かつ「高」を業績スコア順に2つまで。出来高急増だけの 1006 は出さない
    assert [u["code_s"] for u in a["unfollowed"]] == ["1003", "1004"]
    assert a["others"] == 8 - 3 - 2
    assert a["discovery_reason"] == "発掘元:日次レポート テーマ=A 日付=2026-10-21"
