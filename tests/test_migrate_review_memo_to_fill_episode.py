"""migrate_review_memo_to_fill_episode の移行ロジックテスト (issue #387 Phase2)。"""

import pytest

import portfolio_shelve as ps
from webapp import helpers
import migrate_review_memo_to_fill_episode as mig


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "portfolio")


@pytest.fixture(autouse=True)
def _stub_stock_names(monkeypatch):
    monkeypatch.setattr(
        helpers, "_bulk_resolve_stock_names",
        lambda codes: {c: f"銘柄{c}" for c in codes},
    )


def _add_fill(db_path, code_s, trade_date, side, qty, price, *,
              trade_kind="信用新規", tate_price=None, settle_pl=None, salt=""):
    f = ps.create_fill(code_s, trade_date=trade_date, side=side, qty=qty, price=price,
                       amount=int(qty * price), trade_kind=trade_kind,
                       dedup_key=f"{code_s}|{trade_date}|{side}|{salt}",
                       tate_price=tate_price, settle_pl=settle_pl)
    ps.append_fill(f, db_path=db_path)


def _closed_shinyo_round(db_path, code_s, open_date, close_date, tate=1000.0, sell=1200.0):
    """信用新規買→返済売 で1ラウンドを閉じる合成 fill を仕込む。"""
    _add_fill(db_path, code_s, open_date, "buy", 100, tate, trade_kind="信用新規", salt="b")
    _add_fill(db_path, code_s, close_date, "sell", 100, sell, trade_kind="信用返済",
              tate_price=tate, salt="s")


def _episode_key(db_path, code_s, kind="信用"):
    """build_fill_episodes から対象銘柄・区分のエピソードキーを取る。"""
    eps = helpers.build_fill_episodes(db_path=db_path)
    return next(e["episode_key"] for e in eps if e["code_s"] == code_s and e["kind"] == kind)


class TestMigrate:
    def test_migrates_memo_to_matching_episode(self, db_path):
        _closed_shinyo_round(db_path, "6324", "2026-06-29", "2026-06-30")
        # 売却日と close_date が一致する action_log (review_memo 付き)
        ps.append_action_log("6324", "ステータス変更", status_from="3監", status_to="1保",
                             timestamp="2026-06-29T09:00:00+09:00", db_path=db_path)
        ps.append_action_log("6324", "売却", status_from="1保", status_to="2準",
                             review_memo="強さを買うはあってる", reason="利確",
                             timestamp="2026-06-30T15:00:00+09:00", db_path=db_path)

        key = _episode_key(db_path, "6324")
        summary = mig.migrate(db_path=db_path, dry_run=False)
        assert len(summary["migrated"]) == 1
        assert ps.get_fill_memo(key, db_path=db_path) == "強さを買うはあってる"

    def test_dry_run_does_not_write(self, db_path):
        _closed_shinyo_round(db_path, "6324", "2026-06-29", "2026-06-30")
        ps.append_action_log("6324", "売却", status_from="1保", status_to="2準",
                             review_memo="メモ",
                             timestamp="2026-06-30T15:00:00+09:00", db_path=db_path)
        key = _episode_key(db_path, "6324")
        summary = mig.migrate(db_path=db_path, dry_run=True)
        assert len(summary["migrated"]) == 1
        assert ps.get_fill_memo(key, db_path=db_path) == ""  # 書いていない

    def test_existing_memo_not_overwritten(self, db_path):
        _closed_shinyo_round(db_path, "6324", "2026-06-29", "2026-06-30")
        ps.append_action_log("6324", "売却", status_from="1保", status_to="2準",
                             review_memo="ログ側メモ",
                             timestamp="2026-06-30T15:00:00+09:00", db_path=db_path)
        key = _episode_key(db_path, "6324")
        ps.set_fill_memo(key, "既にある", db_path=db_path)
        summary = mig.migrate(db_path=db_path, dry_run=False)
        assert len(summary["already"]) == 1
        assert ps.get_fill_memo(key, db_path=db_path) == "既にある"  # 上書きしない

    def test_within_tolerance_days_matches(self, db_path):
        # 返済ラグ: 売却日 07-02 に対し close_date 06-30 (差2日) は許容
        _closed_shinyo_round(db_path, "6324", "2026-06-29", "2026-06-30")
        ps.append_action_log("6324", "売却", status_from="1保", status_to="2準",
                             review_memo="ラグあり",
                             timestamp="2026-07-02T15:00:00+09:00", db_path=db_path)
        summary = mig.migrate(db_path=db_path, dry_run=False)
        assert len(summary["migrated"]) == 1

    def test_beyond_tolerance_days_skipped(self, db_path):
        # 許容差を超える (差5日) と対応付けせずスキップ
        _closed_shinyo_round(db_path, "6324", "2026-06-20", "2026-06-25")
        ps.append_action_log("6324", "売却", status_from="1保", status_to="2準",
                             review_memo="遠すぎ",
                             timestamp="2026-06-30T15:00:00+09:00", db_path=db_path)
        summary = mig.migrate(db_path=db_path, dry_run=False)
        assert len(summary["skipped"]) == 1
        assert len(summary["migrated"]) == 0

    def test_no_matching_episode_skipped(self, db_path):
        # fill が無い銘柄のメモはスキップ
        ps.append_action_log("9999", "売却", status_from="1保", status_to="2準",
                             review_memo="対応先なし",
                             timestamp="2026-06-30T15:00:00+09:00", db_path=db_path)
        summary = mig.migrate(db_path=db_path, dry_run=False)
        assert len(summary["skipped"]) == 1
        assert len(summary["migrated"]) == 0
