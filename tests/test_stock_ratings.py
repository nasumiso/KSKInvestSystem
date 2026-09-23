"""stock_ratings.py のテスト (tmp_path 上に JSON を作る。本番の KS_DATA_DIR には触れない)"""

import csv
import json

import pytest

import stock_ratings as sr

BASE_FIELDS = {
    "name": "SHIFT", "fund": 35, "mispricing": 16, "momentum": 16, "valuation": 15,
    "confidence": "A", "status": "Active", "thesis": "QA需要拡大", "risks": "採用鈍化",
}


@pytest.fixture
def ratings_dir(tmp_path):
    """3697 と 6134 を登録済みのディレクトリ"""
    sr.update_rating("3697", BASE_FIELDS, "初期登録", "test", ratings_dir=tmp_path)
    sr.update_rating("6134", dict(BASE_FIELDS, name="FUJI"), "初期登録", "test", ratings_dir=tmp_path)
    return tmp_path


def _snapshot(ratings_dir):
    return tuple(
        (ratings_dir / name).read_bytes()
        for name in (sr.RATINGS_FILENAME, sr.HISTORY_FILENAME)
    )


def _history(ratings_dir):
    lines = (ratings_dir / sr.HISTORY_FILENAME).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


@pytest.mark.parametrize("fields, expected_changes", [
    ({"fund": 36}, {"scores.fund": [35, 36]}),
    ({"thesis": "AI駆動開発"}, {"thesis": ["QA需要拡大", "AI駆動開発"]}),
    ({"risks": ""}, {"risks": ["採用鈍化", ""]}),
])
def test_partial_update_keeps_other_fields(ratings_dir, fields, expected_changes):
    """指定しなかった項目・他の銘柄は変わらず、変わった項目だけが履歴に残る"""
    before_3697 = sr.get_rating("3697", ratings_dir=ratings_dir)
    before_6134 = sr.get_rating("6134", ratings_dir=ratings_dir)

    changes = sr.update_rating("3697", fields, "2Q決算反映", "cli", ratings_dir=ratings_dir)

    assert changes == expected_changes
    after_3697 = sr.get_rating("3697", ratings_dir=ratings_dir)
    changed_keys = {k.split(".")[-1] for k in expected_changes}
    for key, value in before_3697.items():
        if key == "scores":
            for axis, score in value.items():
                if axis not in changed_keys:
                    assert after_3697["scores"][axis] == score
        elif key not in changed_keys | {"total", "updated_at"}:
            assert after_3697[key] == value
    assert sr.get_rating("6134", ratings_dir=ratings_dir) == before_6134
    last = _history(ratings_dir)[-1]
    assert (last["code_s"], last["source"], last["reason"], last["changes"]) == (
        "3697", "cli", "2Q決算反映", expected_changes)


@pytest.mark.parametrize("code_s, fields", [
    ("3697", {"fund": 41}),
    ("3697", {"fund": "36"}),
    ("3697", {"confidence": "S"}),
    ("3697", {"fundamental": 36}),
    ("3697", {"updated_at": "2026-01-01"}),
    ("369", {"fund": 36}),
    ("7203", {"name": "トヨタ", "fund": 30}),  # 新規作成で必須項目が足りない
])
def test_validation_error_writes_nothing(ratings_dir, code_s, fields):
    """検証エラーのとき JSON も履歴もバイト単位で変わらない"""
    before = _snapshot(ratings_dir)
    with pytest.raises(sr.RatingValidationError):
        sr.update_rating(code_s, fields, "誤入力", "cli", ratings_dir=ratings_dir)
    assert _snapshot(ratings_dir) == before


def test_same_value_update_writes_nothing(ratings_dir):
    """値が変わらない更新では JSON も履歴も書かない"""
    before = _snapshot(ratings_dir)
    assert sr.update_rating("3697", {"fund": 35, "name": "SHIFT"}, "再確認", "cli",
                            ratings_dir=ratings_dir) == {}
    assert _snapshot(ratings_dir) == before


