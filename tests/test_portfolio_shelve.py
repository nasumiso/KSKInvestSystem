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

    @pytest.mark.parametrize("preexisting_origin", ["", "ゆーさく"])
    def test_add_to_watch_ensures_research_record(
        self, db_path, tmp_path, monkeypatch, preexisting_origin
    ):
        """監視登録すると research レコードも作られ、既存レコードは上書きされない。

        research 未登録だと銘柄ページがメモ編集に入れず (追加プロンプトが出るだけ)、
        イナゴ元の保存先が存在しなくなるため、ここで不変条件を担保する。
        """
        import research_shelve as rs

        research_path = str(tmp_path / "ensure_research_shelve")
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", research_path)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", research_path)

        if preexisting_origin:
            rec = rs.create_research_record("4377", "ワンキャリア",
                                            inago_origin=preexisting_origin)
            rs.upsert_research_record(rec, db_path=research_path)

        ps.add_to_watch("4377", db_path=db_path)

        research = rs.get_research_record("4377", db_path=research_path)
        assert research is not None
        # 既存があれば値は保たれ、無ければ空で作られる
        assert (research.get("inago_origin") or "") == preexisting_origin

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

    def test_transition_to_1ho_removes_pending_in(self, db_path):
        ps.add_to_watch("4377", db_path=db_path)
        ps.upsert_pending_in("4377", 100, "2026-08-11", db_path=db_path)

        ps.transition_status("4377", "1保", db_path=db_path)

        assert ps.list_pending_in(db_path=db_path) == []

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

    def test_position_latest_as_of(self, db_path):
        """保有株数の基準日は取込済み position_source の as_of 最大値 (未取込なら None)"""
        assert ps.get_position_latest_as_of(db_path=db_path) is None

        ps.upsert_position_source("楽天", "特定", "現物", as_of="2026-08-10", row_count=3, db_path=db_path)
        ps.upsert_position_source("SBI", "特定", "現物", as_of="2026-08-13", row_count=1, db_path=db_path)
        # 部分更新 (片方だけ新しいCSV) でも最新の基準日を返す
        assert ps.get_position_latest_as_of(db_path=db_path) == "2026-08-13"


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

    def test_seed_trade_ideas_adds_missing_default_to_existing_master(self, db_path_ti):
        """既存マスターにも、後から追加された既定戦略を補完する。"""
        ps.create_trade_idea("独自戦略", db_path=db_path_ti)
        count = ps.seed_trade_ideas(db_path=db_path_ti)
        assert count == len(ps._NEW_TRADE_IDEA_SEED_NAMES)
        assert ps.get_trade_idea("中長期ファンダ", db_path=db_path_ti) is not None

    def test_seed_trade_ideas_does_not_restore_deleted_default(self, db_path_ti):
        """初回補完後に削除した既定戦略は、次回表示で復活しない。"""
        ps.seed_trade_ideas(db_path=db_path_ti)
        ps.delete_trade_idea("GARP", db_path=db_path_ti)
        assert ps.seed_trade_ideas(db_path=db_path_ti) == 0
        assert ps.get_trade_idea("GARP", db_path=db_path_ti) is None

    def test_seed_trade_ideas_does_not_restore_all_deleted_defaults(self, db_path_ti):
        """全戦略を削除しても、初回補完済みなら既定戦略を復活させない。"""
        ps.seed_trade_ideas(db_path=db_path_ti)
        for item in ps.list_trade_ideas(db_path=db_path_ti):
            ps.delete_trade_idea(item["name"], db_path=db_path_ti)
        assert ps.seed_trade_ideas(db_path=db_path_ti) == 0
        assert ps.list_trade_ideas(db_path=db_path_ti) == []


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


def test_action_log_source_defaults_manual_and_can_be_csv_import(db_path):
    """反映元 (issue #397) は既定で manual、CSV取込時のみ csv_import になる。

    既存ログ (source フィールドが無い旧データ相当) は list 側で manual 補完される。
    """
    ps.add_to_watch("6324", db_path=db_path)  # source 未指定 -> manual
    ps.transition_status("6324", "2準", db_path=db_path)
    ps.transition_status(
        "6324", "1保", qty=500,
        source="csv_import", source_detail="楽天/信用/2026-08-10",
        db_path=db_path,
    )
    logs = ps.list_action_logs("6324", db_path=db_path)
    assert logs[0]["source"] == "manual"
    hold = [l for l in logs if l.get("status_to") == "1保"][0]
    assert hold["source"] == "csv_import"
    assert hold["source_detail"] == "楽天/信用/2026-08-10"

    with pytest.raises(ValueError):
        ps.append_action_log("6324", "株数変更", source="invalid")


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


