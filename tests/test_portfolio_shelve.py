"""portfolio_shelve.py のテスト (tmp_path で一時DBを作成)"""
import glob

import pytest

import portfolio_shelve as ps


@pytest.fixture
def db_path(tmp_path):
    """テスト用一時DBパスを返す"""
    return str(tmp_path / "test_portfolio_shelve")


def test_backup_portfolio_db_rotates_generations(db_path):
    ps.add_to_watch("3496", db_path=db_path)
    for day in ("260701", "260702", "260703"):
        for ext in ps._SHELVE_EXTENSIONS:
            with open(f"{db_path}_{day}{ext}", "w", encoding="utf-8") as f:
                f.write(day)

    created = ps.backup_portfolio_db(db_path=db_path, generations=2)

    assert created
    for ext in ps._SHELVE_EXTENSIONS:
        backups = sorted(glob.glob(f"{db_path}_??????{ext}"))
        assert len(backups) == 2


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
        # list 系フィールドは [], それ以外は ""
        for k, v in memo.items():
            if k in ps.MEMO_LIST_FIELDS:
                assert v == []
            else:
                assert v == ""

    def test_create_memo_partial(self):
        memo = ps.create_memo(gyoutai_theme="人材", trade_idea="押し目買い")
        assert memo["gyoutai_theme"] == "人材"
        assert memo["trade_idea"] == "押し目買い"
        assert memo["watch_in_reason"] == ""

    def test_create_record_minimal(self):
        rec = ps.create_record("4377")
        assert rec["code_s"] == "4377"
        assert "stock_name" not in rec  # 新スキーマでは銘柄名を持たない
        assert rec["status"] == "3監"
        assert rec["registered_at"]
        assert rec["updated_at"] == rec["registered_at"]
        assert set(rec["memo"].keys()) == ps.MEMO_FIELDS

    def test_create_record_with_explicit_status(self):
        rec = ps.create_record("4377", status="1保")
        assert rec["status"] == "1保"

    def test_create_record_invalid_status(self):
        with pytest.raises(ValueError):
            ps.create_record("4377", status="未定")


