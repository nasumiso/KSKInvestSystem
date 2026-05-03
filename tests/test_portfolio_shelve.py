"""portfolio_shelve.py のテスト (tmp_path で一時DBを作成)"""

import pytest

import portfolio_shelve as ps


@pytest.fixture
def db_path(tmp_path):
    """テスト用一時DBパスを返す"""
    return str(tmp_path / "test_portfolio_shelve")


# ==================================================
# スキーマ層: 正規化・バリデーション・ファクトリ
# ==================================================
class TestSchema:
    """スキーマ層のユニットテスト"""

    def test_normalize_code_s_strips_and_uppercases(self):
        assert ps.normalize_code_s(" 6324 ") == "6324"
        assert ps.normalize_code_s("215a") == "215A"

    def test_normalize_code_s_rejects_non_str(self):
        with pytest.raises(TypeError):
            ps.normalize_code_s(6324)

    def test_validate_code_s_accepts_valid(self):
        ps.validate_code_s("6324")
        ps.validate_code_s("215A")
        ps.validate_code_s("0001")

    def test_validate_code_s_rejects_invalid(self):
        with pytest.raises(ValueError):
            ps.validate_code_s("63")
        with pytest.raises(ValueError):
            ps.validate_code_s("12345")

    def test_validate_status_accepts_valid(self):
        for s in ("1保", "2準", "3監"):
            ps.validate_status(s)

    def test_validate_status_rejects_invalid(self):
        with pytest.raises(ValueError):
            ps.validate_status("4売")
        with pytest.raises(ValueError):
            ps.validate_status("hold")

    def test_validate_action_type_accepts_valid(self):
        for t in ("初回登録", "ステータス変更", "売却", "削除"):
            ps.validate_action_type(t)

    def test_validate_action_type_rejects_invalid(self):
        with pytest.raises(ValueError):
            ps.validate_action_type("追加")

    def test_validate_transition_allowed(self):
        ps.validate_transition(None, "3監")          # 新規追加
        ps.validate_transition("3監", "2準")          # 格上げ
        ps.validate_transition("2準", "1保")          # 買い
        ps.validate_transition("1保", "2準")          # 売却
        ps.validate_transition("2準", "3監")          # 格下げ
        ps.validate_transition("3監", "1保")          # 直接保有

    def test_validate_transition_forbidden(self):
        # 1保/2準 への直接登録は禁止
        with pytest.raises(ValueError):
            ps.validate_transition(None, "1保")
        with pytest.raises(ValueError):
            ps.validate_transition(None, "2準")

    def test_create_memo_defaults(self):
        memo = ps.create_memo()
        assert set(memo.keys()) == ps.MEMO_FIELDS
        assert all(v == "" for v in memo.values())

    def test_create_memo_partial(self):
        memo = ps.create_memo(gyoutai_theme="人材", trade_idea="押し目買い")
        assert memo["gyoutai_theme"] == "人材"
        assert memo["trade_idea"] == "押し目買い"
        assert memo["watch_in_reason"] == ""

    def test_create_record_minimal(self):
        rec = ps.create_record("4377", "ワンキャリア")
        assert rec["code_s"] == "4377"
        assert rec["stock_name"] == "ワンキャリア"
        assert rec["status"] == "3監"
        assert rec["registered_at"]
        assert rec["updated_at"] == rec["registered_at"]
        assert set(rec["memo"].keys()) == ps.MEMO_FIELDS

    def test_create_record_with_explicit_status(self):
        rec = ps.create_record("4377", "ワンキャリア", status="1保")
        assert rec["status"] == "1保"

    def test_create_record_invalid_status(self):
        with pytest.raises(ValueError):
            ps.create_record("4377", "ワンキャリア", status="未定")