class TestEpisodeStrategy:
    """エピソードへの戦略ひもづけ (issue #419)"""

    @pytest.fixture
    def seeded_db(self, db_path):
        ps.seed_trade_ideas(db_path=db_path)
        return db_path

    def test_set_requires_registered_master(self, seeded_db):
        # 集計キーなので未登録値は拒否する (fill_memo の自由文字列とは違う)
        key = ps.fill_episode_key("1001", "信用", 1)
        with pytest.raises(ValueError):
            ps.set_episode_strategy(key, "存在しない戦略", db_path=seeded_db)
        ps.set_episode_strategy(key, "GARP", db_path=seeded_db)
        assert ps.get_episode_strategy(key, db_path=seeded_db)["trade_idea"] == "GARP"

    @pytest.mark.parametrize("first_tate,second_tate,expect_stale", [
        (None, "2026-01-05", True),      # 建日が後付け -> 姿が変わるので要再確認
        ("2026-01-05", "2026-01-05", False),  # 既に入っている -> 冪等再取込で誤発火しない
        (None, None, False),             # 後続CSVにも建日が無い -> 変化なし
    ])
    def test_tate_date_backfill_marks_drift(self, seeded_db, first_tate,
                                            second_tate, expect_stale):
        # 建日は carry_over 判定と hold_days を決めるが、指紋は seq 列のハッシュなので
        # 後付けでは変化せず drift を検出できない。取込側で印を立てる (issue #419 レビュー)
        key = ps.fill_episode_key("1001", "信用", 1)
        common = dict(trade_date="2026-01-20", side="sell", qty=100, price=1000.0,
                      amount=100000, trade_kind="信用返済", dedup_key="dup-1")
        ps.append_fill(ps.create_fill("1001", tate_date=first_tate, **common),
                       db_path=seeded_db)
        ps.set_episode_strategy(key, "GARP", fingerprint="abc123def456",
                                hold_days=15, db_path=seeded_db)
        ps.append_fill(ps.create_fill("1001", tate_date=second_tate, **common),
                       db_path=seeded_db)
        fp = ps.get_episode_strategy(key, db_path=seeded_db)["fingerprint"]
        assert (fp == ps._FINGERPRINT_STALE) is expect_stale

    def test_empty_deletes_key_not_stores_blank(self, seeded_db):
        # 「未分類 = キーが存在しない」の1通りだけ。空文字レコードを残すと
        # 一括付与 (未設定のみ埋める) から漏れて二度と拾えなくなる
        key = ps.fill_episode_key("1001", "信用", 1)
        ps.set_episode_strategy(key, "GARP", db_path=seeded_db)
        ps.set_episode_strategy(key, "", db_path=seeded_db)
        assert ps.get_episode_strategy(key, db_path=seeded_db) is None
        assert key not in ps.list_episode_strategies(db_path=seeded_db)

    @pytest.mark.parametrize("time_horizon,hold_days,expected", [
        ("短期", 5, True),
        ("短期", 40, False),      # 短期戦略が40日保有は矛盾
        ("中期", 3, False),       # 中期戦略が3日で手仕舞いは矛盾
        ("中期", 90, True),
        ("中長期", 10, False),    # 中長期戦略が10日回転は矛盾
        ("中長期", 200, True),
        ("恒常", 1, True),        # 恒常は制限なし
        ("中期", None, True),     # 保有中は判定できないので通す
    ])
    def test_hold_days_consistency(self, time_horizon, hold_days, expected):
        assert ps.is_hold_days_consistent(time_horizon, hold_days) is expected

    def test_master_rename_and_delete_cascade(self, seeded_db):
        # rename は追従 (旧名が残ると戦略別テーブルで成績が分断される)、
        # delete はキーごと削除して未分類に戻す
        key = ps.fill_episode_key("1001", "信用", 1)
        ps.set_episode_strategy(key, "GARP", db_path=seeded_db)
        ps.update_trade_idea("GARP", new_name="GARP改", db_path=seeded_db)
        assert ps.get_episode_strategy(key, db_path=seeded_db)["trade_idea"] == "GARP改"

        ps.delete_trade_idea("GARP改", db_path=seeded_db)
        assert ps.get_episode_strategy(key, db_path=seeded_db) is None

    def test_time_horizon_change_drops_only_inconsistent_seeds(self, seeded_db):
        # time_horizon を変えると既存 seed が新定義と矛盾しうる。seed は未分類へ
        # 戻すが、人が確認した manual は機械が覆さない
        seed_key = ps.fill_episode_key("1001", "信用", 1)
        manual_key = ps.fill_episode_key("1002", "信用", 1)
        ps.set_episode_strategy(seed_key, "GARP", source="seed",
                                hold_days=200, db_path=seeded_db)
        ps.set_episode_strategy(manual_key, "GARP", source="manual",
                                hold_days=200, db_path=seeded_db)

        # 中長期 (>=30日) → 短期 (<=20日) にすると 200日保有は矛盾になる
        ps.update_trade_idea("GARP", time_horizon="短期", db_path=seeded_db)

        assert ps.get_episode_strategy(seed_key, db_path=seeded_db) is None
        assert ps.get_episode_strategy(manual_key, db_path=seeded_db) is not None

    def test_fingerprint_changes_when_episode_splits(self):
        # 遡り取込でラウンドが分裂し fill が抜けたら指紋は必ず変わる
        # (open_date+先頭seq では検知できないケース)
        assert (ps.episode_fingerprint([{"seq": 1}, {"seq": 2}, {"seq": 3}])
                != ps.episode_fingerprint([{"seq": 2}, {"seq": 3}]))