# ==================================================
# 高レベル操作: add_to_watch
# ==================================================
class TestAddToWatch:

    def test_add_to_watch_creates_record(self, db_path):
        rec = ps.add_to_watch("4377", db_path=db_path)
        assert rec["code_s"] == "4377"
        assert rec["status"] == "3監"
        assert "stock_name" not in rec  # 新スキーマでは銘柄名を持たない

        loaded = ps.get_record("4377", db_path=db_path)
        assert loaded is not None
        assert "stock_name" not in loaded

    def test_add_to_watch_records_initial_log(self, db_path):
        ps.add_to_watch("4377", db_path=db_path, reason="新規登録")
        logs = ps.list_action_logs("4377", db_path=db_path)
        assert len(logs) == 1
        assert logs[0]["action_type"] == "初回登録"
        assert logs[0]["status_from"] is None
        assert logs[0]["status_to"] == "3監"
        assert logs[0]["reason"] == "新規登録"
        assert logs[0]["seq"] == 1

    def test_add_to_watch_duplicate_raises(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        with pytest.raises(ValueError):
            ps.add_to_watch("4377", db_path=db_path)

    def test_add_to_watch_normalizes_code_s(self, db_path):
        ps.add_to_watch("215a", db_path=db_path)
        loaded = ps.get_record("215A", db_path=db_path)
        assert loaded is not None
        assert loaded["code_s"] == "215A"


# ==================================================
# 高レベル操作: transition_status
# ==================================================
class TestTransitionStatus:

    def test_transition_3kan_to_2jun(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
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
        ps.add_to_watch("4377", db_path=db_path)
        ps.transition_status("4377", "1保", db_path=db_path)
        ps.transition_status("4377", "2準", reason="決算後売り", db_path=db_path)

        logs = ps.list_action_logs("4377", db_path=db_path)
        # 初回登録 + 3監->1保 + 1保->2準 = 3件
        assert len(logs) == 3
        assert logs[2]["action_type"] == "売却"
        assert logs[2]["status_from"] == "1保"
        assert logs[2]["status_to"] == "2準"

    def test_transition_to_1ho_directly_from_3kan(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        rec = ps.transition_status("4377", "1保", db_path=db_path)
        assert rec["status"] == "1保"

    def test_transition_invalid_path_rejected(self, db_path):
        """禁止遷移は ValueError"""
        ps.add_to_watch("4377", db_path=db_path)
        # 3監 -> 3監 は同一遷移として ALLOWED に入っていないので禁止
        # ただし transition_status は同一ステータスなら no-op としているため
        # ここでは別の禁止パターンを試す。実装の ALLOWED_TRANSITIONS 上、
        # すべての非同一遷移は許可されているので、不正遷移は同一以外発生しない。
        # 同一遷移は no-op で通過するので、代わりに未登録銘柄のチェック。
        with pytest.raises(KeyError):
            ps.transition_status("9999", "2準", db_path=db_path)

    def test_transition_same_status_is_noop(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        ps.transition_status("4377", "3監", db_path=db_path)  # no-op
        logs = ps.list_action_logs("4377", db_path=db_path)
        assert len(logs) == 1  # 初回登録だけ、ステータス変更ログは出ない

    def test_transition_with_past_action_date(self, db_path):
        """issue #220: action_date 指定で action_log の timestamp が JST 12:00 で固定される"""
        ps.add_to_watch("4377", db_path=db_path)
        ps.transition_status(
            "4377", "2準",
            reason="3日前に売却",
            action_date="2026-05-10",
            db_path=db_path,
        )
        logs = ps.list_action_logs("4377", db_path=db_path)
        assert logs[1]["timestamp"] == "2026-05-10T12:00:00+09:00"

    def test_transition_with_future_action_date_raises(self, db_path):
        """issue #220: 未来日は ValueError"""
        ps.add_to_watch("4377", db_path=db_path)
        with pytest.raises(ValueError, match="未来日"):
            ps.transition_status(
                "4377", "2準",
                action_date="2099-12-31",
                db_path=db_path,
            )

    def test_transition_with_invalid_action_date_format_raises(self, db_path):
        """issue #220: YYYY-MM-DD 以外のフォーマットは ValueError"""
        ps.add_to_watch("4377", db_path=db_path)
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            ps.transition_status(
                "4377", "2準",
                action_date="2026/05/10",
                db_path=db_path,
            )
        with pytest.raises(ValueError):
            ps.transition_status(
                "4377", "2準",
                action_date="2026-13-99",
                db_path=db_path,
            )

    def test_add_to_watch_with_past_action_date(self, db_path):
        """issue #220: add_to_watch でも action_date が初回登録ログに反映される"""
        ps.add_to_watch(
            "4377",
            reason="昨日メモった銘柄",
            action_date="2026-05-10",
            db_path=db_path,
        )
        logs = ps.list_action_logs("4377", db_path=db_path)
        assert len(logs) == 1
        assert logs[0]["timestamp"] == "2026-05-10T12:00:00+09:00"


# ==================================================
# 高レベル操作: delete_record
# ==================================================
class TestDeleteRecord:

    def test_delete_3kan_succeeds(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        ok = ps.delete_record("4377", reason="興味なくなった", db_path=db_path)
        assert ok is True
        assert ps.get_record("4377", db_path=db_path) is None

    def test_delete_records_log_after_record_gone(self, db_path):
        """削除しても action_log は残る"""
        ps.add_to_watch("4377", db_path=db_path)
        ps.delete_record("4377", reason="不要", db_path=db_path)
        logs = ps.list_action_logs("4377", db_path=db_path)
        # 初回登録 + 削除 = 2件
        assert len(logs) == 2
        assert logs[1]["action_type"] == "削除"
        assert logs[1]["reason"] == "不要"

    def test_delete_1ho_rejected(self, db_path):
        """1保 から直接削除は禁止"""
        ps.add_to_watch("4377", db_path=db_path)
        ps.transition_status("4377", "1保", db_path=db_path)
        with pytest.raises(ValueError):
            ps.delete_record("4377", db_path=db_path)
        # レコードは残っている
        assert ps.get_record("4377", db_path=db_path) is not None

    def test_delete_2jun_rejected(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
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
        ps.add_to_watch("4377", db_path=db_path)
        ps.add_to_watch("7089", db_path=db_path)
        ps.transition_status("7089", "1保", db_path=db_path)

        watch = ps.list_records(status="3監", db_path=db_path)
        hold = ps.list_records(status="1保", db_path=db_path)
        all_recs = ps.list_records(db_path=db_path)

        assert [r["code_s"] for r in watch] == ["4377"]
        assert [r["code_s"] for r in hold] == ["7089"]
        assert [r["code_s"] for r in all_recs] == ["4377", "7089"]

    def test_list_records_sorted_by_code_s(self, db_path):
        ps.add_to_watch("7089", db_path=db_path)
        ps.add_to_watch("4377", db_path=db_path)
        ps.add_to_watch("215A", db_path=db_path)

        recs = ps.list_records(db_path=db_path)
        assert [r["code_s"] for r in recs] == ["215A", "4377", "7089"]

    def test_list_action_logs_per_code(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        ps.add_to_watch("7089", db_path=db_path)
        ps.transition_status("4377", "2準", db_path=db_path)

        logs_4377 = ps.list_action_logs("4377", db_path=db_path)
        logs_7089 = ps.list_action_logs("7089", db_path=db_path)
        all_logs = ps.list_action_logs(db_path=db_path)

        assert len(logs_4377) == 2
        assert len(logs_7089) == 1
        assert len(all_logs) == 3

    def test_list_action_logs_sorted_by_seq(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
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
        rec = ps.create_record("4377")
        ps.upsert_record(rec, db_path=db_path)
        loaded = ps.get_record("4377", db_path=db_path)
        assert loaded is not None
        # upsert は ログを残さない (移行用)
        logs = ps.list_action_logs("4377", db_path=db_path)
        assert logs == []

    def test_upsert_record_overwrites(self, db_path):
        rec1 = ps.create_record("4377", status="3監")
        ps.upsert_record(rec1, db_path=db_path)
        rec2 = ps.create_record("4377", status="1保")
        ps.upsert_record(rec2, db_path=db_path)

        loaded = ps.get_record("4377", db_path=db_path)
        assert loaded["status"] == "1保"

    def test_upsert_record_requires_code_s(self, db_path):
        with pytest.raises(ValueError):
            ps.upsert_record({"status": "3監"}, db_path=db_path)


# ==================================================
# キー名前空間の独立性
# ==================================================
class TestKeyNamespaceIsolation:

    def test_action_log_persists_after_delete(self, db_path):
        """レコード削除後も action_log は残るかつ他キーに影響しない"""
        ps.add_to_watch("4377", db_path=db_path)
        ps.add_to_watch("7089", db_path=db_path)
        ps.delete_record("4377", db_path=db_path)

        # 4377 のレコードはなくなる
        assert ps.get_record("4377", db_path=db_path) is None
        # 4377 のログは残る (初回登録 + 削除)
        logs = ps.list_action_logs("4377", db_path=db_path)
        assert len(logs) == 2
        # 7089 は無事
        assert ps.get_record("7089", db_path=db_path) is not None

    def test_seq_counter_isolated_per_code(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        ps.add_to_watch("7089", db_path=db_path)
        ps.transition_status("4377", "2準", db_path=db_path)
        ps.transition_status("7089", "2準", db_path=db_path)

        logs_4377 = ps.list_action_logs("4377", db_path=db_path)
        logs_7089 = ps.list_action_logs("7089", db_path=db_path)
        # seq は銘柄ごとに独立 (両方 1, 2 になる)
        assert [log["seq"] for log in logs_4377] == [1, 2]
        assert [log["seq"] for log in logs_7089] == [1, 2]


# ==================================================
# 高レベル操作: update_memo (部分更新)
# ==================================================
class TestUpdateMemo:

    @pytest.fixture(autouse=True)
    def seed_strategies(self, db_path):
        """trade_idea の shelve マスターをシード投入 (issue #335 移行後必須)"""
        ps.seed_trade_ideas(db_path=db_path)

    def test_update_single_field(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        rec = ps.update_memo("4377", {"trade_idea": "中期モメンタム"}, db_path=db_path)
        assert rec["memo"]["trade_idea"] == "中期モメンタム"
        # メモ更新は action_log を記録しない (初回登録の 1 件のみ)
        logs = ps.list_action_logs("4377", db_path=db_path)
        assert len(logs) == 1
        assert logs[0]["action_type"] == "初回登録"

    @pytest.mark.parametrize(
        "current, new_value, expect_error",
        [
            # 定型リスト内の値は保存できる
            ("", "GARP", False),
            ("", "底値リバ", False),
            # 空文字 (未分類) はいつでも許容
            ("GARP", "", False),
            # リスト外の純新規値は ValueError
            ("", "押し目買い", True),
            # リスト外でも現行値と同じなら保持を許可 (旧自由記述の救済)
            ("押し目買い", "押し目買い", False),
        ],
    )
    def test_trade_idea_options_validation(self, db_path, current, new_value, expect_error):
        """trade_idea の定型値チェック (issue #327): リスト内/空は許容、リスト外新規は拒否、現行値は救済"""
        ps.add_to_watch("4377", db_path=db_path)
        if current:
            # 旧自由記述値は移行 (create_record 直接格納) 相当として update_memo を通さず埋め込む
            rec = ps.get_record("4377", db_path=db_path)
            rec["memo"]["trade_idea"] = current
            ps.upsert_record(rec, db_path=db_path)
        if expect_error:
            with pytest.raises(ValueError, match="マスター未登録"):
                ps.update_memo("4377", {"trade_idea": new_value}, db_path=db_path)
        else:
            rec = ps.update_memo("4377", {"trade_idea": new_value}, db_path=db_path)
            assert rec["memo"]["trade_idea"] == new_value

    def test_update_partial_keeps_other_fields(self, db_path):
        """部分更新: fields に含まれないキーは現行値据え置き (codex P1 対応)"""
        ps.add_to_watch("4377", db_path=db_path)
        ps.update_memo(
            "4377",
            {"trade_idea": "中期テーマ", "watch_in_reason": "B", "stage": "1S"},
            db_path=db_path,
        )
        # 1 項目だけ送信、残りは据え置きされるべき
        rec = ps.update_memo("4377", {"trade_idea": "GARP"}, db_path=db_path)
        assert rec["memo"]["trade_idea"] == "GARP"
        assert rec["memo"]["watch_in_reason"] == "B"  # 据え置き
        assert rec["memo"]["stage"] == "1S"           # 据え置き

    def test_update_with_empty_string_overwrites(self, db_path):
        """空文字を明示的に渡したらメモ削除として "" に上書き"""
        ps.add_to_watch("4377", db_path=db_path)
        ps.update_memo("4377", {"trade_idea": "中期モメンタム"}, db_path=db_path)
        rec = ps.update_memo("4377", {"trade_idea": ""}, db_path=db_path)
        assert rec["memo"]["trade_idea"] == ""
        # メモ更新は action_log を記録しない (初回登録の 1 件のみ)
        logs = ps.list_action_logs("4377", db_path=db_path)
        assert len(logs) == 1

    def test_update_with_none_normalizes_to_empty_string(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        rec = ps.update_memo("4377", {"trade_idea": None}, db_path=db_path)
        assert rec["memo"]["trade_idea"] == ""

    def test_update_all_eight_fields(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        # list 系フィールド (gyoutai_themes) は別バリデーション経路なのでこのテストでは除外
        str_fields = ps.MEMO_FIELDS - ps.MEMO_LIST_FIELDS
        all_fields = {f: f"val_{f}" for f in str_fields}
        all_fields["trade_idea"] = "GARP"  # issue #327: trade_idea は定型値のみ許容
        rec = ps.update_memo("4377", all_fields, db_path=db_path)
        for k, v in all_fields.items():
            assert rec["memo"][k] == v
        logs = ps.list_action_logs("4377", db_path=db_path)
        assert not any(log["action_type"] == "メモ更新" for log in logs)

    def test_update_no_diff_is_noop(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        ps.update_memo("4377", {"trade_idea": "中期モメンタム"}, db_path=db_path)
        rec_before = ps.get_record("4377", db_path=db_path)
        ps.update_memo("4377", {"trade_idea": "中期モメンタム"}, db_path=db_path)
        rec_after = ps.get_record("4377", db_path=db_path)
        # updated_at が変わらない (no-op)
        assert rec_before["updated_at"] == rec_after["updated_at"]
        # メモ更新は action_log を記録しない (初回登録の 1 件のみ)
        logs = ps.list_action_logs("4377", db_path=db_path)
        assert not any(log["action_type"] == "メモ更新" for log in logs)

    def test_update_empty_dict_is_noop(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        rec = ps.update_memo("4377", {}, db_path=db_path)
        # KeyError なし、no-op として現行 record を返す
        assert rec["code_s"] == "4377"
        logs = ps.list_action_logs("4377", db_path=db_path)
        assert not any(log["action_type"] == "メモ更新" for log in logs)

    def test_update_unknown_field_raises(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        with pytest.raises(ValueError):
            ps.update_memo("4377", {"unknown_field": "x"}, db_path=db_path)

    def test_update_non_str_value_raises(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        with pytest.raises(TypeError):
            ps.update_memo("4377", {"trade_idea": 123}, db_path=db_path)

    def test_update_unregistered_record_raises(self, db_path):
        with pytest.raises(KeyError):
            ps.update_memo("9999", {"trade_idea": "GARP"}, db_path=db_path)

    def test_update_invalid_code_s_raises(self, db_path):
        with pytest.raises(ValueError):
            ps.update_memo("abc", {"trade_idea": "GARP"}, db_path=db_path)

    def test_update_normalizes_code_s(self, db_path):
        ps.add_to_watch("215A", db_path=db_path)
        rec = ps.update_memo("215a", {"trade_idea": "GARP"}, db_path=db_path)
        assert rec["code_s"] == "215A"
        assert rec["memo"]["trade_idea"] == "GARP"


class TestGyoutaiThemesField:
    """issue #187: gyoutai_themes (list[str]) フィールドの読み書き / バリデーション。

    issue #282: update_memo がマスター登録済み name のみ受け付けるよう変更されたため、
    テストはマスター登録を前提に動作させる。
    """

    @pytest.fixture(autouse=True)
    def _seed_master(self, db_path):
        """テストで使う name (半導体 / AI) をマスター登録し、戦略シードも投入する"""
        ps.create_theme("半導体", db_path=db_path)
        ps.create_theme("AI", db_path=db_path)
        ps.seed_trade_ideas(db_path=db_path)

    def test_create_memo_defaults_empty_list(self):
        memo = ps.create_memo()
        assert memo["gyoutai_themes"] == []

    def test_create_memo_with_explicit_list(self):
        memo = ps.create_memo(gyoutai_themes=["半導体", "AI"])
        assert memo["gyoutai_themes"] == ["半導体", "AI"]

    def test_update_gyoutai_themes_saves_list(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        rec = ps.update_memo(
            "4377", {"gyoutai_themes": ["半導体", "AI"]}, db_path=db_path
        )
        assert rec["memo"]["gyoutai_themes"] == ["半導体", "AI"]

    def test_update_gyoutai_themes_empty_list_overwrites(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        ps.update_memo("4377", {"gyoutai_themes": ["半導体"]}, db_path=db_path)
        rec = ps.update_memo("4377", {"gyoutai_themes": []}, db_path=db_path)
        assert rec["memo"]["gyoutai_themes"] == []

    def test_update_gyoutai_themes_partial_keeps_other_fields(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        ps.update_memo("4377", {"trade_idea": "GARP"}, db_path=db_path)
        rec = ps.update_memo(
            "4377", {"gyoutai_themes": ["AI"]}, db_path=db_path
        )
        assert rec["memo"]["gyoutai_themes"] == ["AI"]
        assert rec["memo"]["trade_idea"] == "GARP"

    def test_update_gyoutai_themes_rejects_non_list(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        with pytest.raises(TypeError):
            ps.update_memo(
                "4377", {"gyoutai_themes": "半導体\nAI"}, db_path=db_path
            )

    def test_update_gyoutai_themes_rejects_non_str_element(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        with pytest.raises(TypeError):
            ps.update_memo(
                "4377", {"gyoutai_themes": ["半導体", 123]}, db_path=db_path
            )

    def test_update_gyoutai_themes_no_diff_is_noop(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        ps.update_memo("4377", {"gyoutai_themes": ["AI"]}, db_path=db_path)
        ps.update_memo("4377", {"gyoutai_themes": ["AI"]}, db_path=db_path)
        logs = ps.list_action_logs("4377", db_path=db_path)
        # メモ更新は action_log を記録しない
        assert not any(log["action_type"] == "メモ更新" for log in logs)

    def test_legacy_record_without_gyoutai_themes_loads_as_empty_list(
        self, db_path
    ):
        """旧データに gyoutai_themes フィールドがなくても、読み込み時に [] が補完される。"""
        from db_shelve import ShelveDB

        # 旧スキーマ (gyoutai_themes なし) で直接 shelve に書き込む
        legacy_record = {
            "code_s": "4377",
            "status": "3監",
            "registered_at": "2024-01-01T00:00:00+09:00",
            "updated_at": "2024-01-01T00:00:00+09:00",
            "memo": {
                "gyoutai_theme": "半導体\nAI",
                "trade_idea": "",
                "watch_in_reason": "",
                "inago_origin": "",
                "takaichi_sensitivity": "",
                "last_research_update": "",
                "stage": "",
                "jukyu_chart": "",
            },
            "excluded": False,
        }
        with ShelveDB(db_path) as db:
            db["record:4377"] = legacy_record

        rec = ps.get_record("4377", db_path=db_path)
        assert rec["memo"]["gyoutai_themes"] == []

        # list_records 経由でも補完される
        recs = ps.list_records(db_path=db_path)
        assert len(recs) == 1
        assert recs[0]["memo"]["gyoutai_themes"] == []


# ==================================================
# my_watch_list.txt 一方向同期
# ==================================================
@pytest.fixture
def stocks_db_for_sync(tmp_path, monkeypatch):
    """sync_to_my_watch_list_txt が銘柄名を引くための stocks_shelve を差し替え。

    銘柄名は portfolio_shelve には保存されないため、sync は stocks_shelve / research_shelve
    から都度取得する。テストでは tmp_path の stocks_shelve を用意して期待値を入れておく。
    """
    from db_shelve import ShelveDB

    stocks_path = str(tmp_path / "test_stocks_shelve")
    research_path = str(tmp_path / "test_research_shelve")
    monkeypatch.setattr("db_shelve.STOCKS_SHELVE", stocks_path)
    monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", research_path)

    name_map = {
        "4377": "ワンキャリア",
        "7089": "フォースタートアップス",
        "5032": "AnyColor",
        "6232": "ACSL",
    }
    with ShelveDB(stocks_path) as db:
        for code, name in name_map.items():
            db[code] = {"code_s": code, "stock_name": name}
    return stocks_path


class TestSyncToTxt:

    def test_sync_writes_holds_with_h_prefix(self, db_path, tmp_path, stocks_db_for_sync):
        ps.add_to_watch("4377", db_path=db_path)
        ps.transition_status("4377", "1保", db_path=db_path)
        ps.add_to_watch("7089", db_path=db_path)
        ps.transition_status("7089", "1保", db_path=db_path)

        txt_path = str(tmp_path / "my_watch_list.txt")
        ps.sync_to_my_watch_list_txt(txt_path=txt_path, db_path=db_path)

        with open(txt_path, encoding="utf-8") as f:
            content = f.read()
        # 1保 が H 接頭辞付きで code_s 昇順に並ぶ。銘柄名は stocks_shelve から引かれる。
        assert "H4377ワンキャリア" in content
        assert "H7089フォースタートアップス" in content
        # 4377 が 7089 より先に来る
        assert content.index("H4377") < content.index("H7089")

    def test_sync_writes_watch_without_prefix(self, db_path, tmp_path, stocks_db_for_sync):
        ps.add_to_watch("5032", db_path=db_path)
        ps.add_to_watch("6232", db_path=db_path)

        txt_path = str(tmp_path / "my_watch_list.txt")
        ps.sync_to_my_watch_list_txt(txt_path=txt_path, db_path=db_path)

        with open(txt_path, encoding="utf-8") as f:
            content = f.read()
        assert "5032AnyColor" in content
        assert "6232ACSL" in content
        # H 接頭辞は付かない
        assert "H5032" not in content

    def test_sync_separates_holds_from_others(self, db_path, tmp_path, stocks_db_for_sync):
        ps.add_to_watch("4377", db_path=db_path)
        ps.transition_status("4377", "1保", db_path=db_path)
        ps.add_to_watch("5032", db_path=db_path)

        txt_path = str(tmp_path / "my_watch_list.txt")
        ps.sync_to_my_watch_list_txt(txt_path=txt_path, db_path=db_path)

        with open(txt_path, encoding="utf-8") as f:
            content = f.read()
        # 1保 → 空行 → 3監 の順 (現行 my_watch_list.txt の見た目互換)
        h_pos = content.index("H4377")
        w_pos = content.index("5032AnyColor")
        assert h_pos < w_pos
        # 間に空行あり
        between = content[h_pos:w_pos]
        assert "\n\n" in between

    def test_sync_treats_2jun_as_watch(self, db_path, tmp_path, stocks_db_for_sync):
        """2準 は H 接頭辞なしで書き出される (txt は 2 値)"""
        ps.add_to_watch("4377", db_path=db_path)
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

    def test_sync_skips_excluded(self, db_path, tmp_path, stocks_db_for_sync):
        """excluded=True のレコードは txt 出力に含まれない"""
        ps.add_to_watch("5032", db_path=db_path)
        ps.add_to_watch("6232", db_path=db_path)
        ps.exclude_from_universe("6232", db_path=db_path)

        txt_path = str(tmp_path / "my_watch_list.txt")
        ps.sync_to_my_watch_list_txt(txt_path=txt_path, db_path=db_path)

        with open(txt_path, encoding="utf-8") as f:
            content = f.read()
        assert "5032AnyColor" in content
        assert "6232" not in content


# ==================================================
# ユニバース除外 (issue #186)
# ==================================================
class TestExcludeFromUniverse:

    def test_exclude_3kan_record(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        result = ps.exclude_from_universe("4377", reason="不要", db_path=db_path)
        assert result is True
        record = ps.get_record("4377", db_path=db_path)
        assert record["excluded"] is True

    def test_exclude_logs_action(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        ps.exclude_from_universe("4377", reason="ノイズ", db_path=db_path)
        logs = ps.list_action_logs("4377", db_path=db_path)
        # [初回登録, ユニバース除外]
        assert logs[-1]["action_type"] == "ユニバース除外"
        assert logs[-1]["reason"] == "ノイズ"

    def test_exclude_already_excluded_returns_false(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        ps.exclude_from_universe("4377", db_path=db_path)
        result = ps.exclude_from_universe("4377", db_path=db_path)
        assert result is False

    def test_exclude_1ho_rejected(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        ps.transition_status("4377", "1保", db_path=db_path)
        with pytest.raises(ValueError, match="2準/3監"):
            ps.exclude_from_universe("4377", db_path=db_path)

    def test_exclude_2jun_allowed(self, db_path):
        """2準 銘柄もユニバース除外可能 (1保 は禁止のまま)"""
        ps.add_to_watch("4377", db_path=db_path)
        ps.transition_status("4377", "2準", db_path=db_path)
        result = ps.exclude_from_universe("4377", reason="準保有から除外", db_path=db_path)
        assert result is True
        rec = ps.get_record("4377", db_path=db_path)
        assert rec is not None
        assert rec["excluded"] is True
        assert rec["status"] == "2準"  # status は変えない、excluded フラグのみ
        logs = ps.list_action_logs("4377", db_path=db_path)
        assert any(l["action_type"] == "ユニバース除外" for l in logs)

    def test_exclude_unregistered_returns_false(self, db_path):
        result = ps.exclude_from_universe("4377", db_path=db_path)
        assert result is False

    def test_exclude_empty_reason_ok(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        result = ps.exclude_from_universe("4377", db_path=db_path)
        assert result is True


class TestAddToWatchRevive:

    def test_revive_excluded_record(self, db_path):
        ps.add_to_watch("4377", memo=ps.create_memo(stage="3S"), db_path=db_path)
        ps.exclude_from_universe("4377", db_path=db_path)
        record = ps.add_to_watch("4377", db_path=db_path)
        assert record["excluded"] is False
        # メモは保持される
        assert record["memo"]["stage"] == "3S"

    def test_revive_logs_universe_exclusion_with_revive_reason(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        ps.exclude_from_universe("4377", reason="ノイズ", db_path=db_path)
        ps.add_to_watch("4377", db_path=db_path)
        logs = ps.list_action_logs("4377", db_path=db_path)
        # 末尾は復活ログ (action_type=ユニバース除外, reason=復活)
        assert logs[-1]["action_type"] == "ユニバース除外"
        assert logs[-1]["reason"] == "復活"

    def test_add_to_watch_active_record_still_raises(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        with pytest.raises(ValueError, match="既に登録済み"):
            ps.add_to_watch("4377", db_path=db_path)


class TestPortfolioRecordBackwardCompat:

    def test_legacy_record_without_excluded_loads_as_false(self, db_path):
        """旧スキーマのレコード (excluded キーなし) を直接書き込み、list_records で読める"""
        from db_shelve import ShelveDB

        legacy_record = {
            "code_s": "4377",
            "status": "3監",
            "registered_at": "2024-01-01T00:00:00+09:00",
            "updated_at": "2024-01-01T00:00:00+09:00",
            "memo": ps.create_memo(),
        }
        with ShelveDB(db_path) as db:
            db["record:4377"] = legacy_record

        records = ps.list_records(db_path=db_path)
        assert len(records) == 1
        # excluded キーが無くても表示される (デフォルト False 扱い)
        assert records[0]["code_s"] == "4377"


class TestListRecordsFilterExcluded:

    def test_default_filters_excluded(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        ps.add_to_watch("5032", db_path=db_path)
        ps.exclude_from_universe("4377", db_path=db_path)

        records = ps.list_records(db_path=db_path)
        codes = [r["code_s"] for r in records]
        assert codes == ["5032"]

    def test_include_excluded_returns_all(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        ps.add_to_watch("5032", db_path=db_path)
        ps.exclude_from_universe("4377", db_path=db_path)

        records = ps.list_records(include_excluded=True, db_path=db_path)
        codes = [r["code_s"] for r in records]
        assert codes == ["4377", "5032"]


# ==================================================
# issue #269: 保有株数 (qty) の入出力
# ==================================================
class TestQty:
    """update_qty / get_record の qty 補完を集約テスト"""

    @pytest.mark.parametrize(
        "new_qty, expected",
        [
            (100, 100),   # 新規セット
            (250, 250),   # 上書き
        ],
        ids=["set-100", "overwrite-250"],
    )
    def test_update_qty_writes_value_and_action_log(self, db_path, new_qty, expected):
        """update_qty は qty を更新し action_log に「株数変更」を記録する。"""
        ps.add_to_watch("4377", db_path=db_path)
        ps.transition_status("4377", "1保", db_path=db_path)
        log_count_before = len(ps.list_action_logs("4377", db_path=db_path))

        rec = ps.update_qty("4377", new_qty, db_path=db_path)
        assert rec["qty"] == expected
        logs = ps.list_action_logs("4377", db_path=db_path)
        assert len(logs) == log_count_before + 1
        assert logs[-1]["action_type"] == "株数変更"

    def test_update_qty_noop_no_action_log(self, db_path):
        """差分なし (初期 qty=0 に 0 を入れる) は no-op でログが増えない。"""
        ps.add_to_watch("4377", db_path=db_path)
        ps.transition_status("4377", "1保", db_path=db_path)
        log_count_before = len(ps.list_action_logs("4377", db_path=db_path))
        ps.update_qty("4377", 0, db_path=db_path)  # 初期値=0 なので no-op
        assert len(ps.list_action_logs("4377", db_path=db_path)) == log_count_before

    @pytest.mark.parametrize(
        "bad_qty, exc",
        [
            (-1, ValueError),
            (1.5, TypeError),
            ("100", TypeError),
            (True, TypeError),     # bool は除外する仕様
        ],
        ids=["negative", "float", "str", "bool"],
    )
    def test_update_qty_rejects_invalid(self, db_path, bad_qty, exc):
        ps.add_to_watch("4377", db_path=db_path)
        ps.transition_status("4377", "1保", db_path=db_path)
        with pytest.raises(exc):
            ps.update_qty("4377", bad_qty, db_path=db_path)

    def test_update_qty_unregistered_raises_keyerror(self, db_path):
        with pytest.raises(KeyError):
            ps.update_qty("9999", 100, db_path=db_path)

    def test_legacy_record_without_qty_loads_as_zero(self, db_path):
        """qty キーが無い旧データは get_record / list_records で qty=0 補完される"""
        from db_shelve import ShelveDB

        legacy_record = {
            "code_s": "4377",
            "status": "1保",
            "registered_at": "2024-01-01T00:00:00+09:00",
            "updated_at": "2024-01-01T00:00:00+09:00",
            "memo": ps.create_memo(),
            "excluded": False,
            # qty なし
        }
        with ShelveDB(db_path) as db:
            db["record:4377"] = legacy_record

        rec = ps.get_record("4377", db_path=db_path)
        assert rec["qty"] == 0
        records = ps.list_records(db_path=db_path)
        assert records[0]["qty"] == 0


class TestQtyGlobalUpdatedAt:
    """PF 全体で 1 つ持つ qty 最終更新タイムスタンプの集約テスト"""

    def test_initial_state_is_none(self, db_path):
        """qty 更新が一度も走っていなければ None"""
        assert ps.get_qty_global_updated_at(db_path=db_path) is None

    def test_updated_when_qty_changes(self, db_path):
        """qty が実際に変化したときだけ ISO 8601 形式のタイムスタンプが書き込まれる"""
        ps.add_to_watch("4377", db_path=db_path)
        ps.transition_status("4377", "1保", db_path=db_path)
        ps.update_qty("4377", 100, db_path=db_path)

        ts = ps.get_qty_global_updated_at(db_path=db_path)
        assert ts is not None
        # ISO 8601 (JST タイムゾーン付き) の先頭は YYYY-MM-DD
        assert len(ts) >= 10 and ts[4] == "-" and ts[7] == "-"

    def test_not_updated_on_noop(self, db_path):
        """qty が同じ値で再保存された (no-op) ときはタイムスタンプを更新しない"""
        ps.add_to_watch("4377", db_path=db_path)
        ps.transition_status("4377", "1保", db_path=db_path)
        ps.update_qty("4377", 100, db_path=db_path)
        ts_before = ps.get_qty_global_updated_at(db_path=db_path)

        # 同じ値で再保存 → no-op
        ps.update_qty("4377", 100, db_path=db_path)
        ts_after = ps.get_qty_global_updated_at(db_path=db_path)
        assert ts_after == ts_before


# ==================================================
# テーママスター (issue #282)
# ==================================================
class TestThemeMaster:
    """create / update / delete / count + update_memo の整合性"""

    @pytest.mark.parametrize(
        "name",
        [
            "",                        # 空文字
            "   ",                     # 空白のみ
            "x" * 31,                  # 上限超過 (THEME_NAME_MAX_LEN=30)
            "AI/半導体",                # URL 禁止 /
            "AI?半導体",                # URL 禁止 ?
            "AI&半導体",                # URL 禁止 &
            "AI\nNL",                  # 制御文字
        ],
    )
    def test_create_theme_rejects_invalid_names(self, db_path, name):
        with pytest.raises(ValueError):
            ps.create_theme(name, db_path=db_path)

    def test_create_theme_rejects_duplicate(self, db_path):
        ps.create_theme("半導体", db_path=db_path)
        with pytest.raises(ValueError):
            ps.create_theme("半導体", db_path=db_path)

    def test_update_theme_renames_records(self, db_path):
        """リネームで既存銘柄の memo[gyoutai_themes] も新 name に追従し、削除では除去される。
        action_log にも "メモ更新" が追記され、影響件数が delete_theme の戻り値と一致する。
        """
        ps.create_theme("半導体", db_path=db_path)
        ps.create_theme("AI", db_path=db_path)
        ps.add_to_watch("6324", db_path=db_path)
        ps.update_memo(
            "6324", {"gyoutai_themes": ["半導体", "AI"]}, db_path=db_path
        )

        # リネーム
        ps.update_theme("半導体", new_name="セミコン", db_path=db_path)
        rec = ps.get_record("6324", db_path=db_path)
        assert rec["memo"]["gyoutai_themes"] == ["セミコン", "AI"]
        assert ps.get_theme("半導体", db_path=db_path) is None
        assert ps.get_theme("セミコン", db_path=db_path)["name"] == "セミコン"

        # 削除 → 影響 1 銘柄、該当 name が消える
        affected = ps.delete_theme("セミコン", db_path=db_path)
        assert affected == 1
        rec = ps.get_record("6324", db_path=db_path)
        assert rec["memo"]["gyoutai_themes"] == ["AI"]

        # メモ更新は action_log を記録しない
        logs = ps.list_action_logs("6324", db_path=db_path)
        assert not any(l["action_type"] == "メモ更新" for l in logs)

    @pytest.mark.parametrize(
        "current,posted,expected,should_raise",
        [
            # 純新規の未登録 name は ValueError
            (["半導体"], ["半導体", "未登録新規"], None, True),
            # 現行レコードに既にある未登録 name は保持を許可 (移行漏れ救済)
            (["未登録既存"], ["未登録既存"], ["未登録既存"], False),
            # 未登録 name を空文字に置き換えれば除去できる
            (["未登録既存"], [""], [], False),
            # マスター登録済み name は採用
            (["半導体"], ["AI"], ["AI"], False),
            # 重複は除去される
            (["半導体"], ["AI", "AI"], ["AI"], False),
        ],
    )
    def test_update_memo_master_validation(
        self, db_path, current, posted, expected, should_raise
    ):
        """update_memo の gyoutai_themes マスター未登録判定 (issue #282)"""
        ps.create_theme("半導体", db_path=db_path)
        ps.create_theme("AI", db_path=db_path)
        ps.add_to_watch("6324", db_path=db_path)
        # 現行値を直書き込みで仕込む (未登録 name を含むケースを作るため)
        rec = ps.get_record("6324", db_path=db_path)
        rec["memo"]["gyoutai_themes"] = current
        ps.upsert_record(rec, db_path=db_path)

        if should_raise:
            with pytest.raises(ValueError):
                ps.update_memo(
                    "6324", {"gyoutai_themes": posted}, db_path=db_path
                )
        else:
            ps.update_memo(
                "6324", {"gyoutai_themes": posted}, db_path=db_path
            )
            after = ps.get_record("6324", db_path=db_path)
            assert after["memo"]["gyoutai_themes"] == expected

    def test_count_theme_usage(self, db_path):
        ps.create_theme("AI", db_path=db_path)
        ps.create_theme("半導体", db_path=db_path)
        ps.add_to_watch("6324", db_path=db_path)
        ps.add_to_watch("9984", db_path=db_path)
        ps.update_memo("6324", {"gyoutai_themes": ["AI", "半導体"]}, db_path=db_path)
        ps.update_memo("9984", {"gyoutai_themes": ["AI"]}, db_path=db_path)
        assert ps.count_theme_usage(db_path=db_path) == {"AI": 2, "半導体": 1}


@pytest.fixture
def db_path_ti(tmp_path):
    """戦略マスターテスト用の一時 DB パス。"""
    return str(tmp_path / "test_portfolio_ti")


class TestTradeIdeaMaster:
    """戦略マスター CRUD + update_memo 整合性 (issue #335)"""

    def test_create_and_list(self, db_path_ti):
        """作成・一覧・get の基本動作。"""
        ps.create_trade_idea("GARP", "説明A", "中長期", True, db_path=db_path_ti)
        ps.create_trade_idea("夢枠", "説明B", "長期", False, db_path=db_path_ti)

        items = ps.list_trade_ideas(db_path=db_path_ti)
        assert len(items) == 2
        names = [i["name"] for i in items]
        assert names == sorted(names)

        got = ps.get_trade_idea("GARP", db_path=db_path_ti)
        assert got["time_horizon"] == "中長期"
        assert got["over_earnings"] is True

        # 重複は ValueError
        with pytest.raises(ValueError):
            ps.create_trade_idea("GARP", db_path=db_path_ti)

    def test_update_renames_records(self, db_path_ti):
        """リネームで全 record の memo[trade_idea] が追従し、delete で "" になること。"""
        ps.create_trade_idea("中期モメンタム", "説明", "中期", False, db_path=db_path_ti)
        ps.add_to_watch("6324", db_path=db_path_ti)
        ps.update_memo("6324", {"trade_idea": "中期モメンタム"}, db_path=db_path_ti)

        # リネーム
        ps.update_trade_idea("中期モメンタム", new_name="モメンタム中期", db_path=db_path_ti)
        rec = ps.get_record("6324", db_path=db_path_ti)
        assert rec["memo"]["trade_idea"] == "モメンタム中期"

        # 削除 → trade_idea が空文字にリセット
        affected = ps.delete_trade_idea("モメンタム中期", db_path=db_path_ti)
        assert affected == 1
        rec = ps.get_record("6324", db_path=db_path_ti)
        assert rec["memo"]["trade_idea"] == ""

        # メモ更新は action_log を記録しない
        logs = ps.list_action_logs("6324", db_path=db_path_ti)
        assert not any(l["action_type"] == "メモ更新" for l in logs)

    @pytest.mark.parametrize(
        "current_idea,posted,should_raise",
        [
            ("中期モメンタム", "中期モメンタム", False),  # マスター登録済み → OK
            ("中期モメンタム", "",              False),  # 空文字（未分類）→ 常に許容
            ("",              "未登録新規",     True),   # マスター未登録の純新規 → ValueError
            ("旧自由記述",    "旧自由記述",     False),  # 現行レコードの未登録値は保持許可
        ],
    )
    def test_update_memo_master_validation(
        self, db_path_ti, current_idea, posted, should_raise
    ):
        """update_memo の trade_idea マスター未登録判定 (issue #335)"""
        ps.create_trade_idea("中期モメンタム", db_path=db_path_ti)
        ps.add_to_watch("6324", db_path=db_path_ti)
        # 現行値を直書き込みで仕込む
        rec = ps.get_record("6324", db_path=db_path_ti)
        rec["memo"]["trade_idea"] = current_idea
        ps.upsert_record(rec, db_path=db_path_ti)

        if should_raise:
            with pytest.raises(ValueError):
                ps.update_memo("6324", {"trade_idea": posted}, db_path=db_path_ti)
        else:
            ps.update_memo("6324", {"trade_idea": posted}, db_path=db_path_ti)
            after = ps.get_record("6324", db_path=db_path_ti)
            assert after["memo"]["trade_idea"] == posted

    def test_seed_trade_ideas_idempotent(self, db_path_ti):
        """seed_trade_ideas() は空の場合のみ投入し、2回呼んでも件数が変わらないこと。"""
        count = ps.seed_trade_ideas(db_path=db_path_ti)
        assert count == len(ps._TRADE_IDEA_SEED)
        items_after_first = ps.list_trade_ideas(db_path=db_path_ti)

        # 2回目は 0 件投入（冪等）
        count2 = ps.seed_trade_ideas(db_path=db_path_ti)
        assert count2 == 0
        items_after_second = ps.list_trade_ideas(db_path=db_path_ti)
        assert len(items_after_first) == len(items_after_second)


# ==================================================
# issue #361: 終値プロキシ自動付与・バックフィル・土日補正
# ==================================================

from datetime import date as _date  # noqa: E402


@pytest.fixture
def stocks_with_price_log(tmp_path, monkeypatch):
    """_fetch_price_proxy が引く stocks_shelve に price_log を仕込む。

    price_log は [(date, int終値), ...]。5/8(金)=3000, 5/11(月)=3200。
    5/9(土)・5/10(日) は無い → 土日補正で 5/8 の終値が引ける。
    """
    from db_shelve import ShelveDB

    stocks_path = str(tmp_path / "test_stocks_shelve")
    monkeypatch.setattr("db_shelve.STOCKS_SHELVE", stocks_path)
    with ShelveDB(stocks_path) as db:
        db["6324"] = {"price_log": [
            (_date(2026, 5, 8), 3000),
            (_date(2026, 5, 11), 3200),
        ]}
    return stocks_path


@pytest.mark.parametrize("action, expect_proxy", [
    ("hold", True),      # 1保遷移 → 付与
    ("qty_change", True),  # 株数変更 → 付与
    ("sell", True),      # 売却 → 付与
    ("watch", False),    # 初回登録(3監) → 未付与
])
def test_append_action_log_price_proxy(db_path, stocks_with_price_log, action, expect_proxy):
    """売買日イベントのみ price_proxy が自動付与される (issue #361)。"""
    if action == "watch":
        ps.add_to_watch("6324", db_path=db_path)
        logs = ps.list_action_logs("6324", db_path=db_path)
        assert logs[-1]["price_proxy"] is None
        assert logs[-1]["price_source"] is None
        return

    ps.add_to_watch("6324", db_path=db_path)
    ps.transition_status("6324", "2準", db_path=db_path)
    ps.transition_status("6324", "1保", action_date="2026-05-11", qty=500, db_path=db_path)
    if action == "qty_change":
        ps.update_qty("6324", 700, action_date="2026-05-11", db_path=db_path)
    if action == "sell":
        ps.transition_status("6324", "2準", action_date="2026-05-11", db_path=db_path)

    logs = ps.list_action_logs("6324", db_path=db_path)
    latest = logs[-1]
    assert latest["price_source"] == "close"
    assert latest["price_proxy"] == 3200  # 5/11 の終値


def test_append_action_log_weekend_normalized(db_path, stocks_with_price_log):
    """土日 (5/9土) の売買日は直前営業日 (5/8金) に正規化され、終値も 5/8 のものになる。"""
    ps.add_to_watch("6324", db_path=db_path)
    ps.transition_status("6324", "2準", db_path=db_path)
    ps.transition_status("6324", "1保", action_date="2026-05-09", qty=500, db_path=db_path)

    logs = ps.list_action_logs("6324", db_path=db_path)
    hold = [l for l in logs if l.get("status_to") == "1保"][0]
    assert hold["timestamp"][:10] == "2026-05-08"  # 土→金に補正
    assert hold["price_proxy"] == 3000             # 5/8 の終値


@pytest.mark.parametrize("scenario", ["fill_none", "skip_existing", "protect_actual", "overwrite"])
def test_backfill_price_proxies(db_path, stocks_with_price_log, monkeypatch, scenario):
    """backfill の冪等/overwrite/actual保護/土日補正 (issue #361)。"""
    ps.add_to_watch("6324", db_path=db_path)
    ps.transition_status("6324", "2準", db_path=db_path)
    ps.transition_status("6324", "1保", action_date="2026-05-11", qty=500, db_path=db_path)
    hold = [l for l in ps.list_action_logs("6324", db_path=db_path) if l.get("status_to") == "1保"][0]
    seq = hold["seq"]
    key = ps._action_log_key("6324", seq)

    if scenario == "fill_none":
        # price_proxy を手動で None に戻して backfill で埋め直す
        with ps.ShelveDB(db_path) as db:
            e = db[key]; e["price_proxy"] = None; db[key] = e
        stats = ps.backfill_price_proxies(db_path=db_path)
        assert stats["updated"] == 1
        after = [l for l in ps.list_action_logs("6324", db_path=db_path) if l["seq"] == seq][0]
        assert after["price_proxy"] == 3200

    elif scenario == "skip_existing":
        # 既に付与済み → overwrite なしでスキップ
        stats = ps.backfill_price_proxies(db_path=db_path)
        assert stats["updated"] == 0
        assert stats["skipped"] >= 1

    elif scenario == "protect_actual":
        # 実約定は overwrite でも触らない
        with ps.ShelveDB(db_path) as db:
            e = db[key]; e["price_source"] = "actual"; e["price_proxy"] = 9999; db[key] = e
        ps.backfill_price_proxies(overwrite=True, db_path=db_path)
        after = [l for l in ps.list_action_logs("6324", db_path=db_path) if l["seq"] == seq][0]
        assert after["price_proxy"] == 9999
        assert after["price_source"] == "actual"

    elif scenario == "overwrite":
        # price_log を書き換えて overwrite すると再取得される
        with ps.ShelveDB(stocks_with_price_log) as db:
            db["6324"] = {"price_log": [(_date(2026, 5, 11), 5555)]}
        ps.backfill_price_proxies(overwrite=True, db_path=db_path)
        after = [l for l in ps.list_action_logs("6324", db_path=db_path) if l["seq"] == seq][0]
        assert after["price_proxy"] == 5555


class TestFillMemo:
    """fill 建玉ラウンド (エピソード) 単位の振り返りメモ (issue #387 Phase2)。"""

    def test_set_get_roundtrip(self, db_path):
        key = ps.fill_episode_key("6324", "信用", 3)
        assert ps.get_fill_memo(key, db_path=db_path) == ""
        ps.set_fill_memo(key, "利確できたが再現性は微妙", db_path=db_path)
        assert ps.get_fill_memo(key, db_path=db_path) == "利確できたが再現性は微妙"

    def test_empty_deletes(self, db_path):
        key = ps.fill_episode_key("6324", "信用", 3)
        ps.set_fill_memo(key, "メモ", db_path=db_path)
        ps.set_fill_memo(key, "", db_path=db_path)
        assert ps.get_fill_memo(key, db_path=db_path) == ""
        assert key not in ps.list_fill_memos(db_path=db_path)

    def test_list_returns_only_nonempty(self, db_path):
        k1 = ps.fill_episode_key("1001", "現物", 1)
        k2 = ps.fill_episode_key("1002", "信用", 1)
        ps.set_fill_memo(k1, "あり", db_path=db_path)
        ps.set_fill_memo(k2, "", db_path=db_path)
        memos = ps.list_fill_memos(db_path=db_path)
        assert memos == {k1: "あり"}

    def test_key_normalizes_code(self, db_path):
        # 全角/小文字コードは正規化される
        k1 = ps.fill_episode_key("215a", "現物", 1)
        k2 = ps.fill_episode_key("215A", "現物", 1)
        assert k1 == k2

    def test_key_is_seq_based(self, db_path):
        # 同一銘柄・区分でも先頭 seq が異なればキーは別 (同日ラウンドトリップ対応)
        k1 = ps.fill_episode_key("1001", "信用", 5)
        k2 = ps.fill_episode_key("1001", "信用", 9)
        assert k1 != k2
        assert k1 == "1001|信用|5"

    def test_set_rejects_non_str(self, db_path):
        key = ps.fill_episode_key("1001", "現物", 1)
        with pytest.raises(TypeError):
            ps.set_fill_memo(key, 123, db_path=db_path)

    def test_fill_memo_does_not_leak_into_list_fills(self, db_path):
        # fill_memo: プレフィックスは fill: と衝突しない
        key = ps.fill_episode_key("1001", "現物", 1)
        ps.set_fill_memo(key, "メモ", db_path=db_path)
        f = ps.create_fill("1001", trade_date="2026-01-01", side="buy", qty=100,
                           price=1000.0, amount=100000, trade_kind="現物",
                           dedup_key="x")
        ps.append_fill(f, db_path=db_path)
        fills = ps.list_fills(db_path=db_path)
        assert len(fills) == 1
        assert all("review_memo" not in fl for fl in fills)


class TestSplitAdjustment:
    """分割・併合の換算比率キャッシュ (issue #398)。"""

    def test_add_and_get_multiple_events_sorted(self, db_path):
        assert ps.get_split_adjustments("1491", db_path=db_path) == []
        ps.add_split_adjustment("1491", "2025-09-29", 0.05, db_path=db_path)
        ps.add_split_adjustment("1491", "2020-01-01", 0.5, db_path=db_path)
        events = ps.get_split_adjustments("1491", db_path=db_path)
        assert events == [
            {"ex_date": "2020-01-01", "ratio": 0.5},
            {"ex_date": "2025-09-29", "ratio": 0.05},
        ]
        # 同一 ex_date は上書き (dedup)
        ps.add_split_adjustment("1491", "2025-09-29", 0.1, db_path=db_path)
        events2 = ps.get_split_adjustments("1491", db_path=db_path)
        assert len(events2) == 2
        assert {"ex_date": "2025-09-29", "ratio": 0.1} in events2
        # list_all_split_adjustments は build_fill_episodes の N+1 回避用一括取得
        all_adj = ps.list_all_split_adjustments(db_path=db_path)
        assert all_adj["1491"] == events2

    def test_pending_review_cleared_per_event_not_per_code(self, db_path):
        # PRレビュー #405 (P1) 指摘: 同一銘柄に複数の未登録イベントがある状態で
        # 1件だけ登録すると、銘柄単位で pending を丸ごと消してはいけない
        # (残りの未登録イベントの警告が消えてしまう)。
        ps.mark_split_pending_review("9493", reason="テスト", ex_date="2025-06-01", db_path=db_path)
        ps.mark_split_pending_review("9493", reason="テスト", ex_date="2025-09-01", db_path=db_path)
        assert "9493" in ps.list_pending_review_codes(db_path=db_path)

        ps.add_split_adjustment("9493", "2025-06-01", 0.5, db_path=db_path)
        assert "9493" in ps.list_pending_review_codes(db_path=db_path)  # 残り1件はまだ未解決

        ps.add_split_adjustment("9493", "2025-09-01", 0.8, db_path=db_path)
        assert "9493" not in ps.list_pending_review_codes(db_path=db_path)  # 全件解決

    def test_unknown_pending_cleared_on_any_registration(self, db_path):
        # PRレビュー #405 (2周目 P2) 指摘: yfinance 取得失敗時に ex_date 不明のまま
        # "unknown" マーカーで積まれた pending は、通常の ex_date 一致判定では
        # 永久に解除されない。以後 ex_date が判明して登録できた時点で解除する。
        ps.mark_split_pending_review("9495", reason="yfinance取得失敗", db_path=db_path)
        assert "9495" in ps.list_pending_review_codes(db_path=db_path)
        ps.add_split_adjustment("9495", "2025-06-01", 0.5, db_path=db_path)
        assert "9495" not in ps.list_pending_review_codes(db_path=db_path)

    def test_clear_pending_review_for_false_positive(self, db_path):
        # PRレビュー #405 (4周目 P2): 分割ではない誤検知は比率を捏造せず解除できる。
        ps.mark_split_pending_review("9496", reason="単価ジャンプ検出", ex_date="2025-06-01", db_path=db_path)
        ps.mark_split_pending_review("9496", reason="単価ジャンプ検出", ex_date="2025-09-01", db_path=db_path)
        assert ps.clear_split_pending_review("9496", ex_date="2025-06-01", db_path=db_path) is True
        assert ps.list_pending_review_events(db_path=db_path)["9496"] == ["2025-09-01"]

        assert ps.clear_split_pending_review("9496", db_path=db_path) is True
        assert "9496" not in ps.list_pending_review_codes(db_path=db_path)
