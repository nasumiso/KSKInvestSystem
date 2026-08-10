"""import_portfolio_csv.py のテスト (issue #397 Phase1)。

4ソース (楽天現物/楽天信用/SBI現物/SBI信用) のファイル判別・パース・
建玉単位の合算・不足ソース検出を検証する。実データの列構成を縮図として再現する。
"""

import csv

import pytest

import import_portfolio_csv as ic
import portfolio_shelve as ps
import research_shelve as rs


def _write_csv(path, rows):
    with open(path, "w", encoding="shift_jis", newline="") as f:
        w = csv.writer(f)
        for r in rows:
            w.writerow(r)
    return str(path)


# 実CSVのヘッダ構造を縮図として再現 (issue #397 §2-0 / §2-1b)
RAKUTEN_SPOT_ROWS = [
    ["■資産合計欄"],
    ["国内株式", "1,000,000"],  # サマリー行 (誤読対策の検証対象、issue #397 §2-2b)
    [],
    ["■ 保有商品詳細 (すべて）"],
    [],
    ["種別", "銘柄コード・ティッカー", "銘柄", "口座", "保有数量", "［単位］",
     "平均取得価額", "［単位］"],
    ["国内株式", "402A", "アクセルスペース", "特定", "600", "株", "765.21", "円"],
    ["米国株式", "TSLA", "テスラ", "特定", "6", "株", "428.09", "USD"],
]

RAKUTEN_MARGIN_ROWS = [
    ["■表示形式", "個別銘柄"],
    [],
    ["保証金率（新規建）［％］", "71.66"],
    ["売建[円]", "0", "買建[円]", "100"],
    [],
    ["口座区分", "銘柄コード", "銘柄名", "市場名称", "売買", "信用区分", "弁済期限",
     "建玉数量［株］", "執行中［株］", "建単価[円]"],
    ["特定", "402A", "アクセルスペース", "東証", "買建", "制度", "6ヶ月", "500", "0", "580"],
    ["特定", "402A", "アクセルスペース", "東証", "買建", "制度", "6ヶ月", "400", "0", "830"],
    ["特定", "9999", "空売り銘柄", "東証", "売建", "制度", "6ヶ月", "100", "0", "1000"],
]

SBI_SPOT_ROWS = [
    [],
    ["保有証券一覧"],
    [],
    ["株式（特定預り）合計"],
    [],
    ["評価額合計", "評価損益合計"],
    ["100000", "+5000"],
    [],
    ["株式（特定預り）"],
    [],
    ["銘柄コード", "銘柄名称", "保有株数", "売却注文中", "取得単価", "現在値",
     "取得金額", "評価額", "評価損益"],
    ["6501", "日立", "100", "", "4750", "5634", "475000", "563400", "+88400"],
    ["1681", "上場ＭＳエマ", "550", "", "2326", "3284", "1279300", "1806200", "+526900"],
]

SBI_MARGIN_ROWS = [
    [],
    ["信用建玉一覧"],
    [],
    ["個別表示"],
    [],
    ["売建玉総額", "買建玉総額", "建玉評価損益合計"],
    ["--", "2737700", "+181331"],
    [],
    ["銘柄コード", "銘柄名称", "売/買建", "市場", "期限", "建日", "返済期限", "預り",
     "建株数", "注文中", "建単価"],
    ["4970", "東洋合成", "買建", "東証", "6ヶ月", "2026/04/14", "2026/10/13", "特定",
     "100", "", "11610"],
]


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """ウォッチリスト (stocks_shelve) に個別株を登録し、ETF は登録しない。"""
    stocks_db = str(tmp_path / "stocks")
    monkeypatch.setattr("db_shelve.STOCKS_SHELVE", stocks_db)
    monkeypatch.setattr("webapp.helpers.STOCKS_SHELVE", stocks_db)
    rs_db = str(tmp_path / "research")
    monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", rs_db)
    monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", rs_db)
    for code, nm in [("6501", "日立"), ("4970", "東洋合成"), ("402A", "アクセルスペース")]:
        rec = rs.create_research_record(code, nm, overall_rating="B")
        rs.upsert_research_record(rec, db_path=rs_db)
    return str(tmp_path / "portfolio")