class TestPositionLayer:
    """position/position_source レイヤーのテスト (issue #397 Phase1)。

    merged_qty の合算・売建の除外・covered 判定 (全ソース同一 as_of で揃うか) を検証する。
    """

    def _fill_all_sources(self, db_path, as_of="2026-08-10"):
        for broker, kind in ps.EXPECTED_POSITION_SOURCES:
            ps.upsert_position_source(broker, "特定", kind, as_of=as_of, row_count=1, db_path=db_path)

    def test_merged_qty_sums_and_excludes_short(self, db_path):
        # 402A: 楽天現物600 + 楽天信用900 = 1500 (実データ相当、issue #397 §2-3)
        ps.upsert_position("楽天", "特定", "現物", "402A", 600, as_of="2026-08-10", db_path=db_path)
        ps.upsert_position("楽天", "特定", "信用", "402A", 900, as_of="2026-08-10", db_path=db_path)
        assert ps.compute_merged_qty("402A", db_path=db_path) == 1500
        # 信用売建 (空売り) は集計から除外する (issue #397 §2-0)
        ps.upsert_position("楽天", "特定", "信用売建", "1001", 100, as_of="2026-08-10", db_path=db_path)
        assert ps.compute_merged_qty("1001", db_path=db_path) == 0

    def test_upsert_position_overwrites_not_accumulates(self, db_path):
        # position はスナップショットなので同一キーは最新で上書き (fill と違い履歴を持たない)
        ps.upsert_position("楽天", "特定", "現物", "1001", 100, as_of="2026-08-09", db_path=db_path)
        ps.upsert_position("楽天", "特定", "現物", "1001", 150, as_of="2026-08-10", db_path=db_path)
        assert ps.compute_merged_qty("1001", db_path=db_path) == 150
        assert len(ps.list_positions("1001", db_path=db_path)) == 1

    @pytest.mark.parametrize(
        "setup_sources,setup_short,expected",
        [
            ("all", False, True),      # 全4ソース揃い・売建なし -> covered
            ("partial", False, False),  # ソース不足 -> covered=false
            ("all", True, False),       # 全ソース揃っていても売建があれば covered=false
        ],
    )
    def test_is_covered(self, db_path, setup_sources, setup_short, expected):
        if setup_sources == "all":
            self._fill_all_sources(db_path)
        else:
            ps.upsert_position_source("楽天", "特定", "現物", as_of="2026-08-10", row_count=1, db_path=db_path)
        if setup_short:
            ps.upsert_position("楽天", "特定", "信用売建", "1001", 100, as_of="2026-08-10", db_path=db_path)
        assert ps.is_covered("1001", db_path=db_path) is expected

    def test_is_covered_true_even_when_as_of_differs(self, db_path):
        # 基準日が揃っていなくても4ソース全てあれば covered=true
        # (issue #397 Phase3b: 楽天のみ更新・SBIは前回分を引き継ぐ部分更新を許容するため)
        for broker, kind in ps.EXPECTED_POSITION_SOURCES:
            as_of = "2026-08-09" if (broker, kind) == ("楽天", "信用") else "2026-08-10"
            ps.upsert_position_source(broker, "特定", kind, as_of=as_of, row_count=1, db_path=db_path)
        assert ps.is_covered("1001", db_path=db_path) is True
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

    def test_rejects_non_finite_ratio(self, db_path):
        # PRレビュー #405 (5周目 P2): nan/inf は float 変換できても保存してはいけない。
        with pytest.raises(ValueError):
            ps.add_split_adjustment("1491", "2025-09-29", float("nan"), db_path=db_path)
        with pytest.raises(ValueError):
            ps.add_split_adjustment("1491", "2025-09-29", float("inf"), db_path=db_path)
        assert ps.get_split_adjustments("1491", db_path=db_path) == []

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

    @pytest.mark.parametrize("ex_date,expect_remaining", [
        # 実際の権利落ち日は2約定日の間にある。暫定日 (後側の約定日) とは一致しないが、
        # 検出区間を説明できるので解除する。
        ("2025-12-29", []),
        # 区間の外側で登録された場合は、その乖離を説明できないので暫定マーカーを残す。
        ("2026-06-01", ["2026-03-30"]),
    ])
    def test_span_covering_registration_clears_pending(self, db_path, ex_date, expect_remaining):
        # PRレビュー #440 P1: 週足照合は実際の権利落ち日を知らず「後側の約定日」を
        # 暫定 ex_date として積むため、正しい日付を登録しても日付一致では解除されず
        # split_suspect が恒久的に残り、当該エピソードが集計から除外され続けていた。
        ps.mark_split_pending_review(
            "9498", reason="週足の分割調整後終値との乖離率変化",
            ex_date="2026-03-30", span=("2025-08-07", "2026-03-30"), db_path=db_path,
        )
        ps.add_split_adjustment("9498", ex_date, 2.0, db_path=db_path)
        assert ps.list_pending_review_events(db_path=db_path).get("9498", []) == expect_remaining

    def test_reject_pending_review_records_suppression(self, db_path):
        # PRレビュー #405 (5周目 P2): 却下済みイベントは再検出抑止リストに残す。
        ps.mark_split_pending_review("9497", reason="単価ジャンプ検出", ex_date="2025-06-01", db_path=db_path)
        assert ps.reject_split_pending_review("9497", ex_date="2025-06-01", db_path=db_path) is True
        assert "9497" not in ps.list_pending_review_codes(db_path=db_path)
        assert ps.list_rejected_review_events(db_path=db_path)["9497"] == ["2025-06-01"]


