"""portfolio.py の parse_my_portforio() テスト。

portfolio_shelve を真実源とし、空・障害時のみ my_watch_list.txt に
フォールバックする分岐と、戻り値 (ステータス分類・code_s 昇順) を検証する。
"""

import pytest

import portfolio
import portfolio_shelve as ps


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """portfolio.py が参照する DATA_DIR と portfolio_shelve を一時領域に差し替える。"""
    monkeypatch.setattr(portfolio, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ps, "PORTFOLIO_SHELVE", str(tmp_path / "portfolio_shelve"))
    return tmp_path


def _write_txt(tmp_path, content):
    txt = tmp_path / "my_watch_list.txt"
    with open(txt, "w", encoding="utf-8") as f:
        f.write(content)
    return str(txt)


class TestFallbackToTxt:
    """shelve が空のときのみ txt を読む (移行前・shelve 障害時の保険)"""

    def test_falls_back_to_txt_when_shelve_empty(self, isolated_data_dir):
        _write_txt(isolated_data_dir, "H7047ポート\n5032AnyColor\n")
        watch, hold = portfolio.parse_my_portforio()
        assert "7047" in hold
        assert "5032" in watch

    def test_uses_shelve_when_populated(self, isolated_data_dir):
        """shelve に登録があれば txt は無視される"""
        ps.add_to_watch("7047")
        ps.transition_status("7047", "1保")
        _write_txt(isolated_data_dir, "H9999別銘柄\n")
        watch, hold = portfolio.parse_my_portforio()
        assert hold == ["7047"]
        assert watch == []


class TestReturnValue:

    @pytest.mark.parametrize("status,in_watch", [
        ("1保", False),   # 保有 → hold 側
        ("2準", True),    # 2準/3監 → watch 側
        ("3監", True),
    ])
    def test_status_classification(self, isolated_data_dir, status, in_watch):
        ps.add_to_watch("7047")
        ps.transition_status("7047", status)
        watch, hold = portfolio.parse_my_portforio()
        assert ("7047" in watch) is in_watch
        assert ("7047" in hold) is not in_watch

    def test_sorted_by_code_s(self, isolated_data_dir):
        """両リストとも code_s 昇順 (決定論的)"""
        for code in ["7089", "4377", "215A"]:
            ps.add_to_watch(code)
        for code in ["7047", "2980", "402A"]:
            ps.add_to_watch(code)
            ps.transition_status(code, "1保")

        watch, hold = portfolio.parse_my_portforio()
        assert watch == sorted(watch)
        assert hold == sorted(hold)
