"""stock_ratings.py のテスト (tmp_path 上に JSON を作る。本番の KS_DATA_DIR には触れない)"""

import json
from datetime import date

import pytest

import stock_ratings as sr

BASE_FIELDS = {
    "name": "SHIFT", "fund": 35, "mispricing": 16, "fair_price": 1800,
    "confidence": "A", "status": "Active", "thesis": "QA需要拡大", "risks": "採用鈍化",
    "checkpoints": "2Q受注", "mispricing_note": "受注残",
}


@pytest.fixture
def ratings_dir(tmp_path):
    """3697 と 6134 を登録済みのディレクトリ"""
    sr.update_rating("3697", BASE_FIELDS, "初期登録", "test", ratings_dir=tmp_path)
    sr.update_rating("6134", dict(BASE_FIELDS, name="FUJI"), "初期登録", "test", ratings_dir=tmp_path)
    return tmp_path


def _snapshot(ratings_dir):
    return tuple(
        (ratings_dir / name).read_bytes()
        for name in (sr.RATINGS_FILENAME, sr.HISTORY_FILENAME)
    )


def _history(ratings_dir):
    lines = (ratings_dir / sr.HISTORY_FILENAME).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


@pytest.mark.parametrize("fields, expected_changes", [
    ({"fund": 36}, {"scores.fund": [35, 36]}),
    ({"thesis": "AI駆動開発"}, {"thesis": ["QA需要拡大", "AI駆動開発"]}),
    ({"mispricing_note": ""}, {"mispricing_note": ["受注残", ""]}),
])
def test_partial_update_keeps_other_fields(ratings_dir, fields, expected_changes):
    """指定しなかった項目・他の銘柄は変わらず、変わった項目だけが履歴に残る"""
    before_3697 = sr.get_rating("3697", ratings_dir=ratings_dir)
    before_6134 = sr.get_rating("6134", ratings_dir=ratings_dir)

    changes = sr.update_rating("3697", fields, "2Q決算反映", "cli", ratings_dir=ratings_dir)

    assert changes == expected_changes
    after_3697 = sr.get_rating("3697", ratings_dir=ratings_dir)
    changed_keys = {k.split(".")[-1] for k in expected_changes}
    for key, value in before_3697.items():
        if key == "scores":
            for axis, score in value.items():
                if axis not in changed_keys:
                    assert after_3697["scores"][axis] == score
        elif key not in changed_keys | {"total", "updated_at"}:
            assert after_3697[key] == value
    assert sr.get_rating("6134", ratings_dir=ratings_dir) == before_6134
    last = _history(ratings_dir)[-1]
    assert (last["code_s"], last["source"], last["reason"], last["changes"]) == (
        "3697", "cli", "2Q決算反映", expected_changes)


@pytest.mark.parametrize("code_s, fields", [
    ("3697", {"fund": 41}),
    ("3697", {"fund": "36"}),
    ("3697", {"confidence": "S"}),
    ("3697", {"fundamental": 36}),
    ("3697", {"updated_at": "2026-01-01"}),
    ("369", {"fund": 36}),
    ("7203", {"name": "トヨタ", "fund": 30}),  # 新規作成で必須項目が足りない
    ("3697", {"momentum": 16}),  # Ver3 ではシステムが出す軸 (#544)
    ("3697", {"valuation": 15}),
    ("3697", {"fair_price": 0}),
])
def test_validation_error_writes_nothing(ratings_dir, code_s, fields):
    """検証エラーのとき JSON も履歴もバイト単位で変わらない"""
    before = _snapshot(ratings_dir)
    with pytest.raises(sr.RatingValidationError):
        sr.update_rating(code_s, fields, "誤入力", "cli", ratings_dir=ratings_dir)
    assert _snapshot(ratings_dir) == before


