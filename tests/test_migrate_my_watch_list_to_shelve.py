"""migrate_my_watch_list_to_shelve.py のテスト"""

import pytest

import migrate_my_watch_list_to_shelve as mw
import portfolio_shelve as ps


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_portfolio_shelve")


@pytest.fixture
def txt_path(tmp_path):
    p = tmp_path / "my_watch_list.txt"
    return str(p)


def _write_txt(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# ==================================================
# parse_my_watch_list
# ==================================================
class TestParse:

    def test_parse_h_prefix_as_hold(self):
        text = "H7047ポート\nH4377ワンキャリア\n"
        entries = mw.parse_my_watch_list(text)
        assert len(entries) == 2
        assert entries[0] == ("7047", "ポート", "1保")
        assert entries[1] == ("4377", "ワンキャリア", "1保")

    def test_parse_no_prefix_as_watch(self):
        text = "5032AnyColor\n6232ACSL\n"
        entries = mw.parse_my_watch_list(text)
        assert entries[0] == ("5032", "AnyColor", "3監")
        assert entries[1] == ("6232", "ACSL", "3監")

    def test_parse_alpha_code(self):
        text = "H402Aアクセルスペース\n"
        entries = mw.parse_my_watch_list(text)
        assert entries[0] == ("402A", "アクセルスペース", "1保")

    def test_parse_skips_blank_and_invalid_lines(self):
        text = "H7047ポート\n\nINVALID\n5032AnyColor\n"
        entries = mw.parse_my_watch_list(text)
        codes = [e[0] for e in entries]
        assert codes == ["7047", "5032"]

    def test_parse_dedupes_keeping_first(self):
        text = "H7047ポート\n7047重複\n"
        entries = mw.parse_my_watch_list(text)
        assert len(entries) == 1
        assert entries[0] == ("7047", "ポート", "1保")

    def test_parse_real_format(self):
        """実際の my_watch_list.txt と同じフォーマット"""
        text = (
            "H7047ポート\n"
            "H2980SREHD\n"
            "\n"
            "7717ブイ・テクノロジー\n"
            "5032AnyColor\n"
        )
        entries = mw.parse_my_watch_list(text)
        codes = [(e[0], e[2]) for e in entries]
        assert codes == [
            ("7047", "1保"),
            ("2980", "1保"),
            ("7717", "3監"),
            ("5032", "3監"),
        ]


# ==================================================
# merge_into_shelve: txt のみ存在パターン
# ==================================================
class TestMergeTxtOnly:

    def test_creates_new_records(self, db_path):
        entries = [
            ("7047", "ポート", "1保"),
            ("5032", "AnyColor", "3監"),
        ]
        result = mw.merge_into_shelve(entries, db_path=db_path)
        assert result["created"] == 2
        assert result["updated"] == 0
        records = ps.list_records(db_path=db_path)
        assert len(records) == 2

    def test_status_set_correctly_for_h_prefix(self, db_path):
        mw.merge_into_shelve(
            [("7047", "ポート", "1保")], db_path=db_path
        )
        rec = ps.get_record("7047", db_path=db_path)
        assert rec["status"] == "1保"

    def test_action_logs_for_new_hold(self, db_path):
        """1保 で新規追加された場合、初回登録 + ステータス変更の 2 ログ"""
        mw.merge_into_shelve(
            [("7047", "ポート", "1保")], db_path=db_path
        )
        logs = ps.list_action_logs("7047", db_path=db_path)
        assert len(logs) == 2
        assert logs[0]["action_type"] == "初回登録"
        assert logs[0]["status_to"] == "3監"  # ライフサイクル原則
        assert logs[1]["action_type"] == "ステータス変更"
        assert logs[1]["status_from"] == "3監"
        assert logs[1]["status_to"] == "1保"

    def test_action_log_for_new_watch(self, db_path):
        """3監 で新規追加された場合、初回登録ログ 1 件のみ"""
        mw.merge_into_shelve(
            [("5032", "AnyColor", "3監")], db_path=db_path
        )
        logs = ps.list_action_logs("5032", db_path=db_path)
        assert len(logs) == 1
        assert logs[0]["action_type"] == "初回登録"
        assert logs[0]["status_to"] == "3監"


# ==================================================
# merge_into_shelve: 既存レコード (スプシ移行済み) 上書き
# ==================================================
class TestMergeWithExisting:

    def test_overwrites_status_keeps_memo(self, db_path):
        """既存レコード (スプシ由来、メモあり) のステータスを txt で上書き"""
        # 先にスプシ由来レコードを upsert (ステータスは仮で "3監")
        memo = ps.create_memo(gyoutai_theme="人材", trade_idea="押し目")
        existing = ps.create_record(
            "7047", "ポート (スプシ名)", status="3監", memo=memo,
        )
        ps.upsert_record(existing, db_path=db_path)

        # txt 取り込み (1保 として上書き)
        mw.merge_into_shelve(
            [("7047", "ポート (txt名)", "1保")], db_path=db_path
        )

        rec = ps.get_record("7047", db_path=db_path)
        assert rec["status"] == "1保"  # txt のステータスで上書きされた
        # メモは保持
        assert rec["memo"]["gyoutai_theme"] == "人材"
        assert rec["memo"]["trade_idea"] == "押し目"
        # stock_name はスプシ由来を保持 (上書きしない)
        assert rec["stock_name"] == "ポート (スプシ名)"

    def test_no_change_when_status_matches(self, db_path):
        existing = ps.create_record("5032", "AnyColor", status="3監")
        ps.upsert_record(existing, db_path=db_path)

        result = mw.merge_into_shelve(
            [("5032", "AnyColor", "3監")], db_path=db_path
        )
        assert result["unchanged"] == 1
        assert result["updated"] == 0
        # アクションログは増えない
        logs = ps.list_action_logs("5032", db_path=db_path)
        assert logs == []

    def test_records_status_change_log(self, db_path):
        """既存ステータスからの変更ログが記録される"""
        existing = ps.create_record("7047", "ポート", status="3監")
        ps.upsert_record(existing, db_path=db_path)

        mw.merge_into_shelve(
            [("7047", "ポート", "1保")], db_path=db_path
        )

        logs = ps.list_action_logs("7047", db_path=db_path)
        assert len(logs) == 1
        assert logs[0]["action_type"] == "ステータス変更"
        assert logs[0]["status_from"] == "3監"
        assert logs[0]["status_to"] == "1保"


# ==================================================
# import_my_watch_list (実行層)
# ==================================================
class TestImport:

    def test_import_from_file(self, txt_path, db_path):
        _write_txt(txt_path, "H7047ポート\n5032AnyColor\n")
        result = mw.import_my_watch_list(txt_path, db_path=db_path)
        assert result["created"] == 2
        records = ps.list_records(db_path=db_path)
        codes = [r["code_s"] for r in records]
        assert codes == ["5032", "7047"]
        statuses = {r["code_s"]: r["status"] for r in records}
        assert statuses == {"7047": "1保", "5032": "3監"}

    def test_import_missing_file(self, tmp_path, db_path):
        with pytest.raises(FileNotFoundError):
            mw.import_my_watch_list(
                str(tmp_path / "missing.txt"), db_path=db_path
            )

    def test_idempotent_rerun(self, txt_path, db_path):
        """同じ txt を 2 回流しても二重登録にならない"""
        _write_txt(txt_path, "H7047ポート\n5032AnyColor\n")
        mw.import_my_watch_list(txt_path, db_path=db_path)
        result = mw.import_my_watch_list(txt_path, db_path=db_path)
        assert result["created"] == 0
        assert result["updated"] == 0
        assert result["unchanged"] == 2