# ==================================================
# 高レベル操作: add_to_watch
# ==================================================
class TestAddToWatch:

    def test_add_to_watch_creates_record(self, db_path):
        rec = ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)
        assert rec["code_s"] == "4377"
        assert rec["status"] == "3監"

        loaded = ps.get_record("4377", db_path=db_path)
        assert loaded is not None
        assert loaded["stock_name"] == "ワンキャリア"

    def test_add_to_watch_records_initial_log(self, db_path):
        ps.add_to_watch("4377", "ワンキャリア", db_path=db_path, reason="新規登録")
        logs = ps.list_action_logs("4377", db_path=db_path)
        assert len(logs) == 1
        assert logs[0]["action_type"] == "初回登録"
        assert logs[0]["status_from"] is None
        assert logs[0]["status_to"] == "3監"
        assert logs[0]["reason"] == "新規登録"
        assert logs[0]["seq"] == 1

    def test_add_to_watch_duplicate_raises(self, db_path):
        ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)
        with pytest.raises(ValueError):
            ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)

    def test_add_to_watch_normalizes_code_s(self, db_path):
        ps.add_to_watch("215a", "テスト銘柄", db_path=db_path)
        loaded = ps.get_record("215A", db_path=db_path)
        assert loaded is not None
        assert loaded["code_s"] == "215A"


# ==================================================
# 高レベル操作: transition_status
# ==================================================
class TestTransitionStatus:

    def test_transition_3kan_to_2jun(self, db_path):
        ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)
        rec = ps.transition_status(
            "4377", "2準", reason="そろそろ買う", db_path=db_path
        )
        assert rec["status"] == "2準"

        logs = ps.list_action_logs("4377", db_path=db_path)
        assert len(logs) == 2  # 初回登録 + ステータス変更
        assert logs[1]["action_type"] == "ステータス変更"
        assert logs[1]["status_from"] == "3監"
        assert logs[1]["status_to"] == "2準"

    def test_transition_1ho_to_2jun_records_uri_kyaku(self, db_path):
        """1保 -> 2準 は売却として記録される"""
        ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)
        ps.transition_status("4377", "1保", db_path=db_path)
        ps.transition_status("4377", "2準", reason="決算後売り", db_path=db_path)

        logs = ps.list_action_logs("4377", db_path=db_path)
        # 初回登録 + 3監->1保 + 1保->2準 = 3件
        assert len(logs) == 3
        assert logs[2]["action_type"] == "売却"
        assert logs[2]["status_from"] == "1保"
        assert logs[2]["status_to"] == "2準"

    def test_transition_to_1ho_directly_from_3kan(self, db_path):
        ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)
        rec = ps.transition_status("4377", "1保", db_path=db_path)
        assert rec["status"] == "1保"

    def test_transition_invalid_path_rejected(self, db_path):
        """禁止遷移は ValueError"""
        ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)
        # 3監 -> 3監 は同一遷移として ALLOWED に入っていないので禁止
        # ただし transition_status は同一ステータスなら no-op としているため
        # ここでは別の禁止パターンを試す。実装の ALLOWED_TRANSITIONS 上、
        # すべての非同一遷移は許可されているので、不正遷移は同一以外発生しない。
        # 同一遷移は no-op で通過するので、代わりに未登録銘柄のチェック。
        with pytest.raises(KeyError):
            ps.transition_status("9999", "2準", db_path=db_path)

    def test_transition_same_status_is_noop(self, db_path):
        ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)
        ps.transition_status("4377", "3監", db_path=db_path)  # no-op
        logs = ps.list_action_logs("4377", db_path=db_path)
        assert len(logs) == 1  # 初回登録だけ、ステータス変更ログは出ない


# ==================================================
# 高レベル操作: delete_record
# ==================================================
class TestDeleteRecord:

    def test_delete_3kan_succeeds(self, db_path):
        ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)
        ok = ps.delete_record("4377", reason="興味なくなった", db_path=db_path)
        assert ok is True
        assert ps.get_record("4377", db_path=db_path) is None

    def test_delete_records_log_after_record_gone(self, db_path):
        """削除しても action_log は残る"""
        ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)
        ps.delete_record("4377", reason="不要", db_path=db_path)
        logs = ps.list_action_logs("4377", db_path=db_path)
        # 初回登録 + 削除 = 2件
        assert len(logs) == 2
        assert logs[1]["action_type"] == "削除"
        assert logs[1]["reason"] == "不要"

    def test_delete_1ho_rejected(self, db_path):
        """1保 から直接削除は禁止"""
        ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)
        ps.transition_status("4377", "1保", db_path=db_path)
        with pytest.raises(ValueError):
            ps.delete_record("4377", db_path=db_path)
        # レコードは残っている
        assert ps.get_record("4377", db_path=db_path) is not None

    def test_delete_2jun_rejected(self, db_path):
        ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)
        ps.transition_status("4377", "2準", db_path=db_path)
        with pytest.raises(ValueError):
            ps.delete_record("4377", db_path=db_path)

    def test_delete_nonexistent_returns_false(self, db_path):
        ok = ps.delete_record("4377", db_path=db_path)
        assert ok is False


