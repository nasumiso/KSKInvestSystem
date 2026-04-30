"""pts_data.py のユニットテスト。

- get_pts_csv_path_for_date: pts_YYMMDD.csv 形式
- load_pts_changes_for_date:
  - 日付一致時のみ dict 返却
  - 指定日 CSV 不在で空 dict
  - "+2.5%" → "+2.5" の % 除去
  - "-3.0%" の符号保持
  - 数値パース不能/空欄はスキップ
"""

import os
from datetime import date

import pytest

import pts_data


@pytest.fixture
def isolated_data_dir(monkeypatch, tmp_path):
    """pts_data の DATA_DIR を tmp_path に差し替え、today_stocks/ を作る"""
    today_stocks = tmp_path / "today_stocks"
    today_stocks.mkdir()
    monkeypatch.setattr(pts_data, "DATA_DIR", str(tmp_path))
    return tmp_path


def _write_pts_csv(data_dir, dt, rows):
    """pts_YYMMDD.csv をテスト用に書き出す。rows は list[list[str]]"""
    fname = "pts_%02d%02d%02d.csv" % (dt.year - 2000, dt.month, dt.day)
    path = data_dir / "today_stocks" / fname
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(",".join(row) + "\n")
    return path


class TestGetPtsCsvPathForDate:
    """get_pts_csv_path_for_date のテスト"""

    def test_basic_naming(self, isolated_data_dir):
        path = pts_data.get_pts_csv_path_for_date(date(2026, 4, 30))
        assert path.endswith(os.path.join("today_stocks", "pts_260430.csv"))

    def test_zero_padded_month_day(self, isolated_data_dir):
        path = pts_data.get_pts_csv_path_for_date(date(2026, 1, 5))
        assert path.endswith("pts_260105.csv")

    def test_rejects_non_date(self, isolated_data_dir):
        with pytest.raises(TypeError):
            pts_data.get_pts_csv_path_for_date("2026-04-30")

    def test_does_not_check_existence(self, isolated_data_dir):
        # 存在しなくてもパスは返す
        path = pts_data.get_pts_csv_path_for_date(date(2030, 1, 1))
        assert "pts_300101.csv" in path


class TestLoadPtsChangesForDate:
    """load_pts_changes_for_date のテスト"""

    def test_empty_dict_when_csv_missing(self, isolated_data_dir):
        # CSV が無い日付を指定 → 空 dict
        result = pts_data.load_pts_changes_for_date(date(2030, 1, 1))
        assert result == {}

    def test_strips_percent_keeps_sign(self, isolated_data_dir):
        # "+2.5%" → "+2.5", "-3.0%" → "-3.0"
        target = date(2026, 4, 30)
        _write_pts_csv(
            isolated_data_dir, target,
            [
                ["1", "6501 日立", "東P", "電機", "1531", "+300", "+2.5%", "16800"],
                ["2", "7203 トヨタ", "東P", "輸送機", "2900", "-90", "-3.0%", "10000"],
            ],
        )
        result = pts_data.load_pts_changes_for_date(target)
        assert result == {"6501": "+2.5", "7203": "-3.0"}

    def test_skips_invalid_or_empty(self, isolated_data_dir):
        target = date(2026, 4, 30)
        _write_pts_csv(
            isolated_data_dir, target,
            [
                ["1", "6501 日立", "東P", "電機", "1531", "+300", "+2.5%", "16800"],
                ["2", "9999 テスト", "東G", "情報", "100", "0", "", "1000"],   # 空
                ["3", "0001 不正", "東G", "情報", "100", "0", "abc%", "1000"],  # 数値不能
            ],
        )
        result = pts_data.load_pts_changes_for_date(target)
        assert result == {"6501": "+2.5"}

    def test_no_fallback_to_latest_csv(self, isolated_data_dir):
        """日付一致が必須。古い CSV が残っていても指定日の CSV が無ければ空 dict。"""
        # 1 日前の CSV を作るが、当日の CSV は作らない
        yesterday = date(2026, 4, 29)
        _write_pts_csv(
            isolated_data_dir, yesterday,
            [["1", "6501 日立", "東P", "電機", "1531", "+300", "+2.5%", "16800"]],
        )
        # 当日を指定 → 空 dict (フォールバックしない)
        today = date(2026, 4, 30)
        result = pts_data.load_pts_changes_for_date(today)
        assert result == {}

    def test_handles_missing_columns_gracefully(self, isolated_data_dir):
        target = date(2026, 4, 30)
        _write_pts_csv(
            isolated_data_dir, target,
            [
                ["1", "6501 日立", "東P", "電機"],  # カラム不足
                ["2", "7203 トヨタ", "東P", "輸送機", "2900", "-90", "-1.5%", "10000"],
            ],
        )
        result = pts_data.load_pts_changes_for_date(target)
        assert result == {"7203": "-1.5"}

    def test_handles_code_field_with_only_code(self, isolated_data_dir):
        """code+name の name が無くても code_s だけは取れる"""
        target = date(2026, 4, 30)
        _write_pts_csv(
            isolated_data_dir, target,
            [["1", "215A", "東G", "情報", "1000", "+50", "+5.0%", "1000"]],
        )
        result = pts_data.load_pts_changes_for_date(target)
        assert result == {"215A": "+5.0"}
