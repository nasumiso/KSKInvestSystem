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
# suggest_gyoutai_themes
# ==================================================
@pytest.mark.parametrize(
    "business_text, theme_names, llm_result, expected",
    [
        # business_text 空 → LLM 呼ばず []
        ("", ["AI", "半導体"], '["AI"]', []),
        # theme_names 空 → []
        ("半導体の会社", [], '["半導体"]', []),
        # マスター外を除去 (防衛 はマスターに無い)
        ("AIと防衛の会社", ["AI", "半導体"], '["AI", "防衛"]', ["AI"]),
        # 重複除去 + max_suggest=2 で打ち切り
        (
            "色々な事業",
            ["AI", "半導体", "EV"],
            '["AI", "AI", "半導体", "EV"]',
            ["AI", "半導体"],
        ),
        # フェンス付き・説明文付きでも配列を抽出
        ("半導体の会社", ["AI", "半導体"], 'おすすめ: ```json\n["半導体"]\n```', ["半導体"]),
        # パース不能 → []
        ("半導体の会社", ["AI", "半導体"], "JSONじゃない応答", []),
    ],
)
def test_suggest_gyoutai_themes(monkeypatch, business_text, theme_names, llm_result, expected):
    monkeypatch.setattr(ts, "_run_claude", lambda prompt, timeout_sec: llm_result)
    result = ts.suggest_gyoutai_themes(business_text, theme_names)
    assert result == expected
