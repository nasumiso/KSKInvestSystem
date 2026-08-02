"""fill 基準の建玉ラウンド・エピソード再構成テスト (issue #387 Phase4b)。

build_fill_episodes / _episode_pl_from_round のロジックを合成 fill で検証する。
現物ラウンド (平均取得単価法)・信用ラウンド (建単価/決済損益)・現引ブリッジ・
部分売り・保有中・分割約定・期首持ち越しの境界を確認する。
"""

import pytest

import portfolio_shelve as ps
from webapp import helpers


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "portfolio")


@pytest.fixture(autouse=True)
def _stub_stock_names(monkeypatch):
    # 銘柄名解決は stocks DB を開かずにコード=名前で返す (N+1 テストの分離)
    monkeypatch.setattr(
        helpers, "_bulk_resolve_stock_names",
        lambda codes: {c: f"銘柄{c}" for c in codes},
    )


def _add(db_path, code_s, trade_date, side, qty, price, *,
         trade_kind="現物", broker="楽天", tate_price=None, settle_pl=None,
         seq_salt=""):
    """テスト用に fill を1件追加する。dedup_key は一意化する。"""
    dedup_key = f"{code_s}|{trade_date}|{side}|{qty}|{price}|{trade_kind}|{seq_salt}"
    fill = ps.create_fill(
        code_s, trade_date=trade_date, side=side, qty=qty, price=price,
        amount=int(qty * price), trade_kind=trade_kind, dedup_key=dedup_key,
        broker=broker, tate_price=tate_price, settle_pl=settle_pl,
    )
    ps.append_fill(fill, db_path=db_path)


class TestGenbaiBridge:
    """現引ブリッジ: 現引 (buy) → 現物売 で 1 現物ラウンドが閉じる。"""

    def test_genbiki_to_uridashi_round(self, db_path):
        # 4369 相当: 現引3 (取得原価3129.12/2678.5/2634.59) → 現物売3 (3150/2618/2642)
        _add(db_path, "4369", "2026-02-05", "buy", 100, 3129.12, trade_kind="現引", seq_salt="a")
        _add(db_path, "4369", "2026-02-05", "buy", 100, 2678.5, trade_kind="現引", seq_salt="b")
        _add(db_path, "4369", "2026-02-05", "buy", 100, 2634.59, trade_kind="現引", seq_salt="c")
        _add(db_path, "4369", "2026-03-09", "sell", 100, 3150.0, seq_salt="d")
        _add(db_path, "4369", "2026-03-13", "sell", 100, 2618.0, seq_salt="e")
        _add(db_path, "4369", "2026-03-19", "sell", 100, 2642.0, seq_salt="f")

        eps = helpers.build_fill_episodes(db_path=db_path)
        genbutsu = [e for e in eps if e["code_s"] == "4369" and e["kind"] == "現物"]
        assert len(genbutsu) == 1
        ep = genbutsu[0]
        assert ep["closed"] is True
        assert ep["qty_peak"] == 300
        assert ep["open_date"] == "2026-02-05"
        assert ep["close_date"] == "2026-03-19"
        # 取得 844,221 / 売却 841,000 → -3,221
        assert ep["pl"]["profit_amount"] == -3221


class TestGenbutsuRound:
    def test_simple_win(self, db_path):
        _add(db_path, "1001", "2026-01-10", "buy", 100, 1000.0, seq_salt="a")
        _add(db_path, "1001", "2026-01-20", "sell", 100, 1200.0, seq_salt="b")
        eps = helpers.build_fill_episodes(db_path=db_path)
        assert len(eps) == 1
        ep = eps[0]
        assert ep["closed"]
        assert ep["pl"]["profit_amount"] == 20000
        assert round(ep["pl"]["return_pct"], 2) == 20.0
        assert ep["pl"]["hold_days"] == 10

    def test_average_cost_partial_sells(self, db_path):
        # 0→200→300→0: 100@1000, 100@1200, 売100@1300, 売200@1400
        _add(db_path, "1002", "2026-01-01", "buy", 100, 1000.0, seq_salt="a")
        _add(db_path, "1002", "2026-01-02", "buy", 100, 1200.0, seq_salt="b")
        _add(db_path, "1002", "2026-01-03", "buy", 100, 1100.0, seq_salt="c")
        _add(db_path, "1002", "2026-01-10", "sell", 100, 1300.0, seq_salt="d")
        _add(db_path, "1002", "2026-01-15", "sell", 200, 1400.0, seq_salt="e")
        eps = helpers.build_fill_episodes(db_path=db_path)
        assert len(eps) == 1
        ep = eps[0]
        assert ep["qty_peak"] == 300
        # avg_cost = (1000+1200+1100)/3 = 1100
        # 実現 = (1300-1100)*100 + (1400-1100)*200 = 20000 + 60000 = 80000
        assert ep["pl"]["profit_amount"] == 80000

    def test_two_rounds_separate(self, db_path):
        # 0→100→0→100→0 で2エピソード
        _add(db_path, "1003", "2026-01-01", "buy", 100, 1000.0, seq_salt="a")
        _add(db_path, "1003", "2026-01-05", "sell", 100, 1100.0, seq_salt="b")
        _add(db_path, "1003", "2026-02-01", "buy", 100, 1200.0, seq_salt="c")
        _add(db_path, "1003", "2026-02-05", "sell", 100, 1150.0, seq_salt="d")
        eps = helpers.build_fill_episodes(db_path=db_path)
        assert len([e for e in eps if e["code_s"] == "1003"]) == 2
        pls = sorted(e["pl"]["profit_amount"] for e in eps)
        assert pls == [-5000, 10000]

    def test_holding_open_round(self, db_path):
        # 買いのみ = 保有中、損益 None
        _add(db_path, "1004", "2026-01-01", "buy", 100, 1000.0, seq_salt="a")
        eps = helpers.build_fill_episodes(db_path=db_path)
        assert len(eps) == 1
        assert eps[0]["closed"] is False
        assert eps[0]["pl"] is None
        assert eps[0]["close_date"] is None


