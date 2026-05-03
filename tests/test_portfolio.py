"""portfolio.py の parse_my_portforio() 互換性テスト。

§3-4-1 Step 1: txt のみを取り込んだ portfolio_shelve に対し、
新実装 (shelve 参照) と旧実装 (txt 直接パース) で **集合一致** を保証する。
"""

import os

import pytest

import portfolio
import portfolio_shelve as ps
import migrate_my_watch_list_to_shelve as mw


# ==================================================
# fixtures
# ==================================================

@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """portfolio.py が参照する DATA_DIR を一時ディレクトリに差し替える。

    portfolio_shelve のパスも tmp_path 配下に変更し、テスト同士が干渉しないようにする。
    """
    monkeypatch.setattr(portfolio, "DATA_DIR", str(tmp_path))
    # portfolio_shelve のグローバルパスを差し替えてテスト用 DB を使う
    test_db_path = str(tmp_path / "portfolio_shelve")
    monkeypatch.setattr(ps, "PORTFOLIO_SHELVE", test_db_path)
    return tmp_path


def _write_txt(tmp_path, content):
    txt = tmp_path / "my_watch_list.txt"
    with open(txt, "w", encoding="utf-8") as f:
        f.write(content)
    return str(txt)


# ==================================================
# フォールバック: shelve 空 / 障害時に txt 経由で動く
# ==================================================
class TestFallbackToTxt:

    def test_falls_back_to_txt_when_shelve_empty(self, isolated_data_dir):
        """portfolio_shelve が空なら旧実装 (txt) を使う"""
        _write_txt(isolated_data_dir, "H7047ポート\n5032AnyColor\n")
        watch, hold = portfolio.parse_my_portforio()
        assert "7047" in hold
        assert "5032" in watch

    def test_uses_shelve_when_populated(self, isolated_data_dir):
        """shelve に登録があれば shelve を使う (txt は無関係)"""
        # shelve に書き込み
        ps.add_to_watch("7047", "ポート")
        ps.transition_status("7047", "1保")
        # txt とは別の銘柄
        _write_txt(isolated_data_dir, "H9999別銘柄\n")
        watch, hold = portfolio.parse_my_portforio()
        # shelve 由来のみ返る、txt の 9999 は無視
        assert hold == ["7047"]
        assert watch == []


# ==================================================
# Step 1 互換性: txt のみ取り込み → 集合一致
# ==================================================
class TestStep1CompatibilityWithTxtOnly:
    """§3-4-1 Step 1: portfolio_shelve に txt のみを取り込んだ状態で、
    旧実装 (txt 直接) と新実装 (shelve 参照) の戻り値の **集合が一致** することを確認。
    """

    def test_basic_compatibility(self, isolated_data_dir):
        txt_content = (
            "H7047ポート\n"
            "H4377ワンキャリア\n"
            "H402Aアクセルスペース\n"
            "5032AnyColor\n"
            "6232ACSL\n"
            "5243note\n"
        )
        txt_path = _write_txt(isolated_data_dir, txt_content)

        # 旧実装相当 (txt 直接 parse)
        old_watch, old_hold = portfolio._parse_my_portforio_from_txt()

        # txt を portfolio_shelve に取り込み
        mw.import_my_watch_list(txt_path)

        # 新実装 (shelve 参照)
        new_watch, new_hold = portfolio.parse_my_portforio()

        # 集合が完全一致
        assert set(old_watch) == set(new_watch)
        assert set(old_hold) == set(new_hold)

    def test_empty_txt(self, isolated_data_dir):
        _write_txt(isolated_data_dir, "")
        old_watch, old_hold = portfolio._parse_my_portforio_from_txt()
        new_watch, new_hold = portfolio.parse_my_portforio()
        # 旧 (txt 空) と新 (shelve 空 → txt フォールバック → 空) で一致
        assert old_watch == new_watch == []
        assert old_hold == new_hold == []

    def test_only_holds(self, isolated_data_dir):
        txt_path = _write_txt(
            isolated_data_dir, "H7047ポート\nH4377ワンキャリア\n"
        )
        mw.import_my_watch_list(txt_path)
        watch, hold = portfolio.parse_my_portforio()
        assert set(watch) == set()
        assert set(hold) == {"7047", "4377"}

    def test_only_watch(self, isolated_data_dir):
        txt_path = _write_txt(
            isolated_data_dir, "5032AnyColor\n6232ACSL\n"
        )
        mw.import_my_watch_list(txt_path)
        watch, hold = portfolio.parse_my_portforio()
        assert set(watch) == {"5032", "6232"}
        assert set(hold) == set()


# ==================================================
# 戻り値の型・順序保証
# ==================================================
class TestReturnTypeAndOrder:

    def test_returns_two_lists(self, isolated_data_dir):
        ps.add_to_watch("7047", "ポート")
        watch, hold = portfolio.parse_my_portforio()
        assert isinstance(watch, list)
        assert isinstance(hold, list)

    def test_sorted_by_code_s(self, isolated_data_dir):
        for code in ["7089", "4377", "215A"]:
            ps.add_to_watch(code, code)
        for code in ["7047", "2980", "402A"]:
            ps.add_to_watch(code, code)
            ps.transition_status(code, "1保")

        watch, hold = portfolio.parse_my_portforio()
        # code_s 昇順 (決定論的)
        assert watch == sorted(watch)
        assert hold == sorted(hold)

    def test_2jun_treated_as_watch(self, isolated_data_dir):
        """2準 はウォッチ側に含まれる (txt には 2準 がないが、shelve 経由では発生する)"""
        ps.add_to_watch("7047", "ポート")
        ps.transition_status("7047", "2準")
        watch, hold = portfolio.parse_my_portforio()
        assert "7047" in watch
        assert "7047" not in hold


# ==================================================
# Step 2 互換性: 集合関係の確認
# ==================================================
class TestStep2SetRelation:
    """§3-4-1 Step 2: スプシ移行後、新 parse は旧 parse の集合を **上回る** ことを確認。
    増分はスプシ由来銘柄のみ。
    """

    def test_new_set_is_superset(self, isolated_data_dir):
        """txt + スプシ追加銘柄を取り込んだ shelve は txt のみ集合を包含する"""
        # txt 由来
        txt_path = _write_txt(
            isolated_data_dir, "H7047ポート\n5032AnyColor\n"
        )
        mw.import_my_watch_list(txt_path)
        # txt のみ集合 (旧実装相当)
        old_watch, old_hold = portfolio._parse_my_portforio_from_txt()
        old_set = set(old_watch) | set(old_hold)

        # スプシのみの銘柄を追加
        ps.add_to_watch("4377", "スプシ追加銘柄")  # 3監

        # 新 parse の集合
        new_watch, new_hold = portfolio.parse_my_portforio()
        new_set = set(new_watch) | set(new_hold)

        # 旧 ⊆ 新
        assert old_set <= new_set
        # 増分はスプシ由来 (4377) のみ
        assert new_set - old_set == {"4377"}
