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
         tate_date=None, seq_salt=""):
    """テスト用に fill を1件追加する。dedup_key は一意化する。"""
    dedup_key = f"{code_s}|{trade_date}|{side}|{qty}|{price}|{trade_kind}|{seq_salt}"
    fill = ps.create_fill(
        code_s, trade_date=trade_date, side=side, qty=qty, price=price,
        amount=int(qty * price), trade_kind=trade_kind, dedup_key=dedup_key,
        broker=broker, tate_price=tate_price, settle_pl=settle_pl,
        tate_date=tate_date,
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

    def test_shinyo_to_genbiki_to_genbutsu_full_flow(self, db_path):
        """信用新規買 → 現引 → 現物売 で、信用建玉が現引で現物へ振り替わる (1436相当)。

        現引で信用建玉が尽きると信用ラウンドは損益なしでクローズ (保有中に残らない)。
        現物ラウンドが現引取得原価 × 現物売で損益確定する。
        """
        _add(db_path, "1436", "2026-05-08", "buy", 300, 1440.0, trade_kind="信用新規", seq_salt="a")
        _add(db_path, "1436", "2026-05-13", "buy", 100, 1810.0, trade_kind="信用新規", seq_salt="b")
        _add(db_path, "1436", "2026-06-08", "buy", 100, 1813.74, trade_kind="現引", seq_salt="c")
        _add(db_path, "1436", "2026-06-08", "buy", 300, 1443.31, trade_kind="現引", seq_salt="d")
        _add(db_path, "1436", "2026-06-09", "sell", 100, 1855.0, trade_kind="現物", seq_salt="e")
        _add(db_path, "1436", "2026-06-16", "sell", 300, 1260.0, trade_kind="現物", seq_salt="f")

        eps = helpers.build_fill_episodes(db_path=db_path)
        # 保有中は残らない (信用建玉は現引で全部振替、現物は全部売却)
        assert all(e["closed"] for e in eps), [e for e in eps if not e["closed"]]
        shinyo = [e for e in eps if e["kind"] == "信用"]
        genbutsu = [e for e in eps if e["kind"] == "現物"]
        assert len(shinyo) == 1
        assert len(genbutsu) == 1
        # 信用ラウンドは現引で閉じたため損益なし (信用として売っていない)
        assert shinyo[0]["pl"] is None
        # 信用ラウンドの終了日は最後の信用新規日 (05-13) ではなく現引日 (06-08) (P2)
        assert shinyo[0]["close_date"] == "2026-06-08"
        assert shinyo[0]["last_trade_date"] == "2026-06-08"
        # 現物: 取得 (1813.74*100 + 1443.31*300) / 売却 (1855*100 + 1260*300)
        # = 181374 + 432993 = 614367 取得, 185500 + 378000 = 563500 売却 → -50,867
        assert genbutsu[0]["pl"]["profit_amount"] == -50867

    def test_genbiki_does_not_leave_open_shinyo(self, db_path):
        """現引で信用建玉が尽きたら信用が保有中に残らない (誤保有バグの回帰)。"""
        _add(db_path, "2001", "2026-01-10", "buy", 100, 1000.0, trade_kind="信用新規", seq_salt="a")
        _add(db_path, "2001", "2026-01-20", "buy", 100, 1050.0, trade_kind="現引", seq_salt="b")
        eps = helpers.build_fill_episodes(db_path=db_path)
        # 信用は現引で振替済み → 信用の保有中は無い。現物は現引100株を保有中
        open_shinyo = [e for e in eps if not e["closed"] and e["kind"] == "信用"]
        open_genbutsu = [e for e in eps if not e["closed"] and e["kind"] == "現物"]
        assert open_shinyo == []
        assert len(open_genbutsu) == 1
        assert open_genbutsu[0]["open_pl"]["held_qty"] == 100

    def test_genbiki_then_same_day_sell_not_open(self, db_path):
        """同日に 現引(買) → 現物売 があるとき、現引を先に処理し保有中に残さない (6366相当)。

        CSVの seq 上は現物売が先に来ても、同日は建玉を作る側 (現引) を先に処理する。
        """
        _add(db_path, "6366", "2026-03-16", "buy", 300, 1051.0, trade_kind="信用新規", seq_salt="a")
        # 同日 05-11: 現物売300 (seq が現引より先) と 現引300
        _add(db_path, "6366", "2026-05-11", "sell", 300, 753.1, trade_kind="現物", seq_salt="b")
        _add(db_path, "6366", "2026-05-11", "buy", 300, 1061.62, trade_kind="現引", seq_salt="c")
        eps = helpers.build_fill_episodes(db_path=db_path)
        # 現引で現物化した300株を同日売却 → 現物は保有中に残らない
        open_genbutsu = [e for e in eps if not e["closed"] and e["kind"] == "現物"]
        assert open_genbutsu == [], open_genbutsu


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
        sell = next(f for f in ep["fills"] if f["side"] == "sell")
        assert sell["fill_pl"] == 20000
        assert sell["fill_return_pct"] == 20.0

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
        sells = [f for f in ep["fills"] if f["side"] == "sell"]
        assert [f["fill_pl"] for f in sells] == [20000, 60000]
        assert [f["fill_return_pct"] for f in sells] == pytest.approx(
            [1300 / 1100 * 100 - 100, 1400 / 1100 * 100 - 100]
        )

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
             tate_date="2026-01-01", tate_price=1000.0, seq_salt="b")
        eps = helpers.build_fill_episodes(db_path=db_path)
        credit = [e for e in eps if e["kind"] == "信用"]
        assert len(credit) == 1
        # (1300-1000)*100 = 30000
        assert credit[0]["pl"]["profit_amount"] == 30000
        settle = next(f for f in credit[0]["fills"] if f["trade_kind"] == "信用返済")
        assert settle["fill_pl"] == 30000
        assert settle["fill_return_pct"] == 30.0
        assert settle["hold_days"] == 9

    def test_sbi_credit_uses_settle_pl(self, db_path):
        _add(db_path, "2002", "2026-01-01", "buy", 100, 1000.0, trade_kind="信用新規",
             broker="SBI", seq_salt="a")
        _add(db_path, "2002", "2026-01-10", "sell", 100, 1250.0, trade_kind="信用返済",
             broker="SBI", settle_pl=24000, tate_price=None, seq_salt="b")
        eps = helpers.build_fill_episodes(db_path=db_path)
        credit = [e for e in eps if e["kind"] == "信用"]
        assert len(credit) == 1
        assert credit[0]["pl"]["profit_amount"] == 24000
        settle = next(f for f in credit[0]["fills"] if f["trade_kind"] == "信用返済")
        assert settle["fill_pl"] == 24000
        assert "fill_return_pct" not in settle
        assert "hold_days" not in settle

    def test_credit_without_pl_source_is_none(self, db_path):
        # 建単価も決済損益も無い信用返済 → 損益不能
        _add(db_path, "2003", "2026-01-01", "buy", 100, 1000.0, trade_kind="信用新規", seq_salt="a")
        _add(db_path, "2003", "2026-01-10", "sell", 100, 1300.0, trade_kind="信用返済", seq_salt="b")
        eps = helpers.build_fill_episodes(db_path=db_path)
        credit = [e for e in eps if e["kind"] == "信用"]
        assert len(credit) == 1
        assert credit[0]["pl"] is None

    def test_pre_import_repayment_does_not_close_current_round(self, db_path):
        """取込前建玉の返済は、当期に新規で建てた信用玉と相殺しない。"""
        _add(db_path, "7089", "2026-02-20", "buy", 200, 1339.0,
             trade_kind="信用新規", seq_salt="new")
        _add(db_path, "7089", "2026-02-20", "sell", 100, 1339.0,
             trade_kind="信用返済", tate_date="2025-08-21", tate_price=955.0,
             seq_salt="old-a")
        _add(db_path, "7089", "2026-03-03", "sell", 100, 1304.0,
             trade_kind="信用返済", tate_date="2025-12-29", tate_price=975.0,
             seq_salt="old-b")
        _add(db_path, "7089", "2026-05-12", "sell", 200, 1550.0,
             trade_kind="信用返済", tate_date="2026-02-20", tate_price=1339.0,
             seq_salt="settle")

        eps = helpers.build_fill_episodes(db_path=db_path)
        current = [e for e in eps if e["kind"] == "信用" and not e["carry_over"]]
        carry_over = [e for e in eps if e["kind"] == "信用" and e["carry_over"]]

        assert len(current) == 1
        assert current[0]["open_date"] == "2026-02-20"
        assert current[0]["close_date"] == "2026-05-12"
        assert current[0]["qty_peak"] == 200
        assert current[0]["pl"]["profit_amount"] == 42200
        assert len(carry_over) == 2
        assert {e["open_date"] for e in carry_over} == {"2025-08-21", "2025-12-29"}