@pytest.mark.parametrize(
    "rows,expected",
    [
        (RAKUTEN_SPOT_ROWS, ("楽天", "現物")),
        (RAKUTEN_MARGIN_ROWS, ("楽天", "信用")),
        (SBI_SPOT_ROWS, ("SBI", "現物")),
        (SBI_MARGIN_ROWS, ("SBI", "信用")),
    ],
)
def test_detect_source(rows, expected):
    """4ソースをファイル名に依存せず中身から判別する (issue #397 §5-2 判別表)。"""
    assert ic.detect_source(rows) == expected


def test_parse_rakuten_spot_skips_summary_row_and_foreign_stock(db_path):
    """冒頭サマリー行の誤読を防ぎ、国内株式のみ採用する (issue #397 §2-2b)。"""
    parsed = ic.parse_rakuten_spot(RAKUTEN_SPOT_ROWS)
    assert len(parsed) == 1
    assert parsed[0] == {"code_s": "402A", "account": "特定", "kind": "現物",
                         "qty": 600, "avg_price": 765.21}


def test_parse_rakuten_margin_separates_buy_and_sell(db_path):
    """買建は kind=信用、売建は kind=信用売建 に分けて返す (issue #397 §2-0)。"""
    parsed = ic.parse_rakuten_margin(RAKUTEN_MARGIN_ROWS)
    kinds = {(p["code_s"], p["kind"]) for p in parsed}
    assert ("402A", "信用") in kinds
    assert ("9999", "信用売建") in kinds
    # 建玉単位の複数行 (issue #397 §2-0) は統合層で合算する
    agg = ic._aggregate_by_account_kind_code(parsed)
    assert agg[("特定", "信用", "402A")]["qty"] == 900  # 500+400


def test_parse_sbi_spot_excludes_etf(db_path):
    """SBI現物CSVに混在するETFを除外する (issue #397 §2-1b)。"""
    parsed = ic.parse_sbi_spot(SBI_SPOT_ROWS)
    codes = {p["code_s"] for p in parsed}
    assert codes == {"6501"}  # 1681 (ETF) は除外


def test_import_csvs_requires_all_sources_unless_allow_partial(tmp_path, db_path):
    """4ソース不足時はエラー、--allow-partial 相当の引数で続行できる (issue #397 §5-1)。"""
    path = _write_csv(tmp_path / "rakuten_spot.csv", RAKUTEN_SPOT_ROWS)
    with pytest.raises(ValueError, match="必要なソースが不足"):
        ic.import_csvs([path], "2026-08-10", dry_run=True, db_path=db_path)

    result = ic.import_csvs([path], "2026-08-10", dry_run=True, allow_partial=True, db_path=db_path)
    assert set(result["missing_sources"]) == {"楽天/信用", "SBI/現物", "SBI/信用"}


def test_import_csvs_apply_writes_position_only_not_record(tmp_path, db_path):
    """Phase1: --apply でも position のみ保存し、record (qty/status) は一切変更しない
    (issue #397 §5-1)。"""
    paths = [
        _write_csv(tmp_path / "r_spot.csv", RAKUTEN_SPOT_ROWS),
        _write_csv(tmp_path / "r_margin.csv", RAKUTEN_MARGIN_ROWS),
        _write_csv(tmp_path / "s_spot.csv", SBI_SPOT_ROWS),
        _write_csv(tmp_path / "s_margin.csv", SBI_MARGIN_ROWS),
    ]
    result = ic.import_csvs(paths, "2026-08-10", dry_run=False, db_path=db_path)

    # 402A は未登録なので record は作られない (登録は Phase2 の add_to_watch 経由)
    assert ps.get_record("402A", db_path=db_path) is None
    # position は合算済みで保存されている (楽天現物600 + 楽天信用900)
    assert ps.compute_merged_qty("402A", db_path=db_path) == 1500
    assert ps.is_covered("402A", db_path=db_path) is True
    diff = next(d for d in result["diffs"] if d["code_s"] == "402A")
    assert diff["judgement"] == "未登録+保有検出 (Phase2で登録)"