HEADER = ["順位", "コード", "銘柄", "ファンダ /40", "未織込 /20", "モメンタム /20",
          "Valuation /20", "総合 /100", "Confidence", "保有", "役割", "投資仮説", "主要リスク",
          "次の格上げ/確認条件", "最終レビュー", "更新メモ", "Status", ""]
ROW_OK = ["1", "6134", "FUJI", "39", "16", "18", "18", "91", "A", "保有", "本命", "仮説",
          "リスク", "条件", "2026-09-23", "四季報反映", "Active", ""]
ROW_WARN = ["2", "6479", "ミネベアミツミ", "36", "16", "7", "17", "70", "A", "B", "", "",
            "", "", "2026-08-20", "2026-08-20", "Watch", "Active"]
ROW_BAD = ["3", "4970", "東洋合成工業", "41", "15", "7", "16", "79", "B", "B", "", "",
           "", "", "2026-09-22", "メモ", "Active", ""]


@pytest.mark.parametrize("rows, expect_error", [
    ([ROW_OK, ROW_WARN], False),
    ([ROW_OK, ROW_WARN, ROW_BAD], True),
])
def test_migrate(tmp_path, rows, expect_error):
    """1行でも検証エラーがあれば何も書かない。怪しい値は警告を出して移行する。再実行は中断する"""
    csv_path = tmp_path / "ledger.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows([HEADER, *rows])
    out_dir = tmp_path / "out"

    if expect_error:
        with pytest.raises(sr.RatingValidationError) as exc_info:
            sr.migrate_from_csv(csv_path, ratings_dir=out_dir)
        assert any("4970" in e and "scores.fund" in e for e in exc_info.value.errors)
        assert not (out_dir / sr.RATINGS_FILENAME).exists()
        assert not (out_dir / sr.HISTORY_FILENAME).exists()
        return

    count, warnings = sr.migrate_from_csv(csv_path, ratings_dir=out_dir)
    assert count == 2
    assert any("6479" in w and "総合" in w for w in warnings)
    assert any("6479" in w and "日付だけ" in w for w in warnings)
    assert any("6479" in w and "右の列" in w for w in warnings)
    fuji = sr.get_rating("6134", ratings_dir=out_dir)
    assert (fuji["total"], fuji["updated_at"], fuji["mispricing_note"], fuji["checkpoints"]) == (
        91, "2026-09-23", "", "条件")
    assert [h["source"] for h in _history(out_dir)] == ["migration", "migration"]
    with pytest.raises(FileExistsError):
        sr.migrate_from_csv(csv_path, ratings_dir=out_dir)


def test_update_regenerates_html(ratings_dir):
    """更新のたびに HTML が作り直され、Status ごとに総合点順で並び、文字列はエスケープされる"""
    sr.update_rating("6134", {"fund": 40, "thesis": "<b>受注</b> & 利益率"}, "反映", "cli",
                     ratings_dir=ratings_dir)
    sr.update_rating("4417", dict(BASE_FIELDS, name="GSX", status="Watch", fund=38),
                     "新規", "cli", ratings_dir=ratings_dir)

    page = (ratings_dir / sr.HTML_FILENAME).read_text(encoding="utf-8")
    assert page.index("<h2>Active") < page.index('href="#s6134"') < page.index('href="#s3697"')
    assert page.index('href="#s3697"') < page.index("<h2>Watch") < page.index('href="#s4417"')
    assert "&lt;b&gt;受注&lt;/b&gt; &amp; 利益率" in page and "<b>受注" not in page


def test_html_failure_does_not_block_update(ratings_dir, monkeypatch):
    """HTML の書き出しに失敗しても、JSON の更新と履歴の記録は成功する"""
    def broken(*args, **kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr(sr, "render_html", broken)
    sr.update_rating("3697", {"fund": 36}, "反映", "cli", ratings_dir=ratings_dir)

    assert sr.get_rating("3697", ratings_dir=ratings_dir)["scores"]["fund"] == 36
    assert _history(ratings_dir)[-1]["changes"] == {"scores.fund": [35, 36]}