class TestCarryOver:
    def test_sell_only_round_is_pl_none(self, db_path):
        # 期首持ち越し: 買い記録が無く売りだけ → クローズ済だが損益不能
        _add(db_path, "3001", "2026-02-20", "sell", 300, 1500.0, seq_salt="a")
        eps = helpers.build_fill_episodes(db_path=db_path)
        assert len(eps) == 1
        assert eps[0]["closed"] is True
        assert eps[0]["pl"] is None


class TestShortRound:
    """信用売建 (空売り): 新規売で建て、返済買で閉じる。損益の符号は買建と逆。"""

    @pytest.mark.parametrize("settle_pl,tate_price,expected", [
        (None, 1500.0, 20000),    # 楽天形式: (建1500 - 買戻1300) * 100 = +20000
        (-176534, None, -176534),  # SBI形式: settle_pl をそのまま採用
    ])
    def test_short_round_pl(self, db_path, settle_pl, tate_price, expected):
        _add(db_path, "5001", "2026-01-05", "sell", 100, 1500.0,
             trade_kind="信用新規", seq_salt="a")
        _add(db_path, "5001", "2026-01-09", "buy", 100, 1300.0,
             trade_kind="信用返済", tate_price=tate_price, settle_pl=settle_pl,
             seq_salt="b")
        eps = helpers.build_fill_episodes(db_path=db_path)
        assert len(eps) == 1
        ep = eps[0]
        assert ep["is_short"] is True
        assert ep["closed"] is True
        assert ep["qty_peak"] == 100
        assert ep["pl"]["profit_amount"] == expected
        settle = next(f for f in ep["fills"] if f["trade_kind"] == "信用返済")
        assert settle["fill_pl"] == expected
        if tate_price is not None:
            assert settle["fill_return_pct"] == expected / (tate_price * 100) * 100
        else:
            assert "fill_return_pct" not in settle

    def test_short_and_long_are_separate_rounds(self, db_path):
        # 両建て: 売建が買建の建玉を打ち消してラウンドを誤って閉じないこと
        _add(db_path, "5002", "2026-01-05", "sell", 100, 1500.0,
             trade_kind="信用新規", seq_salt="a")
        _add(db_path, "5002", "2026-01-06", "buy", 100, 1000.0,
             trade_kind="信用新規", seq_salt="b")
        _add(db_path, "5002", "2026-01-09", "buy", 100, 1300.0,
             trade_kind="信用返済", tate_price=1500.0, seq_salt="c")
        _add(db_path, "5002", "2026-01-10", "sell", 100, 1200.0,
             trade_kind="信用返済", tate_price=1000.0, seq_salt="d")
        eps = helpers.build_fill_episodes(db_path=db_path)
        assert len(eps) == 2
        short = [e for e in eps if e["is_short"]][0]
        long_ = [e for e in eps if not e["is_short"]][0]
        # 売建: (1500-1300)*100 / 買建: (1200-1000)*100
        assert short["pl"]["profit_amount"] == 20000
        assert long_["pl"]["profit_amount"] == 20000
        assert short["closed"] is True and long_["closed"] is True

    def test_open_short_unrealized_is_inverted(self, db_path):
        # 保有中の売建: 現在値が建単価より下なら含み益
        _add(db_path, "5003", "2026-01-05", "sell", 100, 1500.0,
             trade_kind="信用新規", seq_salt="a")
        eps = helpers.build_fill_episodes(db_path=db_path)
        ep = eps[0]
        assert ep["closed"] is False
        assert helpers._episode_open_pl(ep, 1300.0)["unrealized"] == 20000
        assert helpers._episode_open_pl(ep, 1700.0)["unrealized"] == -20000

    def test_pre_import_short_repayment_is_closed_carry_over(self, db_path):
        """新規売が取込範囲に無い返済買は、保有中の買建にしない。"""
        _add(db_path, "5004", "2026-01-05", "buy", 100, 1300.0,
             trade_kind="信用返済", settle_pl=20000, broker="SBI", seq_salt="a")
        eps = helpers.build_fill_episodes(db_path=db_path)
        assert len(eps) == 1
        ep = eps[0]
        assert ep["closed"] is True
        assert ep["carry_over"] is True
        assert ep["is_short"] is True
        assert ep["pl"]["profit_amount"] == 20000

    def test_same_day_short_open_is_processed_before_settlement(self, db_path):
        """同日の新規売→返済買は、返済買を期首持越し扱いにしない。"""
        _add(db_path, "5005", "2026-01-05", "sell", 100, 1500.0,
             trade_kind="信用新規", seq_salt="a")
        _add(db_path, "5005", "2026-01-05", "buy", 100, 1300.0,
             trade_kind="信用返済", settle_pl=20000, broker="SBI", seq_salt="b")
        eps = helpers.build_fill_episodes(db_path=db_path)
        assert len(eps) == 1
        assert eps[0]["closed"] is True
        assert eps[0]["carry_over"] is False
        assert eps[0]["is_short"] is True


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


