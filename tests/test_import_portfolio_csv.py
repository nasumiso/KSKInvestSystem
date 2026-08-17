"""import_portfolio_csv.py のテスト (issue #397 Phase1)。

4ソース (楽天現物/楽天信用/SBI現物/SBI信用) のファイル判別・パース・
建玉単位の合算・不足ソース検出を検証する。実データの列構成を縮図として再現する。
"""

import csv
import datetime

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

# 楽天現物の JP版 (assetbalance(JP): 国内株式のみ)。種別列・口座列が無く、
# 口座区分は "■特定口座" のようなセクション見出しで表現される。
RAKUTEN_JP_SPOT_ROWS = [
    ["■現在の評価額合計［円］", "", "10,365,050"],
    ["■評価損益合計", "前日比［円］", "776,850"],
    ["■特定口座"],
    [],
    ["銘柄コード", "銘柄名", "保有数量［株］", "執行中［株］", "(内訳　通常数量[株])",
     "(内訳　積立数量[株])", "平均取得価額［円］", "取得総額［円］"],
    ["402A", "アクセルスペース", "600", "600", "600", "0", "765.21", "459,129"],
    ["1681", "上場ＭＳエマ", "550", "0", "550", "0", "2,326.00", "1,279,300"],  # ETF
    ["", "", "", "", "", "", "特定口座合計", "9,166,700"],  # 合計行 (スキップ対象)
    [],
    ["■NISA口座"],
    [],
    ["銘柄コード", "銘柄名", "保有数量［株］", "執行中［株］", "(内訳　通常数量[株])",
     "(内訳　積立数量[株])", "平均取得価額［円］", "取得総額［円］"],
    ["6501", "日立", "100", "0", "100", "0", "4,750.00", "475,000"],
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
    # テスト共通fixtureは DATA_DIR を隔離するため、実ファイルではなく明示的な
    # ETF集合を使う。CSVパーサーのETF除外をテスト環境でも検証する。
    monkeypatch.setattr(ps, "_etf_codes_cache", frozenset({"1681"}))
    for code, nm in [("6501", "日立"), ("4970", "東洋合成"), ("402A", "アクセルスペース")]:
        rec = rs.create_research_record(code, nm, overall_rating="B")
        rs.upsert_research_record(rec, db_path=rs_db)
    return str(tmp_path / "portfolio")


@pytest.mark.parametrize(
    "rows,expected",
    [
        (RAKUTEN_SPOT_ROWS, ("楽天", "現物")),
        (RAKUTEN_JP_SPOT_ROWS, ("楽天", "現物")),
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


def test_parse_rakuten_jp_spot_sections_and_summary_row(db_path):
    """JP版は口座区分をセクション見出しから取り、合計行とETFを除外する。"""
    parsed = ic.parse_rakuten_jp_spot(RAKUTEN_JP_SPOT_ROWS)
    assert parsed == [
        {"code_s": "402A", "account": "特定", "kind": "現物",
         "qty": 600, "avg_price": 765.21},
        {"code_s": "6501", "account": "NISA", "kind": "現物",
         "qty": 100, "avg_price": 4750.0},
    ]


def test_select_parser_switches_rakuten_spot_format(db_path):
    """同じ ("楽天", "現物") でも (all)版と(JP)版で別パーサーを選ぶ。"""
    source = ("楽天", "現物")
    assert ic.select_parser(source, RAKUTEN_SPOT_ROWS) is ic.parse_rakuten_spot
    assert ic.select_parser(source, RAKUTEN_JP_SPOT_ROWS) is ic.parse_rakuten_jp_spot


@pytest.mark.parametrize("invalid_code", ["", "12X34"])
def test_parse_rakuten_spot_rejects_invalid_domestic_stock_code(db_path, invalid_code):
    """国内株式の保有行で不正なコードを検出したら取込を停止する。"""
    invalid_rows = [row.copy() for row in RAKUTEN_SPOT_ROWS]
    invalid_rows[6][1] = invalid_code
    with pytest.raises(ValueError, match="国内株式行の銘柄コードが不正"):
        ic.parse_rakuten_spot(invalid_rows)


def test_parse_rakuten_margin_separates_buy_and_sell(db_path):
    """買建は kind=信用、売建は kind=信用売建 に分けて返す (issue #397 §2-0)。"""
    parsed = ic.parse_rakuten_margin(RAKUTEN_MARGIN_ROWS)
    kinds = {(p["code_s"], p["kind"]) for p in parsed}
    assert ("402A", "信用") in kinds
    assert ("9999", "信用売建") in kinds
    # 建玉単位の複数行 (issue #397 §2-0) は統合層で合算する
    agg = ic._aggregate_by_account_kind_code(parsed)
    assert agg[("特定", "信用", "402A")]["qty"] == 900  # 500+400


@pytest.mark.parametrize(
    "parser,rows,row_index,code_index",
    [
        (ic.parse_rakuten_margin, RAKUTEN_MARGIN_ROWS, 6, 1),
        (ic.parse_sbi_spot, SBI_SPOT_ROWS, 11, 0),
        (ic.parse_sbi_margin, SBI_MARGIN_ROWS, 9, 0),
    ],
)
def test_parsers_reject_invalid_position_code(db_path, parser, rows, row_index, code_index):
    """保有・建玉行の不正な銘柄コードは、行を捨てず取込を停止する。"""
    invalid_rows = [row.copy() for row in rows]
    invalid_rows[row_index][code_index] = "12X34"
    with pytest.raises(ValueError, match="銘柄コードが不正"):
        parser(invalid_rows)


@pytest.mark.parametrize(
    "parser,rows,row_index,qty_index",
    [
        (ic.parse_rakuten_spot, RAKUTEN_SPOT_ROWS, 6, 4),
        (ic.parse_rakuten_margin, RAKUTEN_MARGIN_ROWS, 6, 7),
        (ic.parse_sbi_margin, SBI_MARGIN_ROWS, 9, 8),
    ],
)
@pytest.mark.parametrize("invalid_qty", ["--", "1.5"])
def test_other_parsers_reject_invalid_qty(db_path, parser, rows, row_index, qty_index, invalid_qty):
    """全ソースで不正な株数を検出したら取込を停止する。"""
    invalid_rows = [row.copy() for row in rows]
    invalid_rows[row_index][qty_index] = invalid_qty
    with pytest.raises(ValueError, match="0以上の整数|数値として読めません"):
        parser(invalid_rows)


def test_margin_parsers_exclude_etf(db_path):
    """信用建玉に混在するETFも株式分析の取込対象から除外する。"""
    rakuten_rows = [row.copy() for row in RAKUTEN_MARGIN_ROWS]
    rakuten_rows.append(["特定", "1681", "ETF", "東証", "買建", "制度", "6ヶ月", "100", "0", "1000"])
    sbi_rows = [row.copy() for row in SBI_MARGIN_ROWS]
    sbi_rows.append(["1681", "ETF", "買建", "東証", "6ヶ月", "", "", "特定", "100", "", "1000"])

    assert "1681" not in {p["code_s"] for p in ic.parse_rakuten_margin(rakuten_rows)}
    assert "1681" not in {p["code_s"] for p in ic.parse_sbi_margin(sbi_rows)}


def test_parse_sbi_spot_excludes_etf(db_path):
    """SBI現物CSVに混在するETFを除外する (issue #397 §2-1b)。"""
    parsed = ic.parse_sbi_spot(SBI_SPOT_ROWS)
    codes = {p["code_s"] for p in parsed}
    assert codes == {"6501"}  # 1681 (ETF) は除外


@pytest.mark.parametrize("invalid_qty", ["--", "1.5"])
def test_parse_sbi_spot_keeps_unknown_code_and_rejects_invalid_qty(db_path, invalid_qty):
    """銘柄名未解決の保有行は残し、株数異常は取込全体を停止する。"""
    unknown_rows = SBI_SPOT_ROWS + [
        ["9999", "新規上場", "50", "", "1000", "1100", "50000", "55000", "+5000"],
    ]
    assert {p["code_s"] for p in ic.parse_sbi_spot(unknown_rows)} == {"6501", "9999"}

    invalid_rows = SBI_SPOT_ROWS + [
        ["9999", "新規上場", invalid_qty, "", "1000", "1100", "", "", ""],
    ]
    with pytest.raises(ValueError, match="9999.*保有株数"):
        ic.parse_sbi_spot(invalid_rows)


def test_parse_sbi_spot_handles_multiple_account_sections(db_path, monkeypatch):
    """特定・NISA等の複数セクションが同一CSVに存在する場合、それぞれ正しい
    account に紐付け、想定外の口座区分として警告する (issue #397 §5-2)。

    旧実装は最初のヘッダ行しか見ないため、2セクション目のデータ行を
    1セクション目 (特定) の続きとして誤って合算していた (実データでは
    未発生だが、SBIでNISA/一般の保有が発生した場合に効く回帰テスト)。
    """
    rows = [
        [], ["保有証券一覧"], [],
        ["株式（特定預り）合計"], [], ["評価額合計", "評価損益合計"], ["100000", "+5000"], [],
        ["株式（特定預り）"], [],
        ["銘柄コード", "銘柄名称", "保有株数", "売却注文中", "取得単価", "現在値",
         "取得金額", "評価額", "評価損益"],
        ["6501", "日立", "100", "", "4750", "5634", "475000", "563400", "+88400"],
        [],
        ["株式（NISA預り）合計"], [], ["評価額合計", "評価損益合計"], ["50000", "+2000"], [],
        ["株式（NISA預り）"], [],
        ["銘柄コード", "銘柄名称", "保有株数", "売却注文中", "取得単価", "現在値",
         "取得金額", "評価額", "評価損益"],
        ["4970", "東洋合成", "50", "", "11000", "13540", "550000", "677000", "+127000"],
    ]
    warnings = []
    monkeypatch.setattr(ic, "log_warning", lambda msg: warnings.append(msg))

    parsed = ic.parse_sbi_spot(rows)
    by_code = {p["code_s"]: p for p in parsed}
    assert by_code["6501"]["account"] == "特定"
    assert by_code["4970"]["account"] == "NISA"  # 特定に誤合算されないこと
    assert any("想定外の口座区分" in w for w in warnings)


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
    assert diff["judgement"] == "未登録+保有検出 (反映すると監視へ登録)"


def test_import_csvs_partial_update_carries_over_db_sources(tmp_path, db_path):
    """楽天のみ再取込した場合、SBI分はDBの前回分を引き継いで covered=True のまま
    merged_qty・自動反映が機能する (issue #397 Phase3b の部分更新)。"""
    all_paths = [
        _write_csv(tmp_path / "r_spot.csv", RAKUTEN_SPOT_ROWS),
        _write_csv(tmp_path / "r_margin.csv", RAKUTEN_MARGIN_ROWS),
        _write_csv(tmp_path / "s_spot.csv", SBI_SPOT_ROWS),
        _write_csv(tmp_path / "s_margin.csv", SBI_MARGIN_ROWS),
    ]
    ic.import_csvs(all_paths, "2026-08-03", dry_run=False, db_path=db_path)

    # 2回目: 楽天2ファイルのみ (SBIは未アップロード)
    rakuten_only = [
        _write_csv(tmp_path / "r_spot2.csv", RAKUTEN_SPOT_ROWS),
        _write_csv(tmp_path / "r_margin2.csv", RAKUTEN_MARGIN_ROWS),
    ]
    result = ic.import_csvs(rakuten_only, "2026-08-10", dry_run=False, apply_records=True, db_path=db_path)

    assert result["missing_sources"] == []
    assert result["carried_over_sources"] == {"SBI/現物": "2026-08-03", "SBI/信用": "2026-08-03"}
    # 6501 は SBI現物のみに存在 (楽天CSVには登場しない) -> SBI分を引き継いで covered のまま
    assert ps.is_covered("6501", db_path=db_path) is True
    assert ps.compute_merged_qty("6501", db_path=db_path) == 100


def test_import_csvs_partial_dry_run_previews_carried_over_merged_qty(tmp_path, db_path):
    """部分更新の dry-run でも、DBに前回分があれば merged_qty に合算して表示する。"""
    all_paths = [
        _write_csv(tmp_path / "r_spot.csv", RAKUTEN_SPOT_ROWS),
        _write_csv(tmp_path / "r_margin.csv", RAKUTEN_MARGIN_ROWS),
        _write_csv(tmp_path / "s_spot.csv", SBI_SPOT_ROWS),
        _write_csv(tmp_path / "s_margin.csv", SBI_MARGIN_ROWS),
    ]
    ic.import_csvs(all_paths, "2026-08-03", dry_run=False, db_path=db_path)

    rakuten_only = [
        _write_csv(tmp_path / "r_spot2.csv", RAKUTEN_SPOT_ROWS),
        _write_csv(tmp_path / "r_margin2.csv", RAKUTEN_MARGIN_ROWS),
    ]
    result = ic.import_csvs(rakuten_only, "2026-08-10", dry_run=True, db_path=db_path)

    diff = next(d for d in result["diffs"] if d["code_s"] == "6501")
    assert diff["covered"] is True
    assert diff["merged_qty"] == 100


def test_import_csvs_replaces_positions_of_uploaded_source(tmp_path, db_path):
    """再取込したソースから消えた銘柄は前回 position に残さない。"""
    all_paths = [
        _write_csv(tmp_path / "r_spot.csv", RAKUTEN_SPOT_ROWS),
        _write_csv(tmp_path / "r_margin.csv", RAKUTEN_MARGIN_ROWS),
        _write_csv(tmp_path / "s_spot.csv", SBI_SPOT_ROWS),
        _write_csv(tmp_path / "s_margin.csv", SBI_MARGIN_ROWS),
    ]
    ic.import_csvs(all_paths, "2026-08-03", dry_run=False, db_path=db_path)

    empty_rakuten_spot = RAKUTEN_SPOT_ROWS[:6]
    result = ic.import_csvs([
        _write_csv(tmp_path / "r_spot_empty.csv", empty_rakuten_spot),
        _write_csv(tmp_path / "r_margin2.csv", RAKUTEN_MARGIN_ROWS),
    ], "2026-08-10", dry_run=False, db_path=db_path)

    assert ps.compute_merged_qty("402A", db_path=db_path) == 900
    assert next(d for d in result["diffs"] if d["code_s"] == "402A")["merged_qty"] == 900


def test_import_csvs_replaces_position_sources_of_uploaded_source(tmp_path, db_path):
    """再取込したソースの古い口座メタデータは残さない。"""
    paths = [
        _write_csv(tmp_path / "r_spot.csv", RAKUTEN_SPOT_ROWS),
        _write_csv(tmp_path / "r_margin.csv", RAKUTEN_MARGIN_ROWS),
        _write_csv(tmp_path / "s_spot.csv", SBI_SPOT_ROWS),
        _write_csv(tmp_path / "s_margin.csv", SBI_MARGIN_ROWS),
    ]
    ic.import_csvs(paths, "2026-08-03", dry_run=False, db_path=db_path)
    ps.upsert_position_source("楽天", "NISA", "現物", as_of="2026-08-03", row_count=1, db_path=db_path)

    ic.import_csvs([
        _write_csv(tmp_path / "r_spot2.csv", RAKUTEN_SPOT_ROWS),
        _write_csv(tmp_path / "r_margin2.csv", RAKUTEN_MARGIN_ROWS),
    ], "2026-08-10", dry_run=False, db_path=db_path)

    sources = [s for s in ps.list_position_sources(db_path=db_path)
               if (s["broker"], s["kind"]) == ("楽天", "現物")]
    assert len(sources) == 1
    assert sources[0]["account"] == "特定"
    assert sources[0]["as_of"] == "2026-08-10"
    assert sources[0]["row_count"] == 1


@pytest.mark.parametrize("as_of", ["2026-02-30", "2026-08-10suffix"])
def test_import_csvs_rejects_invalid_as_of_before_writing(tmp_path, db_path, as_of):
    """不正な基準日では position を書き換えない。"""
    path = _write_csv(tmp_path / "r_spot.csv", RAKUTEN_SPOT_ROWS)
    with pytest.raises(ValueError, match="as_of"):
        ic.import_csvs([path], as_of, dry_run=False, allow_partial=True, db_path=db_path)
    assert ps.list_positions(db_path=db_path) == []


def test_import_csvs_rejects_future_as_of_before_writing(tmp_path, db_path):
    """未来の基準日では position を書き換えない。"""
    path = _write_csv(tmp_path / "r_spot.csv", RAKUTEN_SPOT_ROWS)
    future_as_of = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="未来日"):
        ic.import_csvs([path], future_as_of, dry_run=False, allow_partial=True, db_path=db_path)
    assert ps.list_positions(db_path=db_path) == []


def test_judge_sell_when_held_qty_is_missing_but_csv_qty_is_zero():
    """1保はDB qtyが0でも、全ソースで保有ゼロなら売却候補にする。"""
    assert ic._judge("1保", True, 0, 0) == "売却候補 (反映すると準保有へ)"


class TestPhase2ApplyRecords:
    """apply_records=True (issue #397 Phase2) の record 同期を検証する。

    402A: 現物600+信用900=1500 (merged_qty)。4970: SBI信用100 (merged_qty)。
    いずれも4ソース全てが揃っているので covered=True になる (RAKUTEN_SPOT_ROWS /
    SBI_SPOT_ROWS には元々登場しない銘柄なので、DB側の初期状態だけで判定を作れる)。
    """

    def _paths(self, tmp_path):
        return [
            _write_csv(tmp_path / "r_spot.csv", RAKUTEN_SPOT_ROWS),
            _write_csv(tmp_path / "r_margin.csv", RAKUTEN_MARGIN_ROWS),
            _write_csv(tmp_path / "s_spot.csv", SBI_SPOT_ROWS),
            _write_csv(tmp_path / "s_margin.csv", SBI_MARGIN_ROWS),
        ]

    @pytest.mark.parametrize(
        "overrides,match",
        [
            ({"12X34": {"note": "確認"}}, "code_s"),
            ({"4970": {"trade_idea": "未登録戦略"}}, "マスター未登録"),
        ],
    )
    def test_invalid_override_is_rejected_before_writing(self, tmp_path, db_path, overrides, match):
        """不正な確認画面入力では position を書き換えない。"""
        with pytest.raises(ValueError, match=match):
            ic.import_csvs(
                self._paths(tmp_path), "2026-08-10", dry_run=False,
                apply_records=True, overrides=overrides, db_path=db_path,
            )
        assert ps.list_positions(db_path=db_path) == []

    def test_qty_change_on_existing_1poh(self, tmp_path, db_path):
        """1保 かつ merged_qty != db_qty -> 株数変更のみ (§5-3)。"""
        ps.add_to_watch("402A", db_path=db_path)
        ps.transition_status("402A", "2準", db_path=db_path)
        ps.seed_trade_ideas(db_path=db_path)
        ps.update_memo("402A", {"trade_idea": "GARP"}, db_path=db_path)
        ps.transition_status("402A", "1保", qty=1000, db_path=db_path)
        ps.update_qty("402A", 1000, log_action=False, db_path=db_path)

        result = ic.import_csvs(self._paths(tmp_path), "2026-08-10", dry_run=False,
                                apply_records=True, db_path=db_path)

        record = ps.get_record("402A", db_path=db_path)
        assert record["status"] == "1保"
        assert record["qty"] == 1500  # 現物600+信用900
        applied = next(a for a in result["applied"] if a["code_s"] == "402A")
        assert applied["action"] == "株数変更"
        log = ps.list_action_logs("402A", db_path=db_path)[-1]
        assert log["source"] == "csv_import"

    def test_sell_when_merged_qty_zero(self, tmp_path, db_path):
        """1保 かつ merged_qty=0 -> 2準へ自動OUT。3監にはしない (§5-4)。

        record["qty"] も 0 に同期する (2準なのに旧qtyが残る矛盾状態を避ける)。
        売却ログには「遷移前」の qty (=売った株数) が残ること (log_qty) も検証する。
        """
        ps.add_to_watch("8888", db_path=db_path)  # CSVに一切登場しないコード
        ps.transition_status("8888", "2準", db_path=db_path)
        ps.seed_trade_ideas(db_path=db_path)
        ps.update_memo("8888", {"trade_idea": "GARP"}, db_path=db_path)
        ps.transition_status("8888", "1保", qty=100, db_path=db_path)
        ps.update_qty("8888", 100, log_action=False, db_path=db_path)

        result = ic.import_csvs(self._paths(tmp_path), "2026-08-10", dry_run=False,
                                apply_records=True, allow_partial=False, db_path=db_path)

        record = ps.get_record("8888", db_path=db_path)
        assert record["status"] == "2準"
        assert record["qty"] == 0
        applied = next(a for a in result["applied"] if a["code_s"] == "8888")
        assert applied["action"] == "売却(OUT)"
        logs = ps.list_action_logs("8888", db_path=db_path)
        sell_log = logs[-1]
        assert sell_log["action_type"] == "売却"
        assert sell_log["source"] == "csv_import"
        assert sell_log["qty"] == 100  # 遷移前の保有株数 (売った株数) がログに残る
        # qty=0 への更新は log_action=False なので、追加の「株数変更」ログは残らない
        assert not any(l["action_type"] == "株数変更" for l in logs)

    def test_new_in_auto_when_trade_idea_set(self, tmp_path, db_path):
        """2準 かつ trade_idea 設定済み -> 自動で1保に遷移 (§6-2)。

        402A は CSV に登場するが本テストの DB には未登録なので、別途
        「登録+保留キューへ」の対象になる (想定通り、§5-3b)。4970 の
        挙動のみを検証する。
        """
        ps.add_to_watch("4970", db_path=db_path)
        ps.transition_status("4970", "2準", db_path=db_path)
        ps.seed_trade_ideas(db_path=db_path)
        ps.update_memo("4970", {"trade_idea": "GARP"}, db_path=db_path)

        result = ic.import_csvs(self._paths(tmp_path), "2026-08-10", dry_run=False,
                                apply_records=True, db_path=db_path)

        record = ps.get_record("4970", db_path=db_path)
        assert record["status"] == "1保"
        assert record["qty"] == 100  # SBI信用のみ
        assert not any(p["code_s"] == "4970" for p in ps.list_pending_in(db_path=db_path))
        applied = next(a for a in result["applied"] if a["code_s"] == "4970")
        assert applied["action"] == "新規IN(自動)"
        assert "GARP" in applied["detail"]  # 適用した戦略が分かるようにする (issue #397)

    def test_new_in_queued_when_trade_idea_missing(self, tmp_path, db_path):
        """2準 かつ trade_idea 未設定 -> 保留キューへ (§6-2)。record は変更しない。"""
        ps.add_to_watch("4970", db_path=db_path)
        ps.transition_status("4970", "2準", db_path=db_path)

        result = ic.import_csvs(self._paths(tmp_path), "2026-08-10", dry_run=False,
                                apply_records=True, db_path=db_path)

        record = ps.get_record("4970", db_path=db_path)
        assert record["status"] == "2準"
        pending = ps.list_pending_in(db_path=db_path)
        assert any(p["code_s"] == "4970" for p in pending)
        applied = next(a for a in result["applied"] if a["code_s"] == "4970")
        assert applied["action"] == "保留キューへ"

    def test_new_in_queued_when_trade_idea_is_explicitly_cleared(self, tmp_path, db_path):
        """既存戦略を明示解除した2準銘柄は自動INせず保留する。"""
        ps.add_to_watch("4970", db_path=db_path)
        ps.transition_status("4970", "2準", db_path=db_path)
        ps.seed_trade_ideas(db_path=db_path)
        ps.update_memo("4970", {"trade_idea": "GARP"}, db_path=db_path)

        result = ic.import_csvs(
            self._paths(tmp_path), "2026-08-10", dry_run=False, apply_records=True,
            overrides={"4970": {"trade_idea": ""}}, db_path=db_path,
        )

        record = ps.get_record("4970", db_path=db_path)
        assert record["status"] == "2準"
        assert record["memo"]["trade_idea"] == ""
        assert next(a for a in result["applied"] if a["code_s"] == "4970")["action"] == "保留キューへ"

    def test_new_in_never_auto_from_3kan_even_with_trade_idea(self, tmp_path, db_path):
        """3監 は trade_idea があっても自動INしない、必ず保留キュー (§6-2)。"""
        ps.add_to_watch("4970", db_path=db_path)  # 3監のまま
        ps.seed_trade_ideas(db_path=db_path)
        ps.update_memo("4970", {"trade_idea": "GARP"}, db_path=db_path)

        result = ic.import_csvs(self._paths(tmp_path), "2026-08-10", dry_run=False,
                                apply_records=True, db_path=db_path)

        record = ps.get_record("4970", db_path=db_path)
        assert record["status"] == "3監"
        assert any(p["code_s"] == "4970" for p in ps.list_pending_in(db_path=db_path))
        applied = next(a for a in result["applied"] if a["code_s"] == "4970")
        assert applied["action"] == "保留キューへ"

    def test_new_in_from_3kan_saves_selected_trade_idea(self, tmp_path, db_path):
        """3監では自動INせず、確認画面で選んだ戦略だけ保存する。"""
        ps.add_to_watch("4970", db_path=db_path)
        ps.seed_trade_ideas(db_path=db_path)

        result = ic.import_csvs(
            self._paths(tmp_path), "2026-08-10", dry_run=False, apply_records=True,
            overrides={"4970": {"trade_idea": "GARP"}}, db_path=db_path,
        )

        record = ps.get_record("4970", db_path=db_path)
        assert record["status"] == "3監"
        assert record["memo"]["trade_idea"] == "GARP"
        assert any(p["code_s"] == "4970" for p in ps.list_pending_in(db_path=db_path))
        assert next(a for a in result["applied"] if a["code_s"] == "4970")["action"] == "保留キューへ"

    def test_zero_qty_removes_pending_in(self, tmp_path, db_path):
        """保有ゼロになった準保有・監視銘柄は保留キューからも取り除く。"""
        ps.add_to_watch("4970", db_path=db_path)
        ps.upsert_pending_in("4970", 100, "2026-08-03", db_path=db_path)
        empty_sbi_margin = SBI_MARGIN_ROWS[:9]

        result = ic.import_csvs([
            _write_csv(tmp_path / "r_spot.csv", RAKUTEN_SPOT_ROWS),
            _write_csv(tmp_path / "r_margin.csv", RAKUTEN_MARGIN_ROWS),
            _write_csv(tmp_path / "s_spot.csv", SBI_SPOT_ROWS),
            _write_csv(tmp_path / "s_margin_empty.csv", empty_sbi_margin),
        ], "2026-08-10", dry_run=False, apply_records=True, db_path=db_path)

        assert not any(p["code_s"] == "4970" for p in ps.list_pending_in(db_path=db_path))
        assert any(a["action"] == "保留キューから削除" for a in result["applied"])

    def test_unregistered_code_registers_then_queues(self, tmp_path, db_path):
        """未登録銘柄は add_to_watch() で3監登録した上で保留キューへ (§5-3b)。"""
        assert ps.get_record("402A", db_path=db_path) is None

        result = ic.import_csvs(self._paths(tmp_path), "2026-08-10", dry_run=False,
                                apply_records=True, db_path=db_path)

        record = ps.get_record("402A", db_path=db_path)
        assert record is not None and record["status"] == "3監"
        log = ps.list_action_logs("402A", db_path=db_path)[0]
        assert log["action_type"] == "初回登録" and log["source"] == "csv_import"
        pending = ps.list_pending_in(db_path=db_path)
        assert any(p["code_s"] == "402A" and p["qty"] == 1500 for p in pending)
        applied = next(a for a in result["applied"] if a["code_s"] == "402A")
        assert applied["action"] == "登録+保留キューへ"
