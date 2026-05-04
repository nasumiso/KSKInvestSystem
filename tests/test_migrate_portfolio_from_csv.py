"""migrate_portfolio_from_csv.py のテスト"""

import csv

import pytest

import migrate_portfolio_from_csv as mp
import portfolio_shelve as ps


# ==================================================
# fixtures
# ==================================================

@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_portfolio_shelve")


def _make_row(
    *,
    code_s: str = "",
    stock_name: str = "",
    gyoutai: str = "",
    watch_reason: str = "",
    inago: str = "",
    trade_idea: str = "",
    takaichi: str = "",
) -> list:
    """36 列の行を組み立てる。指標系列は空文字で埋める。"""
    row = [""] * mp.EXPECTED_COL_COUNT
    row[mp.COL_CODE_S] = code_s
    row[mp.COL_STOCK_NAME] = stock_name
    row[mp.COL_GYOUTAI] = gyoutai
    row[mp.COL_WATCH_REASON] = watch_reason
    row[mp.COL_INAGO] = inago
    row[mp.COL_TRADE_IDEA] = trade_idea
    row[mp.COL_TAKAICHI] = takaichi
    return row


@pytest.fixture
def sample_csv(tmp_path):
    """先頭空行 + ヘッダ + 3 行データの CSV を作成。"""
    csv_path = tmp_path / "portfolio.csv"
    rows = [
        [""] * mp.EXPECTED_COL_COUNT,  # 1 行目: 空行
        # 2 行目: ヘッダ
        [
            "銘柄コード", "銘柄名", "保有リスト", "業態・テーマ",
            *([""] * 27),
            "ウォッチ・IN理由",
            "需給チャート",
            "イナゴ元・きっかけ",
            "投資売買アイデア",
            "高市感応度",
        ],
        _make_row(
            code_s="4377",
            stock_name="ワンキャリア",
            gyoutai="人材\n少子高齢",
            watch_reason="新卒シェアトップ",
            trade_idea="押し目買い",
            inago="ケイ",
            takaichi="C:給付付き税額控除",
        ),
        _make_row(
            code_s="7089",
            stock_name="フォースタートアップス",
            gyoutai="人材",
            takaichi="B:成長産業への積極投資",
        ),
        _make_row(
            code_s="215A",
            stock_name="アクセルスペース",
        ),
    ]
    # ヘッダ行の長さ調整
    if len(rows[1]) < mp.EXPECTED_COL_COUNT:
        rows[1] += [""] * (mp.EXPECTED_COL_COUNT - len(rows[1]))
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)
    return str(csv_path)


# ==================================================
# 読込層
# ==================================================
class TestReadCsvRows:

    def test_skips_empty_first_row_and_header(self, sample_csv):
        rows = mp.read_csv_rows(sample_csv)
        assert len(rows) == 3
        assert rows[0][mp.COL_CODE_S] == "4377"
        assert rows[1][mp.COL_CODE_S] == "7089"
        assert rows[2][mp.COL_CODE_S] == "215A"

    def test_pads_short_rows(self, tmp_path):
        csv_path = tmp_path / "short.csv"
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow([""] * mp.EXPECTED_COL_COUNT)  # 空行
            w.writerow(["銘柄コード", "銘柄名"])     # ヘッダ
            w.writerow(["4377", "ワンキャリア"])     # データ (2 列のみ)
        rows = mp.read_csv_rows(str(csv_path))
        assert len(rows) == 1
        assert len(rows[0]) == mp.EXPECTED_COL_COUNT
        assert rows[0][mp.COL_CODE_S] == "4377"


# ==================================================
# 列パース層
# ==================================================
class TestParseMemoColumns:

    def test_extracts_5_memo_fields(self):
        row = _make_row(
            gyoutai="人材",
            watch_reason="新卒",
            trade_idea="押し目",
            inago="ケイ",
            takaichi="C",
        )
        memo, warnings = mp.parse_memo_columns(row)
        assert memo["gyoutai_theme"] == "人材"
        assert memo["watch_in_reason"] == "新卒"
        assert memo["trade_idea"] == "押し目"
        assert memo["inago_origin"] == "ケイ"
        assert memo["takaichi_sensitivity"] == "C"
        assert warnings == []

    def test_strips_whitespace(self):
        row = _make_row(gyoutai="  人材  ")
        memo, _ = mp.parse_memo_columns(row)
        assert memo["gyoutai_theme"] == "人材"

    def test_handles_short_row(self):
        memo, warnings = mp.parse_memo_columns(["4377", "x"])
        assert memo == ps.create_memo()
        assert warnings  # 列数不足の警告あり