class TestOpenPositionPL:
    """保有中エピソードの実現損益 (部分売り) と含み損益 (残玉評価)。"""

    def test_genbutsu_partial_sell_realized_and_unrealized(self, db_path, monkeypatch):
        # 現物 0→200 で100株@1500売却 (実現)、残100株、現在値1400 (含み)
        _add(db_path, "7001", "2026-01-10", "buy", 200, 1000.0, seq_salt="a")
        _add(db_path, "7001", "2026-01-20", "sell", 100, 1500.0, seq_salt="b")
        # 現在値 = price_log 最新終値をモック
        import datetime as _dt
        monkeypatch.setattr(
            helpers, "_bulk_price_logs",
            lambda codes: {"7001": [(_dt.date(2026, 1, 31), 1400)]},
        )
        eps = helpers.build_fill_episodes(db_path=db_path)
        ep = eps[0]
        assert ep["closed"] is False
        op = ep["open_pl"]
        assert op["realized"] == 50000     # (1500-1000)*100
        assert op["held_qty"] == 100
        assert op["unrealized"] == 40000   # (1400-1000)*100

    def test_open_without_price_has_none_unrealized(self, db_path, monkeypatch):
        _add(db_path, "7002", "2026-01-10", "buy", 100, 1000.0, seq_salt="a")
        monkeypatch.setattr(helpers, "_bulk_price_logs", lambda codes: {"7002": []})
        eps = helpers.build_fill_episodes(db_path=db_path)
        op = eps[0]["open_pl"]
        assert op["realized"] == 0
        assert op["unrealized"] is None    # 現在値なし
        assert op["held_qty"] == 100

    def test_shinyo_reverse_settle_disables_unrealized(self, db_path, monkeypatch):
        # 信用返済 buy (建玉方向と逆) が混ざると含みは None (安全側)
        _add(db_path, "7003", "2026-01-10", "buy", 100, 6000.0, trade_kind="信用新規", seq_salt="a")
        _add(db_path, "7003", "2026-01-12", "buy", 100, 6100.0, trade_kind="信用返済", seq_salt="b")
        import datetime as _dt
        monkeypatch.setattr(
            helpers, "_bulk_price_logs",
            lambda codes: {"7003": [(_dt.date(2026, 1, 31), 6500)]},
        )
        eps = helpers.build_fill_episodes(db_path=db_path)
        op = eps[0]["open_pl"]
        assert op["unrealized"] is None


