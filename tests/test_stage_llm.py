"""stage_llm.py のテスト (issue #580)。claude -p は起動しない"""
from datetime import date

import pytest

import stage_llm


@pytest.mark.parametrize("text, expected", [
    ("下落が続く。\n30週線を割った。\nSTAGE: 3S~4S", ("3S~4S", "下落が続く。\n30週線を割った。")),
    ("根拠\n`STAGE: 1Sor3S`", ("1Sor3S", "根拠")),
    ("根拠\nSTAGE: 5S", None),        # 選択肢に無い
    ("根拠だけで答えが無い", None),
])
def test_parse_stage(text, expected):
    parsed = stage_llm.parse_stage(text)
    assert (parsed and (parsed["stage"], parsed["rationale"])) == (expected or None)


LLM = {"stage": "3S~4S", "run_date": "2026-11-09"}


@pytest.mark.parametrize("human, llm, updated, expected", [
    ("2S(3B)", LLM, date(2026, 11, 1), True),
    ("2S(3B)~3S", LLM, date(2026, 11, 1), False),       # 3S で重なる
    ("1Sor3S", LLM, date(2026, 11, 1), False),           # 1Sor3S は {1S, 3S}
    ("2S", LLM, date(2026, 11, 9), False),               # 読んだ日に人が見直した
    ("2S", LLM, None, True),                             # 更新日が無い
    ("", LLM, None, False),                              # 人のラベルが空なら知らせない
    ("2S", None, None, False),                           # 読み取りが無い
])
def test_is_mismatch(human, llm, updated, expected):
    assert stage_llm.is_mismatch(human, llm, updated) is expected


def _rows(n, monday=date(2026, 11, 2)):
    """週足キャッシュの行 (新しい週が先頭)"""
    from datetime import timedelta
    rows = []
    for i in range(n):
        d = monday - timedelta(weeks=i)
        rows.append((f"{d.year}年{d.month}月{d.day}日", "100", "110", "90", "105", "0", "0", "1,000"))
    return rows


@pytest.mark.parametrize("weeks, saved_week, force, expected", [
    (60, None, False, True),           # 初回
    (60, "2026-10-26", False, True),   # 新しい週が確定した
    (60, "2026-11-02", False, False),  # 同じ週は読み直さない (同じ日に2回走っても)
    (60, "2026-11-02", True, True),    # 銘柄を指定したら読み直す
    (40, None, False, False),          # 週足が足りない
])
def test_select_targets(monkeypatch, weeks, saved_week, force, expected):
    monkeypatch.setattr(stage_llm, "_weekly_rows", lambda code_s: _rows(weeks))
    latest = {"1001": {"week": saved_week}} if saved_week else {}

    targets = stage_llm.select_targets([{"code_s": "1001"}], latest, force=force)

    assert bool(targets) is expected
    if targets:
        assert targets[0]["week"] == "2026-11-02"