class TestExitAlertLifecycle:
    """出口アラート状態の保持・破棄 (PR #409 レビュー)"""

    @pytest.mark.parametrize("leave_status", ["2準", "3監"])
    def test_exit_alert_state_cleared_when_leaving_hold(self, db_path, leave_status):
        # 1保→3監 も許可されているため、2準 だけ消すと 1保→3監→1保 で
        # 旧「防歴」を引き継いでしまう。1保 を離れる全遷移で破棄する。
        ps.add_to_watch("4377", reason="テスト", db_path=db_path)
        ps.transition_status("4377", "1保", db_path=db_path)
        ps.record_exit_alert_event(
            "4377", "cycle-1", {"date": "2026-02-09", "level": "防"}, db_path=db_path
        )
        assert ps.get_exit_alert_state("4377", "cycle-1", db_path=db_path)["triggered"] is True

        ps.transition_status("4377", leave_status, db_path=db_path)
        assert ps.get_exit_alert_state("4377", "cycle-1", db_path=db_path)["triggered"] is False

    def test_exit_alert_state_discarded_on_cycle_mismatch(self, db_path):
        # 戦略A(防記録)→戦略B(違反なし)→戦略A で cycle_id が元へ戻ると、
        # 不一致レコードを消していないと旧 triggered が復活する (PR #409 レビュー)。
        ps.record_exit_alert_event(
            "4377", "pos1|戦略A", {"date": "2026-02-09", "level": "防"}, db_path=db_path
        )
        assert ps.get_exit_alert_state("4377", "pos1|戦略A", db_path=db_path)["triggered"] is True

        ps.get_exit_alert_state("4377", "pos1|戦略B", db_path=db_path)  # 不一致参照で破棄
        assert ps.get_exit_alert_state("4377", "pos1|戦略A", db_path=db_path)["triggered"] is False

    @pytest.mark.parametrize(
        "ma_kind, ma_window, allowed",
        [
            ("day", 50, True),
            ("week", 30, True),
            ("week", 40, True),
            ("day", 20, False),   # evaluate_exit_signal が見ないので黙って無効になる
            ("week", 50, False),
        ],
    )
    def test_exit_rule_ma_window_limited_to_implemented(self, ma_kind, ma_window, allowed):
        rule = {"ma_kind": ma_kind, "ma_window": ma_window}
        if allowed:
            assert ps._validate_exit_rule(rule)["ma_window"] == ma_window
        else:
            with pytest.raises(ValueError, match="ma_window"):
                ps._validate_exit_rule(rule)