@pytest.mark.parametrize("fields, error", [
    ({"risks": " "}, "risks: Active では空にできない"),
    ({"status": "Watch", "checkpoints": ""}, "checkpoints: Watch では空にできない"),
    ({"fair_price": None}, "fair_price: Active では空にできない"),  # Valuation を出せなくなる (#544)
    ({"status": "Archive"}, "note: Archive にするときは"),  # 外す理由を同じ更新で書く
    ({"status": "Archive", "note": "受注鈍化で仮説崩れ"}, None),
])
def test_status_required_fields(ratings_dir, fields, error):
    """Status ごとに空にできない項目があり、Archive にするときは同じ更新で理由を書く (#542)"""
    before = _snapshot(ratings_dir)
    if error is None:
        sr.update_rating("3697", fields, "見送り", "cli", ratings_dir=ratings_dir)
        assert sr.get_rating("3697", ratings_dir=ratings_dir)["status"] == "Archive"
        return
    with pytest.raises(sr.RatingValidationError) as exc_info:
        sr.update_rating("3697", fields, "誤入力", "cli", ratings_dir=ratings_dir)
    assert any(e.startswith(error) for e in exc_info.value.errors)
    assert _snapshot(ratings_dir) == before


def test_archive_is_excluded_from_list_and_ranking(ratings_dir):
    """Archive は既定の一覧と HTML の順位から外れ、明示すれば一覧で取れる (#542)"""
    sr.update_rating("6134", {"fund": 40, "status": "Archive", "note": "見送り"}, "外す", "cli",
                     ratings_dir=ratings_dir)

    assert [r["code_s"] for r in sr.list_ratings(ratings_dir=ratings_dir)] == ["3697"]
    assert [r["code_s"] for r in sr.list_ratings("Archive", ratings_dir=ratings_dir)] == ["6134"]
    page = (ratings_dir / sr.HTML_FILENAME).read_text(encoding="utf-8")
    assert '<td class="num">1</td><td>3697</td>' in page
    assert '<td class="num">—</td><td>6134</td>' in page
    assert page.index('<details class="archive">') < page.index('href="#s6134"')


def test_same_value_update_writes_nothing(ratings_dir):
    """値が変わらない更新では JSON も履歴も書かない"""
    before = _snapshot(ratings_dir)
    assert sr.update_rating("3697", {"fund": 35, "name": "SHIFT"}, "再確認", "cli",
                            ratings_dir=ratings_dir) == {}
    assert _snapshot(ratings_dir) == before


def test_migrate_v3(tmp_path):
    """Ver2 のモメンタムと Valuation の点数を外して履歴に残し、版を 3.0 にする。再実行は中断する (#544)"""
    v2 = dict(BASE_FIELDS)
    v2.pop("fair_price")
    record = {k: v for k, v in v2.items() if k not in ("fund", "mispricing")}
    record["scores"] = {"fund": 35, "mispricing": 16, "momentum": 16, "valuation": 15}
    (tmp_path / sr.RATINGS_FILENAME).write_text(json.dumps(
        {"schema_version": 1, "rubric_version": "2.0", "stocks": {"3697": record}}), encoding="utf-8")

    assert sr.migrate_v3(ratings_dir=tmp_path) == 1

    data = json.loads((tmp_path / sr.RATINGS_FILENAME).read_text(encoding="utf-8"))
    assert data["rubric_version"] == "3.0"
    assert data["stocks"]["3697"]["scores"] == {"fund": 35, "mispricing": 16}
    last = _history(tmp_path)[-1]
    assert (last["source"], last["rubric_version"], last["changes"]) == (
        "migration", "3.0", {"scores.momentum": [16, None], "scores.valuation": [15, None]})
    with pytest.raises(FileExistsError):
        sr.migrate_v3(ratings_dir=tmp_path)


def test_update_regenerates_html(ratings_dir):
    """更新のたびに HTML が作り直され、Status ごとに総合点順で並び、文字列はエスケープされる"""
    sr.update_rating("6134", {"fund": 40, "thesis": "<b>受注</b> & 利益率"}, "反映", "cli",
                     ratings_dir=ratings_dir)
    sr.update_rating("4417", dict(BASE_FIELDS, name="GSX", status="Watch", fund=38),
                     "新規", "cli", ratings_dir=ratings_dir)

    page = (ratings_dir / sr.HTML_FILENAME).read_text(encoding="utf-8")
    assert page.index("<h2>Active") < page.index('href="#s6134"') < page.index('href="#s3697"')
    assert page.index('href="#s3697"') < page.index("<h2>Watch") < page.index('href="#s4417"')
    assert "&lt;b&gt;受注&lt;/b&gt; &amp; 利益率" in page and "<b>受注" not in page