class TestOrdering:
    def test_sorted_by_last_trade_date(self, db_path):
        # A: 建2026-01-01 売2026-01-05 (最終01-05)
        _add(db_path, "5001", "2026-01-01", "buy", 100, 1000.0, seq_salt="a")
        _add(db_path, "5001", "2026-01-05", "sell", 100, 1100.0, seq_salt="b")
        # B: 建2026-01-02 のまま保有中で 2026-03-01 に買い増し (最終03-01)
        _add(db_path, "5002", "2026-01-02", "buy", 100, 2000.0, seq_salt="c")
        _add(db_path, "5002", "2026-03-01", "buy", 100, 2100.0, seq_salt="d")
        eps = helpers.build_fill_episodes(db_path=db_path)
        # 最終約定日降順 → 保有中(最終03-01)が先、クローズ済(最終01-05)が後
        assert [e["code_s"] for e in eps] == ["5002", "5001"]
        assert eps[0]["last_trade_date"] == "2026-03-01"
        assert eps[0]["closed"] is False


class TestFillDateRangeByBroker:
    """証券会社別の取込済み約定日レンジ (最古〜最新、取込タイミング参考) issue #387。"""

    def test_range_per_broker(self, db_path):
        _add(db_path, "1001", "2026-01-10", "buy", 100, 1000.0, broker="楽天", seq_salt="a")
        _add(db_path, "1001", "2026-07-31", "sell", 100, 1200.0, broker="楽天", seq_salt="b")
        _add(db_path, "2002", "2026-03-04", "buy", 100, 500.0,
             trade_kind="信用新規", broker="SBI", seq_salt="c")
        _add(db_path, "2002", "2026-07-21", "sell", 100, 600.0,
             trade_kind="信用返済", broker="SBI", settle_pl=10000, seq_salt="d")
        ranges = helpers.fill_date_range_by_broker(db_path=db_path)
        assert ranges == {
            "楽天": {"first": "2026-01-10", "last": "2026-07-31"},
            "SBI": {"first": "2026-03-04", "last": "2026-07-21"},
        }

    def test_none_broker_counts_as_rakuten(self, db_path):
        _add(db_path, "1001", "2026-05-01", "buy", 100, 1000.0, broker=None, seq_salt="a")
        ranges = helpers.fill_date_range_by_broker(db_path=db_path)
        assert ranges == {"楽天": {"first": "2026-05-01", "last": "2026-05-01"}}

    def test_empty(self, db_path):
        assert helpers.fill_date_range_by_broker(db_path=db_path) == {}