# ==================================================
# 統合層
# ==================================================
class TestBuildRecordFromRow:

    def test_minimal_row(self):
        row = _make_row(code_s="4377", stock_name="ワンキャリア")
        rec, warnings = mp.build_record_from_row(row)
        assert rec is not None
        assert rec["code_s"] == "4377"
        assert "stock_name" not in rec  # 新スキーマでは保存しない
        assert rec["status"] == "3監"  # 仮ステータス
        assert warnings == []

    def test_full_row(self):
        row = _make_row(
            code_s="4377",
            stock_name="ワンキャリア",
            gyoutai="人材",
            takaichi="C",
        )
        rec, _ = mp.build_record_from_row(row)
        assert rec["memo"]["gyoutai_theme"] == "人材"
        assert rec["memo"]["takaichi_sensitivity"] == "C"

    def test_empty_code_returns_none(self):
        row = _make_row(stock_name="名前あり")
        rec, warnings = mp.build_record_from_row(row)
        assert rec is None
        assert any("空の銘柄コード" in w for w in warnings)

    def test_invalid_code_returns_none(self):
        row = _make_row(code_s="ABC1", stock_name="不正コード")
        rec, warnings = mp.build_record_from_row(row)
        assert rec is None
        assert any("不正な銘柄コード" in w for w in warnings)

    def test_lowercase_code_normalized(self):
        row = _make_row(code_s="215a", stock_name="テスト")
        rec, warnings = mp.build_record_from_row(row)
        assert rec is not None
        assert rec["code_s"] == "215A"

    def test_empty_stock_name_warns_but_succeeds(self):
        row = _make_row(code_s="4377")
        rec, warnings = mp.build_record_from_row(row)
        assert rec is not None
        assert "stock_name" not in rec  # 新スキーマでは保存しない
        assert any("銘柄名" in w for w in warnings)


# ==================================================
# 実行層
# ==================================================
class TestMigrate:

    def test_dry_run_does_not_write(self, sample_csv, db_path):
        result = mp.migrate_csv_to_portfolio_shelve(
            sample_csv, dry_run=True, db_path=db_path
        )
        assert result["total"] == 3
        assert result["saved"] == 3
        assert result["skipped"] == 0
        # DB は空のまま
        records = ps.list_records(db_path=db_path)
        assert records == []

    def test_writes_records(self, sample_csv, db_path):
        result = mp.migrate_csv_to_portfolio_shelve(
            sample_csv, dry_run=False, db_path=db_path
        )
        assert result["saved"] == 3
        records = ps.list_records(db_path=db_path)
        codes = sorted(r["code_s"] for r in records)
        assert codes == ["215A", "4377", "7089"]

    def test_records_initial_log(self, sample_csv, db_path):
        mp.migrate_csv_to_portfolio_shelve(sample_csv, db_path=db_path)
        for code in ["4377", "7089", "215A"]:
            logs = ps.list_action_logs(code, db_path=db_path)
            assert len(logs) == 1
            assert logs[0]["action_type"] == "初回登録"
            assert logs[0]["status_to"] == "3監"
            assert logs[0]["reason"] == "スプシ移行"

    def test_memo_preserves_multiline(self, sample_csv, db_path):
        mp.migrate_csv_to_portfolio_shelve(sample_csv, db_path=db_path)
        rec = ps.get_record("4377", db_path=db_path)
        assert "\n" in rec["memo"]["gyoutai_theme"]
        assert rec["memo"]["takaichi_sensitivity"] == "C:給付付き税額控除"

    def test_skips_invalid_rows(self, tmp_path, db_path):
        csv_path = tmp_path / "with_invalid.csv"
        rows = [
            [""] * mp.EXPECTED_COL_COUNT,
            ["銘柄コード"] + [""] * (mp.EXPECTED_COL_COUNT - 1),
            _make_row(code_s="4377", stock_name="OK"),
            _make_row(code_s="ABC1", stock_name="不正コード"),
            _make_row(stock_name="コード空"),
            _make_row(code_s="7089", stock_name="OK2"),
        ]
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(rows)
        result = mp.migrate_csv_to_portfolio_shelve(
            str(csv_path), db_path=db_path
        )
        assert result["saved"] == 2
        assert result["skipped"] == 2

    def test_rerun_does_not_duplicate_log(self, sample_csv, db_path):
        """再実行しても 初回登録 ログは増えない (既存判定)"""
        mp.migrate_csv_to_portfolio_shelve(sample_csv, db_path=db_path)
        mp.migrate_csv_to_portfolio_shelve(sample_csv, db_path=db_path)
        logs = ps.list_action_logs("4377", db_path=db_path)
        assert len(logs) == 1
