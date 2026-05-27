"""migrate_gyoutai_theme_to_list.py のテスト (issue #187)"""

import pytest

import portfolio_shelve as ps
import migrate_gyoutai_theme_to_list as m


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_portfolio_shelve")


def _set_legacy_gyoutai_theme(code_s: str, value: str, db_path: str) -> None:
    """旧スキーマ (gyoutai_theme: str) を持つレコードを用意するヘルパー。

    update_memo 経由で gyoutai_theme を直接書き込むことで、
    新フィールド gyoutai_themes は [] のまま残る (移行前の状態)。

    issue #282: 移行スクリプトは update_memo で gyoutai_themes を書き込むため、
    旧値を分割した各 name をテーママスターに事前登録しておく。
    """
    ps.add_to_watch(code_s, db_path=db_path)
    ps.update_memo(code_s, {"gyoutai_theme": value}, db_path=db_path)
    # 移行先 list に入る name を先にマスター登録
    for raw in value.split("\n"):
        name = raw.strip()
        if not name:
            continue
        try:
            ps.create_theme(name, db_path=db_path)
        except ValueError:
            pass  # 重複は無視


class TestMigrateGyoutaiThemeToList:

    def test_dry_run_does_not_modify(self, db_path):
        _set_legacy_gyoutai_theme("4377", "半導体\nAI", db_path)
        result = m.migrate_gyoutai_theme_to_list(apply=False, db_path=db_path)
        assert result["total"] == 1
        assert result["converted"] == 1
        assert result["skipped"] == 0

        # shelve は変化なし: gyoutai_theme は元の値、gyoutai_themes は空のまま
        rec = ps.get_record("4377", db_path=db_path)
        assert rec["memo"]["gyoutai_theme"] == "半導体\nAI"
        assert rec["memo"]["gyoutai_themes"] == []

    def test_apply_converts_single_slot(self, db_path):
        _set_legacy_gyoutai_theme("4377", "半導体", db_path)
        result = m.migrate_gyoutai_theme_to_list(apply=True, db_path=db_path)
        assert result["converted"] == 1
        rec = ps.get_record("4377", db_path=db_path)
        assert rec["memo"]["gyoutai_themes"] == ["半導体"]
        assert rec["memo"]["gyoutai_theme"] == ""  # 空文字に更新

    def test_apply_converts_two_slots(self, db_path):
        _set_legacy_gyoutai_theme("4377", "半導体\nAI", db_path)
        m.migrate_gyoutai_theme_to_list(apply=True, db_path=db_path)
        rec = ps.get_record("4377", db_path=db_path)
        assert rec["memo"]["gyoutai_themes"] == ["半導体", "AI"]

    def test_apply_truncates_more_than_max(self, db_path):
        # 3 つ以上 → 先頭 2 件に切り詰め
        _set_legacy_gyoutai_theme("4377", "A\nB\nC\nD", db_path)
        m.migrate_gyoutai_theme_to_list(apply=True, db_path=db_path)
        rec = ps.get_record("4377", db_path=db_path)
        assert rec["memo"]["gyoutai_themes"] == ["A", "B"]

    def test_strips_whitespace_and_skips_empty_lines(self, db_path):
        _set_legacy_gyoutai_theme("4377", "  半導体  \n\n  AI  \n", db_path)
        m.migrate_gyoutai_theme_to_list(apply=True, db_path=db_path)
        rec = ps.get_record("4377", db_path=db_path)
        assert rec["memo"]["gyoutai_themes"] == ["半導体", "AI"]

    def test_skips_record_with_empty_gyoutai_theme(self, db_path):
        # 旧フィールドが空 → スキップ (新フィールドも空のまま)
        ps.add_to_watch("4377", db_path=db_path)
        result = m.migrate_gyoutai_theme_to_list(apply=True, db_path=db_path)
        assert result["converted"] == 0
        assert result["skipped"] == 1

    def test_skips_record_with_existing_gyoutai_themes(self, db_path):
        # 既に gyoutai_themes に値がある → スキップ (上書きしない)
        ps.add_to_watch("4377", db_path=db_path)
        ps.create_theme("新データ", db_path=db_path)  # issue #282: マスター事前登録
        ps.update_memo(
            "4377",
            {"gyoutai_theme": "旧データ", "gyoutai_themes": ["新データ"]},
            db_path=db_path,
        )
        result = m.migrate_gyoutai_theme_to_list(apply=True, db_path=db_path)
        assert result["converted"] == 0
        assert result["skipped"] == 1
        rec = ps.get_record("4377", db_path=db_path)
        assert rec["memo"]["gyoutai_themes"] == ["新データ"]
        # gyoutai_theme は触らない
        assert rec["memo"]["gyoutai_theme"] == "旧データ"

    def test_includes_excluded_records(self, db_path):
        """codex P3 対応: excluded 銘柄も移行対象になる"""
        _set_legacy_gyoutai_theme("4377", "半導体", db_path)
        ps.exclude_from_universe("4377", reason="test", db_path=db_path)
        result = m.migrate_gyoutai_theme_to_list(apply=True, db_path=db_path)
        assert result["total"] == 1
        assert result["converted"] == 1
        rec = ps.get_record("4377", db_path=db_path)
        assert rec["memo"]["gyoutai_themes"] == ["半導体"]

    def test_idempotent(self, db_path):
        """2 回実行しても 2 回目は no-op"""
        _set_legacy_gyoutai_theme("4377", "半導体\nAI", db_path)
        m.migrate_gyoutai_theme_to_list(apply=True, db_path=db_path)
        result = m.migrate_gyoutai_theme_to_list(apply=True, db_path=db_path)
        assert result["converted"] == 0
        assert result["skipped"] == 1

    def test_empty_db(self, db_path):
        result = m.migrate_gyoutai_theme_to_list(apply=True, db_path=db_path)
        assert result == {"total": 0, "converted": 0, "skipped": 0}
