"""月次の売買振り返りの数字のテスト (issue #448)。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import trade_review  # noqa: E402

YM = "2026-09"


def _ep(code_s, first_seq, fills, *, kind="信用", profit=None, return_pct=None,
        hold_days=None, trade_idea="中期モメンタム", **extra):
    """エピソードを作る。fills は (約定日, 売買, 株数, 単価[, 取引種別]) のリスト。

    最後の約定が売りならクローズ済みとして扱い、損益を付ける。
    """
    rows = [
        {"trade_date": f[0], "side": f[1], "qty": f[2], "price": f[3],
         "trade_kind": f[4] if len(f) > 4 else ("信用新規" if f[1] == "buy" else "信用返済"),
         "seq": first_seq + i}
        for i, f in enumerate(fills)
    ]
    closed = rows[-1]["side"] == "sell"
    ep = {
        "code_s": code_s, "stock_name": f"銘柄{code_s}", "kind": kind, "first_seq": first_seq,
        "episode_key": f"{code_s}|{kind}|{first_seq}", "fills": rows,
        "open_date": rows[0]["trade_date"],
        "close_date": rows[-1]["trade_date"] if closed else None,
        "closed": closed, "qty_peak": max(r["qty"] for r in rows), "trade_idea": trade_idea,
        "pl": ({"profit_amount": profit, "return_pct": return_pct, "hold_days": hold_days}
               if closed else None),
    }
    ep.update(extra)
    return ep


def _build(episodes, *, exposure_history=(), records=(), stocks=None, fill_strategies=None,
           action_logs=None):
    return trade_review.build_review_dataset(
        YM, episodes=episodes, exposure_history=list(exposure_history), records=list(records),
        stocks=stocks or {}, generated_at="2026-10-01T00:00:00",
        fill_strategies=fill_strategies or {}, action_logs=action_logs,
        trade_ideas=[
            {"name": "中期モメンタム", "time_horizon": "中期", "exit_rule": {"stop_loss_pct": 10}},
            {"name": "中長期ファンダ", "time_horizon": "中長期", "exit_rule": {"stop_loss_pct": 25}},
            {"name": "夢枠", "time_horizon": "長期", "exit_rule": {"stop_loss_pct": None}}])


def test_対象は当月の完全決済と当月の新規建て():
    episodes = [
        _ep("1001", 1, [("2026-06-01", "buy", 100, 1000), ("2026-09-10", "sell", 100, 1100)],
            profit=10000, return_pct=10.0, hold_days=101),                        # 当月に決済
        _ep("1002", 1, [("2026-09-05", "buy", 100, 1000)]),                       # 当月に新規建て
        _ep("1003", 1, [("2026-09-05", "buy", 100, 1000), ("2026-09-08", "sell", 100, 900)],
            profit=-10000, return_pct=-10.0, hold_days=3),                        # 建てて当月に決済
        _ep("1004", 1, [("2026-09-10", "buy", 100, 1000, "現引")], kind="現物"),   # 現引で始まる現物
        _ep("1005", 1, [("2026-06-01", "buy", 200, 1000), ("2026-09-10", "sell", 100, 1100),
                        ("2026-09-11", "buy", 100, 1000)]),                       # 部分売却だけ
        _ep("1006", 1, [("2026-09-05", "buy", 100, 1000)], split_suspect=True),   # 分割の疑い
    ]
    trades = _build(episodes)["trades"]
    assert [r["code_s"] for r in trades["closed"]] == ["1003", "1001"]
    assert [r["code_s"] for r in trades["opened"]] == ["1002"]
    assert trades["split_suspect_count"] == 1
    # 中期 (10〜180日) の戦略を3日で決済したら時間軸と合わない
    assert [r["hold_days_consistent"] for r in trades["closed"]] == [False, True]


def test_サマリーは保有日数帯と局面別の売買額を出す():
    def closed(code_s, hold_days, profit, **kw):
        return _ep(code_s, 1, [("2026-01-05", "buy", 100, 1000), ("2026-09-02", "sell", 100, 1100)],
                   profit=profit, return_pct=1.0, hold_days=hold_days, **kw)

    episodes = [
        closed("2001", 3, 100), closed("2002", 4, 200), closed("2003", 90, 400),
        closed("2004", 91, -800, trade_idea=""),
        # 現引は信用側・現物側の両方に出るので売買額に入れない
        _ep("2005", 1, [("2026-09-03", "buy", 100, 500, "現引")], kind="現物"),
        # 空売りは額に入れず約定数だけ数える
        _ep("2006", 1, [("2026-09-03", "sell", 100, 500, "信用新規")], is_short=True,
                closed=False, close_date=None, pl=None),
        _ep("2007", 1, [("2026-09-03", "buy", 100, 700)]),
    ]
    history = [
        {"date": "2026-09-02", "state": "uptrend_under_pressure", "ratio_pct": 85.0, "position": "within"},
        {"date": "2026-09-03", "state": "market_in_correction", "ratio_pct": 83.0, "position": "over"},
        # 土曜のログは前営業日の写しなので日数に数えない
        {"date": "2026-09-05", "state": "market_in_correction", "ratio_pct": 83.0, "position": "over"},
    ]
    s = _build(episodes, exposure_history=history)["summary"]
    assert s["closed"] == {"count": 4, "pl": -100, "wins": 3, "losses": 1, "no_pl_count": 0}
    assert s["undeclared"] == {"count": 1, "pl": -800}
    assert [(b["count"], b["pl"]) for b in s["hold_bands"]] == [(1, 100), (2, 600), (1, -800)]
    by_state = {r["state"]: r for r in s["by_state"]}
    assert by_state["market_in_correction"]["buy_amount"] == 70000
    assert by_state["market_in_correction"]["buy_count"] == 1
    assert by_state["uptrend_under_pressure"]["sell_amount"] == 440000
    assert s["short_fill_count"] == 1
    assert s["exposure"] == {"ratio_min": 83.0, "ratio_max": 85.0, "logged_days": 2,
                             "correction_days": 1, "correction_days_over": 1}
    # issue #480 以後の行 (目標の維持率) が当月にあれば、そちらで集計する
    history.append({"date": "2026-09-04", "state": "market_in_correction", "target_rate": 70,
                    "step": -1, "margin_rate_pct": 56.7, "position": "over"})
    assert _build(episodes, exposure_history=history)["summary"]["exposure"] == {
        "margin_rate_min": 56.7, "margin_rate_max": 56.7, "logged_days": 1,
        "nonneutral_days": 1, "over_days": 1, "within_days": 0, "under_days": 0}


def test_弱点仮説は年初来で損切りの幅と再INを数える():
    def closed(code_s, seq, open_date, close_date, profit, return_pct, **kw):
        return _ep(code_s, seq, [(open_date, "buy", 100, 1000), (close_date, "sell", 100, 1000)],
                   profit=profit, return_pct=return_pct, hold_days=1, **kw)

    episodes = [
        closed("3001", 1, "2026-02-01", "2026-03-01", -1000, -10.0),   # 幅ちょうどは超過にしない
        # 幅は戦略ごと。中長期ファンダ (25%) の -24% は範囲内、夢枠は幅が無いので判定しない
        closed("3006", 1, "2026-02-01", "2026-03-01", -2000, -24.0, trade_idea="中長期ファンダ"),
        closed("3007", 1, "2026-02-01", "2026-03-01", -4000, -40.0, trade_idea="夢枠"),
        closed("3002", 1, "2026-02-01", "2026-09-01", -3000, -10.1),   # 超過 (当月)
        closed("3003", 1, "2025-11-01", "2025-12-20", -5000, -30.0),   # 前年の決済は母数外
        closed("3004", 1, "2026-02-01", "2026-03-01", -7000, -30.0, split_suspect=True),
        closed("3005", 1, "2026-02-01", "2026-03-01", None, None),     # 損益なし
        # 3001 の決済 (3/1) から30日ちょうどは再IN、31日は再INではない
        closed("3001", 3, "2026-03-31", "2026-04-10", 500, 5.0),
        closed("3005", 3, "2026-04-01", "2026-04-10", 900, 9.0),
        # 買いを決済した直後の空売りは向きが逆なので再INではない
        closed("3002", 5, "2026-09-05", "2026-09-06", 100, 1.0, is_short=True),
        # 前年末の決済から年初に建て直し、まだ保有中
        _ep("3003", 3, [("2026-01-10", "buy", 100, 1000)]),
    ]
    h = _build(episodes)["hypotheses"]
    assert (h["closed_count"], h["excluded_count"]) == (7, 2)
    assert h["stop_loss_width"] == {"loss_count": 4, "loss_pl": -10000, "over_count": 1,
                                    "over_pl": -3000, "within_pl": -3000,
                                    "no_width_count": 1, "no_width_pl": -4000,
                                    "month_over_count": 1}
    assert h["reentry"] == {"count": 2, "settled_count": 1, "settled_wins": 1,
                            "settled_pl": 500, "month_count": 0}


def test_保有PFは1保で数え含み損益率に実現損益を混ぜない():
    def holding(code_s, unrealized, **kw):
        # 部分売却で実現益が出ていても、残りの含み損だけで判定する
        return _ep(code_s, 1, [("2026-08-01", "buy", 200, 1000)], kind="現物",
                   open_pl={"realized": 50000, "unrealized": unrealized, "held_qty": 100,
                            "avg_cost": 1000.0, "return_pct": 20.0}, **kw)

    # 4006 は中長期ファンダ (25%) の範囲内。4007 (戦略のひもづけが要再確認) と
    # 4008 (ロットに別の戦略) は判定しない
    episodes = [holding("4001", -10100), holding("4002", -10000), holding("4003", -30000),
                holding("4004", -30000, split_suspect=True),
                holding("4006", -20000, trade_idea="中長期ファンダ"),
                holding("4007", -20000, strategy_drift=True), holding("4008", -20000)]
    records = [{"code_s": c, "qty": q, "memo": {"trade_idea": "GARP"}}
               for c, q in (("4001", 100), ("4002", 100), ("4004", 2), ("4005", 0),
                            ("4006", 100), ("4007", 100), ("4008", 100))]
    stocks = {c: {"price": 900.0, "stock_name": f"銘柄{c}"}
              for c in ("4001", "4002", "4004", "4005", "4006", "4007", "4008")}
    history = [{"date": "2026-10-01", "ratio_pct": 94.5, "state": "market_in_correction"},
               {"date": "2026-09-30", "ratio_pct": 80.0}]
    p = _build(episodes, records=records, stocks=stocks, exposure_history=history,
               fill_strategies={("4008", 1): {"trade_idea": "中長期ファンダ"}})["portfolio"]
    # 4003 は 1保 に無い (売却の約定が未取込の保有中エピソード)。4005 は株数 0
    assert p["holding_count"] == 6
    assert p["small"] == {"count": 1, "value": 1800.0}
    assert [(r["code_s"], r["stop_loss_pct"], round(r["unrealized_pct"], 1))
            for r in p["over_stop_loss"]] == [("4001", 10.0, -10.1)]
    assert p["exposure"]["date"] == "2026-10-01" and p["exposure"]["ratio_pct"] == 94.5


def test_発掘元は建てた日以前の最後のログを付ける():
    """issue #509: /market のテーマ別のブレイクから監視に追加した銘柄を、取引ごとに拾う。"""
    def log(code_s, day, reason):
        return {"code_s": code_s, "timestamp": f"{day}T15:30:00+09:00", "reason": reason}

    episodes = [_ep(c, 1, [("2026-09-10", "buy", 100, 1000)]) for c in ("5001", "5002", "5003")]
    logs = [
        log("5001", "2026-09-01", "発掘元:日次レポート テーマ=半導体 日付=2026-08-31"),
        log("5001", "2026-09-08", "発掘元:日次レポート テーマ=サイバーセキュリティ 日付=2026-09-08"),
        log("5002", "2026-09-11", "発掘元:日次レポート テーマ=半導体 日付=2026-09-11"),  # 建てた後
        log("5003", "2026-09-01", "WebApp 追加"),
    ]
    opened = _build(episodes, action_logs=logs)["trades"]["opened"]
    assert [r["discovery"] for r in opened] == [
        {"theme": "サイバーセキュリティ", "date": "2026-09-08"}, None, None]


@pytest.mark.parametrize("is_dev, existing, error", [
    (False, None, None),
    (False, "2026-09.md", FileExistsError),   # どちらかが既にあれば何も書かない
    (True, None, RuntimeError),               # 開発機は正本データに書かない
])
def test_保存は上書きせず開発機では書かない(tmp_path, monkeypatch, is_dev, existing, error):
    monkeypatch.setattr(trade_review.ks_util, "is_dev_data_dir", lambda: is_dev)
    if existing:
        (tmp_path / existing).write_text("旧", encoding="utf-8")
    if error:
        with pytest.raises(error):
            trade_review.save_review(YM, {"a": 1}, "# レポート", review_dir=str(tmp_path))
        assert sorted(p.name for p in tmp_path.iterdir()) == ([existing] if existing else [])
        return
    trade_review.save_review(YM, {"a": 1}, "# レポート", review_dir=str(tmp_path))
    assert (tmp_path / "2026-09.md").read_text(encoding="utf-8") == "# レポート"
    assert (tmp_path / "2026-09.json").read_text(encoding="utf-8").strip().startswith("{")