class TestEmpty:
    def test_no_fills(self, db_path):
        assert helpers.build_fill_episodes(db_path=db_path) == []


class TestBrokerBackfill:
    """既存の楽天取込 fill は broker 未設定 (None)。表示時に「楽天」で補完する (P2)。"""

    def test_none_broker_shown_as_rakuten(self, db_path):
        _add(db_path, "9001", "2026-01-10", "buy", 100, 1000.0, broker=None, seq_salt="a")
        _add(db_path, "9001", "2026-01-20", "sell", 100, 1100.0, broker=None, seq_salt="b")
        eps = helpers.build_fill_episodes(db_path=db_path)
        assert len(eps) == 1
        brokers = {f["broker"] for f in eps[0]["fills"]}
        assert brokers == {"楽天"}

    def test_sbi_broker_preserved(self, db_path):
        _add(db_path, "9002", "2026-01-10", "buy", 100, 1000.0,
             trade_kind="信用新規", broker="SBI", seq_salt="a")
        _add(db_path, "9002", "2026-01-20", "sell", 100, 1250.0,
             trade_kind="信用返済", broker="SBI", settle_pl=24000, seq_salt="b")
        eps = helpers.build_fill_episodes(db_path=db_path)
        brokers = {f["broker"] for f in eps[0]["fills"]}
        assert brokers == {"SBI"}


class TestFillMemo:
    """fill エピソード単位の振り返りメモ (issue #387 Phase2)。"""

    def test_episode_key_uses_first_seq(self, db_path):
        # キーは 銘柄+口座種別+ラウンド先頭 fill の seq
        k = ps.fill_episode_key("1001", "現物", 7)
        assert k == "1001|現物|7"

    def test_key_stable_from_open_to_closed(self, db_path, monkeypatch):
        # P1-2: 保有中に付けたメモが売却後 (close_date 確定) も同じキーで追える
        _add(db_path, "1001", "2026-01-10", "buy", 100, 1000.0, seq_salt="a")
        monkeypatch.setattr(helpers, "_bulk_price_logs", lambda codes: {"1001": []})
        eps_open = helpers.build_fill_episodes(db_path=db_path)
        key_open = eps_open[0]["episode_key"]
        assert eps_open[0]["closed"] is False
        ps.set_fill_memo(key_open, "保有中に書いたメモ", db_path=db_path)
        # 売却してラウンドをクローズ
        _add(db_path, "1001", "2026-01-20", "sell", 100, 1200.0, seq_salt="b")
        eps_closed = helpers.build_fill_episodes(db_path=db_path)
        assert eps_closed[0]["closed"] is True
        # キーが変わらずメモが引き継がれる
        assert eps_closed[0]["episode_key"] == key_open
        assert eps_closed[0]["review_memo"] == "保有中に書いたメモ"

    def test_multiple_rounds_have_distinct_keys(self, db_path):
        # P1-2: 同一銘柄・同一区分で複数ラウンドあってもキーが衝突せず別メモを持てる。
        # date ベースの旧キーでは open/close が近いと衝突しうるが seq ベースなら一意。
        _add(db_path, "1001", "2026-01-10", "buy", 100, 1000.0, trade_kind="信用新規", seq_salt="a")
        _add(db_path, "1001", "2026-01-11", "sell", 100, 1100.0, trade_kind="信用返済",
             tate_price=1000.0, seq_salt="b")
        _add(db_path, "1001", "2026-01-12", "buy", 100, 1050.0, trade_kind="信用新規", seq_salt="c")
        _add(db_path, "1001", "2026-01-13", "sell", 100, 1200.0, trade_kind="信用返済",
             tate_price=1050.0, seq_salt="d")
        eps = helpers.build_fill_episodes(db_path=db_path)
        code_eps = [e for e in eps if e["code_s"] == "1001"]
        assert len(code_eps) == 2
        keys = {e["episode_key"] for e in code_eps}
        assert len(keys) == 2  # 衝突しない
        # 別々のメモを持てる
        k1, k2 = sorted(keys)
        ps.set_fill_memo(k1, "1回目", db_path=db_path)
        ps.set_fill_memo(k2, "2回目", db_path=db_path)
        memos = ps.list_fill_memos(db_path=db_path)
        assert memos[k1] == "1回目"
        assert memos[k2] == "2回目"

    def test_memo_attached_to_episode(self, db_path):
        _add(db_path, "1001", "2026-01-10", "buy", 100, 1000.0, seq_salt="a")
        _add(db_path, "1001", "2026-01-20", "sell", 100, 1200.0, seq_salt="b")
        eps = helpers.build_fill_episodes(db_path=db_path)
        key = eps[0]["episode_key"]
        assert eps[0]["review_memo"] == ""  # 初期は空
        ps.set_fill_memo(key, "利確成功、再現性の検証を", db_path=db_path)
        eps2 = helpers.build_fill_episodes(db_path=db_path)
        assert eps2[0]["review_memo"] == "利確成功、再現性の検証を"

    def test_empty_memo_deletes(self, db_path):
        _add(db_path, "1001", "2026-01-10", "buy", 100, 1000.0, seq_salt="a")
        _add(db_path, "1001", "2026-01-20", "sell", 100, 1200.0, seq_salt="b")
        eps = helpers.build_fill_episodes(db_path=db_path)
        key = eps[0]["episode_key"]
        ps.set_fill_memo(key, "メモ", db_path=db_path)
        assert ps.get_fill_memo(key, db_path=db_path) == "メモ"
        ps.set_fill_memo(key, "  ", db_path=db_path)  # 空白は削除扱い
        assert ps.get_fill_memo(key, db_path=db_path) == ""
        assert key not in ps.list_fill_memos(db_path=db_path)

    def test_memo_survives_fill_reimport(self, db_path):
        # メモは fill と独立レイヤー。fill を作り直しても同一キーなら残る
        _add(db_path, "1001", "2026-01-10", "buy", 100, 1000.0, seq_salt="a")
        _add(db_path, "1001", "2026-01-20", "sell", 100, 1200.0, seq_salt="b")
        eps = helpers.build_fill_episodes(db_path=db_path)
        key = eps[0]["episode_key"]
        ps.set_fill_memo(key, "残るはず", db_path=db_path)
        # 同一 dedup の fill を再追加 (重複スキップされる) してもメモは維持
        _add(db_path, "1001", "2026-01-10", "buy", 100, 1000.0, seq_salt="a")
        eps2 = helpers.build_fill_episodes(db_path=db_path)
        assert eps2[0]["review_memo"] == "残るはず"