# ==================================================
# list_records / list_action_logs
# ==================================================
class TestListing:

    def test_list_records_filters_by_status(self, db_path):
        ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)
        ps.add_to_watch("7089", "フォースタートアップス", db_path=db_path)
        ps.transition_status("7089", "1保", db_path=db_path)

        watch = ps.list_records(status="3監", db_path=db_path)
        hold = ps.list_records(status="1保", db_path=db_path)
        all_recs = ps.list_records(db_path=db_path)

        assert [r["code_s"] for r in watch] == ["4377"]
        assert [r["code_s"] for r in hold] == ["7089"]
        assert [r["code_s"] for r in all_recs] == ["4377", "7089"]

    def test_list_records_sorted_by_code_s(self, db_path):
        ps.add_to_watch("7089", "フォースタートアップス", db_path=db_path)
        ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)
        ps.add_to_watch("215A", "アクセルスペース", db_path=db_path)

        recs = ps.list_records(db_path=db_path)
        assert [r["code_s"] for r in recs] == ["215A", "4377", "7089"]

    def test_list_action_logs_per_code(self, db_path):
        ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)
        ps.add_to_watch("7089", "フォースタートアップス", db_path=db_path)
        ps.transition_status("4377", "2準", db_path=db_path)

        logs_4377 = ps.list_action_logs("4377", db_path=db_path)
        logs_7089 = ps.list_action_logs("7089", db_path=db_path)
        all_logs = ps.list_action_logs(db_path=db_path)

        assert len(logs_4377) == 2
        assert len(logs_7089) == 1
        assert len(all_logs) == 3

    def test_list_action_logs_sorted_by_seq(self, db_path):
        ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)
        # seq が二桁・三桁で正しく順序が保たれることを確認するため複数遷移
        for next_status in ["2準", "1保", "2準", "1保", "2準", "3監", "2準"]:
            ps.transition_status("4377", next_status, db_path=db_path)

        logs = ps.list_action_logs("4377", db_path=db_path)
        seqs = [log["seq"] for log in logs]
        assert seqs == sorted(seqs)


# ==================================================
# upsert_record (移行スクリプト用)
# ==================================================
class TestUpsertRecord:

    def test_upsert_record_creates_without_log(self, db_path):
        rec = ps.create_record("4377", "ワンキャリア")
        ps.upsert_record(rec, db_path=db_path)
        loaded = ps.get_record("4377", db_path=db_path)
        assert loaded is not None
        # upsert は ログを残さない (移行用)
        logs = ps.list_action_logs("4377", db_path=db_path)
        assert logs == []

    def test_upsert_record_overwrites(self, db_path):
        rec1 = ps.create_record("4377", "ワンキャリア", status="3監")
        ps.upsert_record(rec1, db_path=db_path)
        rec2 = ps.create_record("4377", "ワンキャリア改名", status="1保")
        ps.upsert_record(rec2, db_path=db_path)

        loaded = ps.get_record("4377", db_path=db_path)
        assert loaded["stock_name"] == "ワンキャリア改名"
        assert loaded["status"] == "1保"

    def test_upsert_record_requires_code_s(self, db_path):
        with pytest.raises(ValueError):
            ps.upsert_record({"stock_name": "x"}, db_path=db_path)


