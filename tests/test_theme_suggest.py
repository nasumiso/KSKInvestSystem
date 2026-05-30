"""theme_suggest.py のテスト (issue #297)。

claude -p は実際に呼ばず、_run_claude を monkeypatch でモックする。
"""
import pytest

import theme_suggest as ts


# ==================================================
# build_business_text
# ==================================================
@pytest.mark.parametrize(
    "ro, comments, so, expected_contains, expected_empty",
    [
        # 全空 → ""
        (None, None, None, [], True),
        ("", [], "", [], True),
        # research overview のみ
        ("半導体製造装置の専業", None, None, ["企業概要", "半導体製造装置"], False),
        # 四季報コメント (period 有り/無り混在、空コメントはスキップ)
        (
            None,
            [{"period": "26.1", "comment": "増益"}, {"period": "", "comment": "新工場"}, {"comment": ""}],
            None,
            ["四季報コメント", "(26.1) 増益", "新工場"],
            False,
        ),
        # 3 ソース全部
        ("特色テキスト", [{"period": "25.11", "comment": "好調"}], "株探概要", ["特色テキスト", "好調", "株探概要"], False),
    ],
)
def test_build_business_text(ro, comments, so, expected_contains, expected_empty):
    result = ts.build_business_text(ro, comments, so)
    if expected_empty:
        assert result == ""
    else:
        for token in expected_contains:
            assert token in result


# ==================================================
# suggest_gyoutai_themes (Phase 2: 確信度 + 新テーマ)
# ==================================================
EMPTY = {"preset": [], "low": [], "new": []}


@pytest.mark.parametrize(
    "business_text, theme_names, llm_result, expected",
    [
        # business_text 空 → LLM 呼ばず空 dict
        ("", ["AI", "半導体"], '{"matched": [{"name": "AI", "confidence": 90}]}', EMPTY),
        # theme_names 空 → 空 dict
        ("半導体の会社", [], '{"matched": [{"name": "半導体", "confidence": 90}]}', EMPTY),
        # パース不能 → 空 dict
        ("半導体の会社", ["AI", "半導体"], "JSONじゃない応答", EMPTY),
        # confidence で preset / low に振り分け (60 が閾値)
        (
            "半導体とAIの会社",
            ["AI", "半導体"],
            '{"matched": [{"name": "半導体", "confidence": 85}, {"name": "AI", "confidence": 40}]}',
            {
                "preset": [{"name": "半導体", "confidence": 85}],
                "low": [{"name": "AI", "confidence": 40}],
                "new": [],
            },
        ),
        # マスター外 matched は除去 (防衛 は一覧に無い)
        (
            "AIと防衛の会社",
            ["AI", "半導体"],
            '{"matched": [{"name": "AI", "confidence": 80}, {"name": "防衛", "confidence": 90}]}',
            {"preset": [{"name": "AI", "confidence": 80}], "low": [], "new": []},
        ),
        # 高確信ゼロ → preset 空 (無理やり埋めない)。新テーマは new に。
        (
            "SMS配信代行サービス",
            ["AI", "半導体"],
            '{"matched": [{"name": "AI", "confidence": 30}], '
            '"new": [{"name": "認証ソリューション", "confidence": 75, "reason": "認証用途が主力"}]}',
            {
                "preset": [],
                "low": [{"name": "AI", "confidence": 30}],
                "new": [{"name": "認証ソリューション", "confidence": 75, "reason": "認証用途が主力"}],
            },
        ),
        # preset は max 2 件で打ち切り (confidence 降順)、フェンス付きでも抽出
        (
            "色々な事業",
            ["AI", "半導体", "EV"],
            '```json\n{"matched": [{"name": "EV", "confidence": 70}, '
            '{"name": "AI", "confidence": 95}, {"name": "半導体", "confidence": 80}]}\n```',
            {
                "preset": [{"name": "AI", "confidence": 95}, {"name": "半導体", "confidence": 80}],
                "low": [{"name": "EV", "confidence": 70}],
                "new": [],
            },
        ),
    ],
)
def test_suggest_gyoutai_themes(monkeypatch, business_text, theme_names, llm_result, expected):
    monkeypatch.setattr(ts, "_run_claude", lambda prompt, timeout_sec: llm_result)
    result = ts.suggest_gyoutai_themes(business_text, theme_names)
    assert result == expected