class TestSplitAdjustment:
    """株式分割・併合をまたぐ現物エピソードの換算 (issue #398)。

    1491 中外鉱業の実データ相当 (2025-09-29 権利落ち、20株->1株併合)。
    """

    def _add_1491_fills(self, db_path):
        # 併合前 (20:1)
        _add(db_path, "1491", "2025-02-17", "buy", 8000, 45, seq_salt="a")
        _add(db_path, "1491", "2025-04-03", "buy", 3000, 60, seq_salt="b")
        _add(db_path, "1491", "2025-07-11", "sell", 3000, 63, seq_salt="c")
        _add(db_path, "1491", "2025-07-22", "sell", 2000, 65, seq_salt="d")
        _add(db_path, "1491", "2025-09-03", "sell", 2000, 62, seq_salt="e")
        # 併合後
        _add(db_path, "1491", "2025-09-30", "buy", 300, 925, seq_salt="f")
        _add(db_path, "1491", "2025-10-06", "buy", 300, 936, seq_salt="g")
        _add(db_path, "1491", "2025-10-16", "buy", 300, 950, seq_salt="h")
        _add(db_path, "1491", "2025-12-02", "sell", 400, 738, seq_salt="i")
        _add(db_path, "1491", "2025-12-17", "sell", 400, 702, seq_salt="j")
        _add(db_path, "1491", "2025-12-18", "buy", 100, 757, seq_salt="k")
        _add(db_path, "1491", "2025-12-25", "buy", 100, 869, seq_salt="l")
        _add(db_path, "1491", "2025-12-25", "buy", 100, 896, seq_salt="m")
        _add(db_path, "1491", "2026-02-16", "sell", 100, 1118, seq_salt="n")
        _add(db_path, "1491", "2026-02-16", "sell", 100, 1118, seq_salt="o")
        _add(db_path, "1491", "2026-02-25", "sell", 400, 1050, seq_salt="p")

    def test_split_adjustment_closes_episode_at_zero(self, db_path):
        self._add_1491_fills(db_path)
        ps.add_split_adjustment("1491", "2025-09-29", 0.05, db_path=db_path)
        eps = helpers.build_fill_episodes(db_path=db_path)
        genbutsu = [e for e in eps if e["code_s"] == "1491" and e["kind"] == "現物"]
        assert len(genbutsu) == 1
        ep = genbutsu[0]
        assert ep["closed"] is True  # 換算後は残高0株でクローズする
        assert ep["pl"]["profit_amount"] == 27100  # キャッシュフロー検算と一致 (issue #398)

    def test_multiple_split_events_apply_cumulative_ratio(self, db_path):
        # 最古の fill (2025-01-01) は2回のイベント両方の累積比率、
        # 中間の fill (2025-06-01) は2回目のイベントのみ適用される
        _add(db_path, "2491", "2025-01-01", "buy", 100, 10000, seq_salt="a")
        _add(db_path, "2491", "2025-06-01", "buy", 100, 500, seq_salt="b")
        _add(db_path, "2491", "2025-12-01", "sell", 1200, 100, seq_salt="c")
        ps.add_split_adjustment("2491", "2025-03-01", 0.1, db_path=db_path)   # 10:1併合
        ps.add_split_adjustment("2491", "2025-09-01", 2.0, db_path=db_path)   # 1:2分割
        eps = helpers.build_fill_episodes(db_path=db_path)
        ep = [e for e in eps if e["code_s"] == "2491"][0]
        fills = {f["trade_date"]: f for f in ep["fills"]}
        # 2025-01-01: 100株@10000 -> ×0.1×2.0 = ×0.2 -> 20株@50000
        assert fills["2025-01-01"]["qty"] == pytest.approx(20)
        assert fills["2025-01-01"]["price"] == pytest.approx(50000)
        # 2025-06-01: 100株@500 -> ×2.0 (03-01イベントより後なので対象外) -> 200株@250
        assert fills["2025-06-01"]["qty"] == pytest.approx(200)
        assert fills["2025-06-01"]["price"] == pytest.approx(250)
        # 2025-12-01 の売りは両イベントより後なので換算なし
        assert fills["2025-12-01"]["qty"] == 1200

    def test_shinyo_round_not_affected_by_split_adjustment(self, db_path):
        # 同一銘柄に信用ラウンドを混在させ、split_adj 登録後も settle_pl 側の損益が不変
        _add(db_path, "3491", "2025-01-01", "buy", 100, 1000, trade_kind="信用新規", seq_salt="a")
        _add(db_path, "3491", "2025-02-01", "sell", 100, 1100, trade_kind="信用返済",
             settle_pl=9500, seq_salt="b")
        ps.add_split_adjustment("3491", "2025-06-01", 0.5, db_path=db_path)  # 1:2併合
        eps = helpers.build_fill_episodes(db_path=db_path)
        shinyo = [e for e in eps if e["code_s"] == "3491" and e["kind"] == "信用"][0]
        assert shinyo["pl"]["profit_amount"] == 9500  # settle_pl のまま、換算されない
        assert shinyo["fills"][0]["qty"] == 100  # 信用 fill の qty も不変

    def test_genbiki_not_adjusted_so_shinyo_round_closes(self, db_path):
        # 現引の qty は信用新規側の減算にも使われるため、現引だけ換算すると
        # shinyo_qty が0に戻らずクローズを取り逃す (simplifyレビューで発見した回帰)。
        # 現引を換算対象から除外すれば、信用新規と現引が同じ株数基準のままクローズできる。
        _add(db_path, "6491", "2025-01-01", "buy", 300, 1000, trade_kind="信用新規", seq_salt="a")
        _add(db_path, "6491", "2025-06-01", "buy", 300, 1000, trade_kind="現引", seq_salt="b")
        _add(db_path, "6491", "2025-12-01", "sell", 100, 900, seq_salt="c")
        ps.add_split_adjustment("6491", "2025-09-01", 0.833333, db_path=db_path)
        eps = helpers.build_fill_episodes(db_path=db_path)
        shinyo = [e for e in eps if e["code_s"] == "6491" and e["kind"] == "信用"][0]
        assert shinyo["closed"] is True
        assert shinyo["fills"][0]["qty"] == 300  # 現引は換算されず信用新規と同じ基準

    def test_price_jump_detected_without_registration_marks_suspect(self, db_path):
        # 1491相当 (保有中): 未換算のまま既存ロジックで処理すると併合前4,000株が
        # 未消化のまま残り保有中になる (issue #398 の背景そのもの)。
        self._add_1491_fills(db_path)  # split_adj は登録しない
        eps = helpers.build_fill_episodes(db_path=db_path)
        open_ep = [e for e in eps if e["code_s"] == "1491" and e["kind"] == "現物"][0]
        assert open_ep["closed"] is False
        assert open_ep["split_suspect"] is True
        # サマリー集計 (calc_trade_summary 相当) から除外されることを確認 (route側と同じ条件式)
        fill_pls_1491 = [e["pl"] for e in eps if e["code_s"] == "1491" and e["closed"]
                         and e["pl"] and not e.get("split_suspect")]
        assert fill_pls_1491 == []

        # クローズ済みでも split_suspect が付くこと (closed/open 双方への伝播確認)
        _add(db_path, "5491", "2025-01-01", "buy", 8000, 45, seq_salt="a")
        _add(db_path, "5491", "2025-06-01", "sell", 8000, 60, seq_salt="b")  # 未換算のまま0株で完結
        _add(db_path, "5491", "2025-09-01", "buy", 100, 900, seq_salt="c")   # 単価ジャンプ
        _add(db_path, "5491", "2025-09-15", "sell", 100, 950, seq_salt="d")
        eps2 = helpers.build_fill_episodes(db_path=db_path)
        closed_ep = [e for e in eps2 if e["code_s"] == "5491" and e["kind"] == "現物"][0]
        assert closed_ep["closed"] is True
        assert closed_ep["split_suspect"] is True

    def test_pending_review_marks_suspect_without_price_jump(self, db_path):
        # 9252相当: 現物fillの単価変化が小さく単価ジャンプ検知には引っかからないが、
        # --check-splits の保有中総当たりチェックでのみ見つかるケース。
        # build_fill_episodes は yfinance を呼ばないため、pending_review 経由で伝播する。
        _add(db_path, "9252", "2025-08-06", "buy", 100, 3270, seq_salt="a")
        _add(db_path, "9252", "2025-08-08", "sell", 83, 4250, trade_kind="現物(単元未満)", seq_salt="b")
        eps = helpers.build_fill_episodes(db_path=db_path)
        ep = [e for e in eps if e["code_s"] == "9252" and e["kind"] == "現物"][0]
        assert not ep.get("split_suspect")  # pending_review 未登録ならフラグは付かない

        ps.mark_split_pending_review("9252", reason="保有中総当たりチェック", db_path=db_path)
        eps2 = helpers.build_fill_episodes(db_path=db_path)
        ep2 = [e for e in eps2 if e["code_s"] == "9252" and e["kind"] == "現物"][0]
        assert ep2["split_suspect"] is True

        # add_split_adjustment で登録すれば pending は自動解除される
        ps.add_split_adjustment("9252", "2025-08-07", 0.833333, db_path=db_path)
        assert "9252" not in ps.list_pending_review_codes(db_path=db_path)

    def test_merger_ratio_closes_without_residual_qty(self, db_path):
        # 20:1併合相当の比率で浮動小数の残差が出てもクローズ判定を妨げない
        _add(db_path, "4491", "2025-01-01", "buy", 4000, 45, seq_salt="a")
        _add(db_path, "4491", "2025-06-01", "sell", 3000, 60, seq_salt="b")
        _add(db_path, "4491", "2025-06-15", "sell", 1000, 70, seq_salt="c")
        ps.add_split_adjustment("4491", "2025-03-01", 0.05, db_path=db_path)
        eps = helpers.build_fill_episodes(db_path=db_path)
        ep = [e for e in eps if e["code_s"] == "4491"][0]
        assert ep["closed"] is True  # 200株 - 200株 = 残差はあっても0扱い

    def test_second_split_after_registration_still_marks_suspect(self, db_path):
        # PRレビュー #405 指摘: 「登録済みイベントが1件でもあれば安全」という判定は
        # 粗く、同一銘柄で後日発生した別の分割・併合 (未登録) を見逃す。
        # 1回目 (登録済み) の後、単価が急変する2回目のジャンプが検知されるはず。
        _add(db_path, "8491", "2025-01-01", "buy", 1000, 100, seq_salt="a")
        _add(db_path, "8491", "2025-06-01", "sell", 500, 110, seq_salt="b")
        ps.add_split_adjustment("8491", "2025-03-01", 0.5, db_path=db_path)  # 1回目登録済み
        _add(db_path, "8491", "2025-12-01", "sell", 100, 900, seq_salt="c")  # 2回目 (未登録) の痕跡
        eps = helpers.build_fill_episodes(db_path=db_path)
        ep = [e for e in eps if e["code_s"] == "8491" and e["kind"] == "現物"][0]
        assert ep["split_suspect"] is True  # 登録済みでも新規ジャンプがあれば要確認扱い