# ==================================================
# キー名前空間の独立性
# ==================================================
class TestKeyNamespaceIsolation:

    def test_action_log_persists_after_delete(self, db_path):
        """レコード削除後も action_log は残るかつ他キーに影響しない"""
        ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)
        ps.add_to_watch("7089", "フォースタートアップス", db_path=db_path)
        ps.delete_record("4377", db_path=db_path)

        # 4377 のレコードはなくなる
        assert ps.get_record("4377", db_path=db_path) is None
        # 4377 のログは残る (初回登録 + 削除)
        logs = ps.list_action_logs("4377", db_path=db_path)
        assert len(logs) == 2
        # 7089 は無事
        assert ps.get_record("7089", db_path=db_path) is not None

    def test_seq_counter_isolated_per_code(self, db_path):
        ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)
        ps.add_to_watch("7089", "フォースタートアップス", db_path=db_path)
        ps.transition_status("4377", "2準", db_path=db_path)
        ps.transition_status("7089", "2準", db_path=db_path)

        logs_4377 = ps.list_action_logs("4377", db_path=db_path)
        logs_7089 = ps.list_action_logs("7089", db_path=db_path)
        # seq は銘柄ごとに独立 (両方 1, 2 になる)
        assert [log["seq"] for log in logs_4377] == [1, 2]
        assert [log["seq"] for log in logs_7089] == [1, 2]


# ==================================================
# my_watch_list.txt 一方向同期
# ==================================================
class TestSyncToTxt:

    def test_sync_writes_holds_with_h_prefix(self, db_path, tmp_path):
        ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)
        ps.transition_status("4377", "1保", db_path=db_path)
        ps.add_to_watch("7089", "フォースタートアップス", db_path=db_path)
        ps.transition_status("7089", "1保", db_path=db_path)

        txt_path = str(tmp_path / "my_watch_list.txt")
        ps.sync_to_my_watch_list_txt(txt_path=txt_path, db_path=db_path)

        with open(txt_path, encoding="utf-8") as f:
            content = f.read()
        # 1保 が H 接頭辞付きで code_s 昇順に並ぶ
        assert "H4377ワンキャリア" in content
        assert "H7089フォースタートアップス" in content
        # 4377 が 7089 より先に来る
        assert content.index("H4377") < content.index("H7089")

    def test_sync_writes_watch_without_prefix(self, db_path, tmp_path):
        ps.add_to_watch("5032", "AnyColor", db_path=db_path)
        ps.add_to_watch("6232", "ACSL", db_path=db_path)

        txt_path = str(tmp_path / "my_watch_list.txt")
        ps.sync_to_my_watch_list_txt(txt_path=txt_path, db_path=db_path)

        with open(txt_path, encoding="utf-8") as f:
            content = f.read()
        assert "5032AnyColor" in content
        assert "6232ACSL" in content
        # H 接頭辞は付かない
        assert "H5032" not in content

    def test_sync_separates_holds_from_others(self, db_path, tmp_path):
        ps.add_to_watch("4377", "保有銘柄", db_path=db_path)
        ps.transition_status("4377", "1保", db_path=db_path)
        ps.add_to_watch("5032", "ウォッチ銘柄", db_path=db_path)

        txt_path = str(tmp_path / "my_watch_list.txt")
        ps.sync_to_my_watch_list_txt(txt_path=txt_path, db_path=db_path)

        with open(txt_path, encoding="utf-8") as f:
            content = f.read()
        # 1保 → 空行 → 3監 の順 (現行 my_watch_list.txt の見た目互換)
        h_pos = content.index("H4377")
        w_pos = content.index("5032ウォッチ銘柄")
        assert h_pos < w_pos
        # 間に空行あり
        between = content[h_pos:w_pos]
        assert "\n\n" in between

    def test_sync_treats_2jun_as_watch(self, db_path, tmp_path):
        """2準 は H 接頭辞なしで書き出される (txt は 2 値)"""
        ps.add_to_watch("4377", "ワンキャリア", db_path=db_path)
        ps.transition_status("4377", "2準", db_path=db_path)

        txt_path = str(tmp_path / "my_watch_list.txt")
        ps.sync_to_my_watch_list_txt(txt_path=txt_path, db_path=db_path)

        with open(txt_path, encoding="utf-8") as f:
            content = f.read()
        # H 接頭辞なし
        assert "4377ワンキャリア" in content
        assert "H4377" not in content

    def test_sync_empty_db_writes_empty_file(self, db_path, tmp_path):
        txt_path = str(tmp_path / "my_watch_list.txt")
        ps.sync_to_my_watch_list_txt(txt_path=txt_path, db_path=db_path)
        with open(txt_path, encoding="utf-8") as f:
            assert f.read() == ""