class TestShinyoRound:
    def test_rakuten_credit_uses_tate_price(self, db_path):
        # 信用新規買建 100@1000 → 信用返済売埋 100@1300 (tate_price=1000)
        _add(db_path, "2001", "2026-01-01", "buy", 100, 1000.0, trade_kind="信用新規", seq_salt="a")
        _add(db_path, "2001", "2026-01-10", "sell", 100, 1300.0, trade_kind="信用返済",
             tate_price=1000.0, seq_salt="b")
        eps = helpers.build_fill_episodes(db_path=db_path)
        credit = [e for e in eps if e["kind"] == "信用"]
        assert len(credit) == 1
        # (1300-1000)*100 = 30000
        assert credit[0]["pl"]["profit_amount"] == 30000

    def test_sbi_credit_uses_settle_pl(self, db_path):
        _add(db_path, "2002", "2026-01-01", "buy", 100, 1000.0, trade_kind="信用新規",
             broker="SBI", seq_salt="a")
        _add(db_path, "2002", "2026-01-10", "sell", 100, 1250.0, trade_kind="信用返済",
             broker="SBI", settle_pl=24000, tate_price=None, seq_salt="b")
        eps = helpers.build_fill_episodes(db_path=db_path)
        credit = [e for e in eps if e["kind"] == "信用"]
        assert len(credit) == 1
        assert credit[0]["pl"]["profit_amount"] == 24000

    def test_credit_without_pl_source_is_none(self, db_path):
        # 建単価も決済損益も無い信用返済 → 損益不能
        _add(db_path, "2003", "2026-01-01", "buy", 100, 1000.0, trade_kind="信用新規", seq_salt="a")
        _add(db_path, "2003", "2026-01-10", "sell", 100, 1300.0, trade_kind="信用返済", seq_salt="b")
        eps = helpers.build_fill_episodes(db_path=db_path)
        credit = [e for e in eps if e["kind"] == "信用"]
        assert len(credit) == 1
        assert credit[0]["pl"] is None


class TestCarryOver:
    def test_sell_only_round_is_pl_none(self, db_path):
        # 期首持ち越し: 買い記録が無く売りだけ → クローズ済だが損益不能
        _add(db_path, "3001", "2026-02-20", "sell", 300, 1500.0, seq_salt="a")
        eps = helpers.build_fill_episodes(db_path=db_path)
        assert len(eps) == 1
        assert eps[0]["closed"] is True
        assert eps[0]["pl"] is None


class TestGenbutsuAndShinyoSeparate:
    def test_genbutsu_and_shinyo_are_separate_rounds(self, db_path):
        # 同一銘柄で現物と信用が並行 → 別ラウンド
        _add(db_path, "4001", "2026-01-01", "buy", 100, 1000.0, trade_kind="現物", seq_salt="a")
        _add(db_path, "4001", "2026-01-05", "sell", 100, 1100.0, trade_kind="現物", seq_salt="b")
        _add(db_path, "4001", "2026-01-02", "buy", 100, 1000.0, trade_kind="信用新規", seq_salt="c")
        _add(db_path, "4001", "2026-01-06", "sell", 100, 1200.0, trade_kind="信用返済",
             tate_price=1000.0, seq_salt="d")
        eps = helpers.build_fill_episodes(db_path=db_path)
        kinds = sorted(e["kind"] for e in eps if e["code_s"] == "4001")
        assert kinds == ["信用", "現物"]


class TestEmpty:
    def test_no_fills(self, db_path):
        assert helpers.build_fill_episodes(db_path=db_path) == []