def test_html_failure_does_not_block_update(ratings_dir, monkeypatch):
    """HTML の書き出しに失敗しても、JSON の更新と履歴の記録は成功する"""
    def broken(*args, **kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr(sr, "render_html", broken)
    sr.update_rating("3697", {"fund": 36}, "反映", "cli", ratings_dir=ratings_dir)

    assert sr.get_rating("3697", ratings_dir=ratings_dir)["scores"]["fund"] == 36
    assert _history(ratings_dir)[-1]["changes"] == {"scores.fund": [35, 36]}


@pytest.mark.parametrize("has_mirror, dev, expect_copy", [
    (True, False, True),
    (False, False, False),
    (True, True, False),   # 開発用データからは本番 Drive に出さない
])
def test_mirror_to_drive(tmp_path, monkeypatch, has_mirror, dev, expect_copy):
    """Drive ミラーがあれば既存ファイルへ上書きコピーする (inode = Drive のファイルIDを保つ)"""
    root, mirror = tmp_path / "stock_ratings", tmp_path / "stock_ratings_drive"
    monkeypatch.setattr(sr, "RATINGS_DIR", root)
    monkeypatch.setattr(sr, "DRIVE_MIRROR_DIR", mirror)
    monkeypatch.setattr(sr, "is_dev_data_dir", lambda: dev)
    if has_mirror:
        mirror.mkdir()
        (mirror / sr.RATINGS_FILENAME).write_text("{}", encoding="utf-8")
        inode = (mirror / sr.RATINGS_FILENAME).stat().st_ino

    sr.update_rating("3697", BASE_FIELDS, "初期登録", "test")

    if expect_copy:
        for name in (sr.HISTORY_FILENAME, sr.HTML_FILENAME):
            assert (mirror / name).read_bytes() == (root / name).read_bytes()
        # JSON は Drive の写しにだけシステムの2軸を埋め込む (#543 / #544)
        drive = json.loads((mirror / sr.RATINGS_FILENAME).read_text(encoding="utf-8"))
        local = json.loads((root / sr.RATINGS_FILENAME).read_text(encoding="utf-8"))
        assert drive["stocks"]["3697"].pop("system")["momentum"]["missing"] == ["rs", "theme"]
        assert drive == local and "system" not in local["stocks"]["3697"]
        assert (mirror / sr.RATINGS_FILENAME).stat().st_ino == inode
    elif has_mirror:
        assert (mirror / sr.RATINGS_FILENAME).read_text(encoding="utf-8") == "{}"
    else:
        assert not mirror.exists()


RANKS = {"半導体": {"rank": 1}, "AI": {"rank": 2}, "電線": {"rank": 3}, "未集計": {"rank": None}}
DAY = date(2026, 11, 6)


@pytest.mark.parametrize("pt, price, themes, ranks, fair, expected", [
    # モメンタム = RS (momentum_pt / 10) + テーマ (1位10点・最下位0点、2つなら平均) (#543)
    (83, (DAY, 1000), ["半導体"], RANKS, 1200, (10, [], 18, [])),
    (83, (DAY, 1000), ["半導体", "AI"], RANKS, 1200, (10, [], 16, [])),
    (83, (DAY, 1000), [], RANKS, 1200, (10, [], 13, ["theme"])),                    # 業態テーマ無し
    (83, (DAY, 1000), ["半導体"], {"半導体": {"rank": 1}}, 1200, (10, [], 13, ["theme"])),  # 比べるテーマが1つ
    (None, (DAY, 1000), ["AI"], RANKS, 1200, (10, [], 10, ["rs"])),                 # RS 無し
    # Valuation = 上昇余地を −20%→0 / +20%→10 / +60%→20 で換算 (#544)
    (83, (DAY, 1000), ["電線"], RANKS, 800, (0, [], 8, [])),
    (83, (DAY, 1000), ["電線"], RANKS, 1600, (20, [], 8, [])),
    (83, (DAY, 1000), ["電線"], RANKS, 3000, (20, [], 8, [])),                      # 上限で切り詰め
    (83, (DAY, 1000), ["電線"], RANKS, None, (10, ["fair_price"], 8, [])),
    # 終値が基準日のものでなければ、RS も終値も使わない
    (83, (date(2026, 11, 5), 1000), ["電線"], RANKS, 1200, (10, ["price_stale"], 5, ["rs_stale"])),
])
def test_system_scores(monkeypatch, pt, price, themes, ranks, fair, expected):
    """システムが出す2軸は、基準日 (テーマ順位の記録日) の RS と終値だけを使う"""
    monkeypatch.setattr(sr, "_load_system_inputs", lambda codes: (
        {"3697": {"momentum_pt": pt, "price": price}}, {"3697": themes}, ranks, DAY))
    s = sr.compute_system_scores({"3697": {"fair_price": fair}})["3697"]
    v, m = s["valuation"], s["momentum"]
    assert (v["score"], v["missing"], m["total"], m["missing"]) == expected
    assert s["as_of"] == "2026-11-06"


def test_total_includes_system_scores(ratings_dir, monkeypatch):
    """総合点 = AI の2軸 + Valuation + モメンタムで、一覧はその順に並ぶ。読めなくても総合点は出る"""
    monkeypatch.setattr(sr, "_load_system_inputs", lambda codes: (
        {"3697": {"momentum_pt": 83, "price": (DAY, 1000)}, "6134": {"momentum_pt": 20, "price": (DAY, 1000)}},
        {"3697": ["半導体"], "6134": ["電線"]}, RANKS, DAY))
    # 3697: 35 + 16 + Val 20 (1800円 / 1000円) + モメ 18 = 89、6134: 35 + 16 + 20 + (2 + 0) = 73
    assert [(r["code_s"], r["total"]) for r in sr.list_ratings(ratings_dir=ratings_dir)] == [
        ("3697", 89), ("6134", 73)]

    def broken(codes):
        raise OSError("shelve を開けない")
    monkeypatch.setattr(sr, "_load_system_inputs", broken)
    assert sr.get_rating("3697", ratings_dir=ratings_dir)["total"] == 35 + 16 + 10 + 10


def test_history_keeps_system_scores_at_update(ratings_dir, monkeypatch):
    """更新した時点のシステムの2軸と採点ルールの版が履歴に残り、正本には入らない"""
    snapshot = {"as_of": "2026-11-06", "valuation": {"score": 14}, "momentum": {"total": 12}}
    monkeypatch.setattr(sr, "compute_system_scores", lambda records: {c: snapshot for c in records})
    monkeypatch.setattr(sr, "_with_system", lambda code_s, record, system: dict(record, code_s=code_s))
    sr.update_rating("3697", {"fund": 36}, "反映", "cli", ratings_dir=ratings_dir)
    last = _history(ratings_dir)[-1]
    assert (last["system"], last["rubric_version"], last["changes"]) == (
        snapshot, "3.0", {"scores.fund": [35, 36]})
    stored = json.loads((ratings_dir / sr.RATINGS_FILENAME).read_text(encoding="utf-8"))
    assert "system" not in stored["stocks"]["3697"]


@pytest.mark.parametrize("status, updated, held, kessan, reasons", [
    ("Watch", "2026-10-01", {}, [], ["Watch なのにポートフォリオに無い"]),
    ("Active", "2026-10-01", {"1111": "3監"}, [], ["Active なのに3監"]),
    ("Active", "2026-10-01", {"1111": "1保"}, ["2026-10-20"], []),       # Active は決算の後でも候補にしない
    ("Watch", "2026-10-01", {"1111": "2準"}, ["2026-09-20", "2026-10-20", "2026-12-01"],
     ["10/20 の決算の後に未更新"]),                                       # 今日より後の決算は数えない
    ("Watch", "2026-07-01", {"1111": "2準"}, [], ["最後の更新が 2026-07-01"]),  # 90日以上前
    ("Watch", "2026-08-15", {"1111": "2準"}, [], []),                    # 88日前はまだ
])
def test_archive_candidates(status, updated, held, kessan, reasons):
    """外す候補は理由つきで返し、どれにも当たらなければ返さない"""
    rows = [{"code_s": "1111", "name": "X", "status": status, "updated_at": updated}]
    got = sr.archive_candidates(rows, held, {"1111": kessan}, date(2026, 11, 11))
    assert [c["reasons"] for c in got] == ([reasons] if reasons else [])


def test_archive_candidates_order():
    """理由の多い順、同じなら更新の古い順"""
    # 3333 はポートフォリオに無く更新も古い (2つ)。2222・4444 はポートフォリオに無いだけ (1つ)
    rows = [{"code_s": c, "name": c, "status": "Watch", "updated_at": u}
            for c, u in (("4444", "2026-10-01"), ("2222", "2026-09-01"), ("3333", "2026-06-01"))]
    got = sr.archive_candidates(rows, {}, {}, date(2026, 11, 11))
    assert [c["code_s"] for c in got] == ["3333", "2222", "4444"]
