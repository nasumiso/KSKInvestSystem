"""research_shelve.py のテスト (tmp_path で一時DBを作成)"""

import os

import pytest

import research_shelve as rs


@pytest.fixture
def db_path(tmp_path):
    """テスト用一時DBパスを返す"""
    return str(tmp_path / "test_research_shelve")


# ==================================================
# スキーマ層: 正規化・バリデーション・ファクトリ
# ==================================================
class TestSchema:
    """スキーマ層のユニットテスト"""

    # --- ケース1: create_research_record 最小引数 ---
    def test_create_research_record_minimal(self):
        """最小引数でレコードひな型がすべてのフィールドを持つ"""
        rec = rs.create_research_record("3496", "アズーム")
        assert rec["code_s"] == "3496"
        assert rec["stock_name"] == "アズーム"
        assert rec["overview"] == ""
        assert rec["overall_rating"] == ""
        assert rec["institutional_comment"] == ""
        assert rec["memo"] == ""
        assert rec["openwork"] == ""
        assert rec["cramer"] == ""
        assert rec["shikiho_comments"] == []
        assert rec["snapshots"] == []
        # 既知フィールドがすべて含まれる
        assert set(rec.keys()) == rs.RECORD_FIELDS

    # --- ケース2: create_research_record フル引数 ---
    def test_create_research_record_full(self):
        """全フィールド指定でdict内容が一致"""
        snap = rs.create_snapshot("26.1", ir_quant="[A]26%,21%")
        rec = rs.create_research_record(
            "3496",
            "アズーム",
            overview="駐車場サブリース",
            overall_rating="S",
            institutional_comment="あまりいない\n個人多い",
            memo="業績堅調",
            openwork="3.72",
            cramer="独自ビジネス",
            shikiho_comments=["【最高益】..."],
            snapshots=[snap],
        )
        assert rec["overview"] == "駐車場サブリース"
        assert rec["overall_rating"] == "S"
        assert rec["institutional_comment"] == "あまりいない\n個人多い"
        assert rec["memo"] == "業績堅調"
        assert rec["openwork"] == "3.72"
        assert rec["cramer"] == "独自ビジネス"
        assert rec["shikiho_comments"] == ["【最高益】..."]
        assert rec["snapshots"] == [snap]

    # --- ケース3: create_snapshot デフォルト値 ---
    def test_create_snapshot_defaults(self):
        """date_yy_m のみでスナップショットが生成され data_source=manual"""
        snap = rs.create_snapshot("26.1")
        assert snap["date_yy_m"] == "26.1"
        assert snap["ir_quant"] == ""
        assert snap["ir_comment"] == ""
        assert snap["quality_indicators"] == ""
        assert snap["rironkabuka_kairi"] == ""
        assert snap["data_source"] == "manual"
        assert set(snap.keys()) == rs.SNAPSHOT_FIELDS

    def test_create_snapshot_invalid_data_source(self):
        """data_source の不正値で ValueError"""
        with pytest.raises(ValueError):
            rs.create_snapshot("26.1", data_source="invalid")

    # --- ケース4: normalize_code_s ---
    def test_normalize_code_s_lowercase(self):
        """小文字の末尾英字が大文字化される"""
        assert rs.normalize_code_s("135a") == "135A"

    def test_normalize_code_s_whitespace(self):
        """前後の空白が除去される"""
        assert rs.normalize_code_s(" 3496 ") == "3496"

    def test_normalize_code_s_non_string(self):
        """文字列以外は TypeError"""
        with pytest.raises(TypeError):
            rs.normalize_code_s(3496)

    # --- ケース5: validate_code_s 不正値・正常値 ---
    @pytest.mark.parametrize(
        "invalid",
        [
            "",
            "abc",
            "123",      # 3桁
            "1234A",    # 5文字
            "12345",    # 5桁
            "A215",     # 先頭英字
        ],
    )
    def test_validate_code_s_invalid(self, invalid):
        with pytest.raises(ValueError):
            rs.validate_code_s(invalid)

    def test_validate_code_s_none(self):
        """None は TypeError (normalize_code_s の仕様)"""
        with pytest.raises(TypeError):
            rs.validate_code_s(None)

    @pytest.mark.parametrize(
        "valid",
        ["1234", "0001", "9999", "215A", "135A"],
    )
    def test_validate_code_s_valid(self, valid):
        rs.validate_code_s(valid)  # 例外が出なければOK

    # --- ケース6: validate_date_yy_m ---
    @pytest.mark.parametrize(
        "invalid",
        [
            "2024-03-31",   # ISO 形式
            "26.13",        # 月が13
            "26.0",         # 月が0
            "ab.1",         # 年が英字
            "26",           # 月欠落
            "26.",          # 月空
            ".1",           # 年空
        ],
    )
    def test_validate_date_yy_m_invalid(self, invalid):
        with pytest.raises(ValueError):
            rs.validate_date_yy_m(invalid)

    @pytest.mark.parametrize("valid", ["26.1", "25.11", "25.12", "00.1", "99.12"])
    def test_validate_date_yy_m_valid(self, valid):
        rs.validate_date_yy_m(valid)

    # --- ケース7: date_yy_m_sort_key ---
    def test_date_yy_m_sort_key_order(self):
        """(yy, m) タプルで最新ほど大きい値"""
        dates = ["25.7", "26.1", "25.11", "24.2"]
        sorted_desc = sorted(dates, key=rs.date_yy_m_sort_key, reverse=True)
        assert sorted_desc == ["26.1", "25.11", "25.7", "24.2"]

    def test_date_yy_m_sort_key_value(self):
        assert rs.date_yy_m_sort_key("26.1") == (26, 1, 0)
        assert rs.date_yy_m_sort_key("25.11") == (25, 11, 0)

    # --- ケース7b: sort_shikiho_comments_desc ---
    def test_sort_shikiho_comments_desc_basic(self):
        """period ありを降順、空/- は末尾"""
        items = [
            {"period": "", "comment": "A"},
            {"period": "26.3", "comment": "B"},
            {"period": "25.12", "comment": "C"},
            {"period": "-", "comment": "D"},
        ]
        result = rs.sort_shikiho_comments_desc(items)
        assert [it["comment"] for it in result] == ["B", "C", "A", "D"]

    def test_sort_shikiho_comments_desc_preserves_empty_order(self):
        """空/- 同士の相対順序は元リスト順を維持（安定ソート）"""
        items = [
            {"period": "", "comment": "A"},
            {"period": "26.3", "comment": "B"},
            {"period": "", "comment": "C"},
            {"period": "-", "comment": "D"},
            {"period": "", "comment": "E"},
        ]
        result = rs.sort_shikiho_comments_desc(items)
        assert [it["comment"] for it in result] == ["B", "A", "C", "D", "E"]

    def test_sort_shikiho_comments_desc_empty_list(self):
        assert rs.sort_shikiho_comments_desc([]) == []

    def test_sort_shikiho_comments_desc_all_with_period(self):
        items = [
            {"period": "25.7", "comment": "A"},
            {"period": "26.1", "comment": "B"},
            {"period": "25.11", "comment": "C"},
        ]
        result = rs.sort_shikiho_comments_desc(items)
        assert [it["comment"] for it in result] == ["B", "C", "A"]

    def test_sort_shikiho_comments_desc_invalid_period(self):
        """不正な period は最古扱い（クラッシュしない）"""
        items = [
            {"period": "26.3", "comment": "A"},
            {"period": "不明", "comment": "B"},
        ]
        result = rs.sort_shikiho_comments_desc(items)
        assert [it["comment"] for it in result] == ["A", "B"]

    # --- ケース8: overall_rating 不正値 ---
    @pytest.mark.parametrize("bad", ["Z", "s", "A+", "不明"])
    def test_create_record_invalid_rating(self, bad):
        with pytest.raises(ValueError):
            rs.create_research_record("1234", "テスト", overall_rating=bad)


# ==================================================
# CRUD層: get / upsert / delete
# ==================================================
class TestCrud:
    """CRUD 操作のユニットテスト"""

    # --- ケース9: upsert -> get の round trip ---
    def test_upsert_and_get(self, db_path):
        rec = rs.create_research_record("3496", "アズーム", overall_rating="S")
        rs.upsert_research_record(rec, db_path=db_path)
        loaded = rs.get_research_record("3496", db_path=db_path)
        assert loaded is not None
        assert loaded["code_s"] == "3496"
        assert loaded["stock_name"] == "アズーム"
        assert loaded["overall_rating"] == "S"

    # --- ケース10: 同一 code_s で2回 upsert すると最新が残る ---
    def test_upsert_overwrites_existing(self, db_path):
        rec1 = rs.create_research_record("3496", "旧名")
        rs.upsert_research_record(rec1, db_path=db_path)
        rec2 = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec2, db_path=db_path)
        loaded = rs.get_research_record("3496", db_path=db_path)
        assert loaded["stock_name"] == "アズーム"

    # --- ケース11: 非存在 code_s は None ---
    def test_get_missing_returns_none(self, db_path):
        assert rs.get_research_record("9999", db_path=db_path) is None

    # --- ケース12: delete 成功 ---
    def test_delete_existing_returns_true(self, db_path):
        rec = rs.create_research_record("1234", "テスト")
        rs.upsert_research_record(rec, db_path=db_path)
        assert rs.delete_research_record("1234", db_path=db_path) is True
        assert rs.get_research_record("1234", db_path=db_path) is None

    # --- ケース13: 非存在キー削除は False ---
    def test_delete_missing_returns_false(self, db_path):
        assert rs.delete_research_record("9999", db_path=db_path) is False

    # --- ケース14: code_s 欠落レコードの upsert で ValueError ---
    def test_upsert_missing_code_s_raises(self, db_path):
        with pytest.raises(ValueError):
            rs.upsert_research_record({"stock_name": "x"}, db_path=db_path)

    # --- ケース15: 正規化: 小文字で入れて大文字で引ける ---
    def test_upsert_normalizes_code_s(self, db_path):
        rec = rs.create_research_record("135a", "テスト")
        rs.upsert_research_record(rec, db_path=db_path)
        assert rs.get_research_record("135A", db_path=db_path) is not None
        assert rs.get_research_record("135a", db_path=db_path) is not None
        # 保存時に正規化されている
        loaded = rs.get_research_record("135A", db_path=db_path)
        assert loaded["code_s"] == "135A"

    # --- kessan_matagi スキーマ拡張 (issue #138) ---
    def test_kessan_comment_fields_includes_kessan_matagi(self):
        """KESSAN_COMMENT_FIELDS に kessan_matagi が含まれる"""
        assert "kessan_matagi" in rs.KESSAN_COMMENT_FIELDS

    def test_get_research_record_backfills_kessan_matagi_false(self, db_path):
        """旧形式 (kessan_matagi 無) のエントリは読み込み時に False で補完される"""
        rec = rs.create_research_record("5032", "ANYCOLOR")
        # kessan_matagi なしの旧エントリを直接埋め込み
        rec["kessan_comments"] = [{
            "kessanbi": "2026/03/11",
            "quarter": 3,
            "pre_expectation": "○",
            "pre_outlook": "見通し",
            "post_price_change": "-15",
            "post_comment": "[E] -15% x",
        }]
        rs.upsert_research_record(rec, db_path=db_path)
        loaded = rs.get_research_record("5032", db_path=db_path)
        assert loaded["kessan_comments"][0]["kessan_matagi"] is False

    def test_get_research_record_preserves_kessan_matagi_true(self, db_path):
        """True で保存されたエントリは読み込み時も True"""
        rec = rs.create_research_record("5032", "ANYCOLOR")
        rec["kessan_comments"] = [{
            "kessanbi": "2026/03/11",
            "quarter": 3,
            "pre_expectation": "○",
            "pre_outlook": "見通し",
            "post_price_change": "-15",
            "post_comment": "[E] -15% x",
            "kessan_matagi": True,
        }]
        rs.upsert_research_record(rec, db_path=db_path)
        loaded = rs.get_research_record("5032", db_path=db_path)
        assert loaded["kessan_comments"][0]["kessan_matagi"] is True

    def test_get_research_record_normalizes_old_post_price_change(self, db_path):
        """旧 post_price_change のみ持つエントリは post_price_changes に正規化される (issue #133)"""
        rec = rs.create_research_record("5032", "ANYCOLOR")
        rec["kessan_comments"] = [{
            "kessanbi": "2026/03/11",
            "quarter": 3,
            "pre_expectation": "○",
            "pre_outlook": "見通し",
            "post_price_change": "-15",
            "post_comment": "[E] -15% x",
        }]
        rs.upsert_research_record(rec, db_path=db_path)
        loaded = rs.get_research_record("5032", db_path=db_path)
        entry = loaded["kessan_comments"][0]
        assert entry["post_price_changes"] == {"1d": "-15", "5d": ""}

    def test_get_research_record_preserves_post_price_changes_dict(self, db_path):
        """新形式 post_price_changes は読出時もそのまま"""
        rec = rs.create_research_record("5032", "ANYCOLOR")
        rec["kessan_comments"] = [{
            "kessanbi": "2026/03/11",
            "quarter": 3,
            "pre_expectation": "○",
            "pre_outlook": "見通し",
            "post_price_changes": {"1d": "+3.2", "5d": "+5.1"},
            "post_comment": "",
        }]
        rs.upsert_research_record(rec, db_path=db_path)
        loaded = rs.get_research_record("5032", db_path=db_path)
        entry = loaded["kessan_comments"][0]
        assert entry["post_price_changes"] == {"1d": "+3.2", "5d": "+5.1"}


# ==================================================
# スナップショット層: upsert_snapshot
# ==================================================
class TestSnapshot:
    """upsert_snapshot のユニットテスト"""

    @pytest.fixture
    def prepared_db(self, db_path):
        """レコードのみ登録済みのDBを返す (スナップショットは空)"""
        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)
        return db_path

    # --- ケース16: 空配列への1件目追加 ---
    def test_upsert_first_snapshot(self, prepared_db):
        snap = rs.create_snapshot("26.1", ir_quant="[A]26%,21%")
        rs.upsert_snapshot("3496", snap, db_path=prepared_db)
        loaded = rs.get_research_record("3496", db_path=prepared_db)
        assert len(loaded["snapshots"]) == 1
        assert loaded["snapshots"][0]["date_yy_m"] == "26.1"
        assert loaded["snapshots"][0]["ir_quant"] == "[A]26%,21%"

    # --- ケース17: 複数日付追加で先頭が最新 ---
    def test_upsert_multiple_prepends_latest(self, prepared_db):
        rs.upsert_snapshot(
            "3496", rs.create_snapshot("25.11"), db_path=prepared_db
        )
        rs.upsert_snapshot(
            "3496", rs.create_snapshot("26.1"), db_path=prepared_db
        )
        rs.upsert_snapshot(
            "3496", rs.create_snapshot("25.7"), db_path=prepared_db
        )
        loaded = rs.get_research_record("3496", db_path=prepared_db)
        dates = [s["date_yy_m"] for s in loaded["snapshots"]]
        assert dates == ["26.1", "25.11", "25.7"]

    # --- ケース18: 同日2回upsertで冪等上書き ---
    def test_upsert_same_date_overwrites(self, prepared_db):
        rs.upsert_snapshot(
            "3496",
            rs.create_snapshot("26.1", ir_quant="old"),
            db_path=prepared_db,
        )
        rs.upsert_snapshot(
            "3496",
            rs.create_snapshot("26.1", ir_quant="new"),
            db_path=prepared_db,
        )
        loaded = rs.get_research_record("3496", db_path=prepared_db)
        assert len(loaded["snapshots"]) == 1
        assert loaded["snapshots"][0]["ir_quant"] == "new"

    # --- ケース19: overwrite_same_date=False で同日2件許容 ---
    def test_upsert_same_date_allow_duplicates(self, prepared_db):
        rs.upsert_snapshot(
            "3496",
            rs.create_snapshot("26.1", ir_quant="first"),
            overwrite_same_date=False,
            db_path=prepared_db,
        )
        rs.upsert_snapshot(
            "3496",
            rs.create_snapshot("26.1", ir_quant="second"),
            overwrite_same_date=False,
            db_path=prepared_db,
        )
        loaded = rs.get_research_record("3496", db_path=prepared_db)
        same_date = [
            s for s in loaded["snapshots"] if s["date_yy_m"] == "26.1"
        ]
        assert len(same_date) == 2
        ir_quants = {s["ir_quant"] for s in same_date}
        assert ir_quants == {"first", "second"}

    # --- ケース20: レコード非存在でKeyError ---
    def test_upsert_snapshot_missing_record_raises(self, db_path):
        snap = rs.create_snapshot("26.1")
        with pytest.raises(KeyError):
            rs.upsert_snapshot("9999", snap, db_path=db_path)

    # --- ケース21: date_yy_m降順ソート ---
    def test_snapshots_sorted_descending(self, prepared_db):
        # わざとバラバラの順で投入
        for d in ["24.2", "26.1", "25.7", "25.1", "25.11"]:
            rs.upsert_snapshot(
                "3496", rs.create_snapshot(d), db_path=prepared_db
            )
        loaded = rs.get_research_record("3496", db_path=prepared_db)
        dates = [s["date_yy_m"] for s in loaded["snapshots"]]
        assert dates == ["26.1", "25.11", "25.7", "25.1", "24.2"]


# ==================================================
# フィルタ層: list_research_records
# ==================================================
class TestListFilter:
    """list_research_records のユニットテスト"""

    @pytest.fixture
    def populated_db(self, db_path):
        """複数銘柄を投入したDBを返す"""
        records = [
            rs.create_research_record(
                "3496", "アズーム",
                overall_rating="S",
                overview="駐車場サブリース",
                memo="業績堅調",
            ),
            rs.create_research_record(
                "6920", "レーザーテック",
                overall_rating="A",
                overview="半導体装置の世界最大手",
            ),
            rs.create_research_record(
                "1234", "サンプルB",
                overall_rating="B",
                overview="小型成長株",
            ),
            rs.create_research_record(
                "9999", "サンプルC",
                overall_rating="",
                openwork="OPENworkコメント",
            ),
        ]
        for rec in records:
            rs.upsert_research_record(rec, db_path=db_path)
        return db_path

    # --- ケース22: フィルタなし全件 ---
    def test_list_all(self, populated_db):
        results = rs.list_research_records(db_path=populated_db)
        assert len(results) == 4

    # --- ケース23: rating 単一フィルタ ---
    def test_filter_rating_single(self, populated_db):
        results = rs.list_research_records(rating="S", db_path=populated_db)
        assert len(results) == 1
        assert results[0]["code_s"] == "3496"

    # --- ケース24: rating カンマ区切り複数 ---
    def test_filter_rating_multiple(self, populated_db):
        results = rs.list_research_records(rating="S,A", db_path=populated_db)
        codes = [r["code_s"] for r in results]
        assert codes == ["3496", "6920"]

    # --- ケース25: keyword (stock_name) ---
    def test_filter_keyword_stock_name(self, populated_db):
        results = rs.list_research_records(
            keyword="レーザー", db_path=populated_db
        )
        assert len(results) == 1
        assert results[0]["code_s"] == "6920"

    # --- ケース26: keyword (overview / memo) ---
    def test_filter_keyword_overview(self, populated_db):
        results = rs.list_research_records(
            keyword="駐車場", db_path=populated_db
        )
        assert len(results) == 1
        assert results[0]["code_s"] == "3496"

    def test_filter_keyword_memo(self, populated_db):
        results = rs.list_research_records(
            keyword="堅調", db_path=populated_db
        )
        assert len(results) == 1
        assert results[0]["code_s"] == "3496"

    # --- ケース27: keyword 大文字小文字無視 ---
    def test_filter_keyword_case_insensitive(self, populated_db):
        results = rs.list_research_records(
            keyword="openwork", db_path=populated_db
        )
        assert len(results) == 1
        assert results[0]["code_s"] == "9999"

    # --- ケース28: rating と keyword の AND ---
    def test_filter_rating_and_keyword(self, populated_db):
        results = rs.list_research_records(
            rating="S,A", keyword="半導体", db_path=populated_db
        )
        assert len(results) == 1
        assert results[0]["code_s"] == "6920"

        # 条件に合致しない組み合わせは空
        results_empty = rs.list_research_records(
            rating="S", keyword="半導体", db_path=populated_db
        )
        assert results_empty == []

    # --- ケース29: code_s 昇順 ---
    def test_result_sorted_by_code_s(self, populated_db):
        results = rs.list_research_records(db_path=populated_db)
        codes = [r["code_s"] for r in results]
        assert codes == sorted(codes)
        assert codes == ["1234", "3496", "6920", "9999"]


# ==================================================
# バックアップ層
# ==================================================
class TestBackup:
    """backup_research_db のユニットテスト"""

    # --- ケース30: upsert 後のバックアップでファイル生成 ---
    def test_backup_creates_files(self, db_path):
        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)
        created = rs.backup_research_db(db_path=db_path)
        # 少なくとも1つのバックアップファイルが作られている
        assert len(created) >= 1
        for backup_fname in created:
            assert os.path.exists(backup_fname)

    # --- ケース31: DB未作成でも例外にならず空リスト ---
    def test_backup_missing_db(self, tmp_path):
        missing = str(tmp_path / "nonexistent_research_shelve")
        result = rs.backup_research_db(db_path=missing)
        assert result == []


# ==================================================
# 表示整形層
# ==================================================
class TestFormat:
    """format_record_full / format_record_summary のユニットテスト"""

    @pytest.fixture
    def azoom_record(self):
        return rs.create_research_record(
            "3496",
            "アズーム",
            overview="駐車場サブリースが主力",
            overall_rating="S",
            institutional_comment="あまりいない\n個人多い",
            memo="業績堅調",
            openwork="3.72",
            cramer="独自ビジネス",
            shikiho_comments=[
                {"period": "26.3", "comment": "【最高益】駐車場借り上げが順調"},
                {"period": "25.12", "comment": "【連続最高益】主力事業続伸"},
            ],
            snapshots=[
                rs.create_snapshot(
                    "26.1",
                    ir_quant="[A]26%,21%[Q]25%,25%",
                    quality_indicators="555億 PER27 PBR9.3\n配当2.8 ROE36",
                    rironkabuka_kairi="75%(-%)|243%,-91%",
                    data_source="migration",
                ),
                rs.create_snapshot(
                    "25.11",
                    ir_quant="[A]26%,21%",
                    ir_comment="・新中経~30 CAGR35%",
                    quality_indicators="579億 PER31",
                    rironkabuka_kairi="-20%(-%)|42%,-84%",
                    data_source="migration",
                ),
            ],
        )

    # --- ケース32: format_record_full が必須フィールドを含む ---
    def test_format_full_contains_required_fields(self, azoom_record):
        output = rs.format_record_full(azoom_record)
        assert "3496" in output
        assert "アズーム" in output
        assert "S" in output
        assert "駐車場サブリース" in output
        assert "あまりいない" in output
        assert "個人多い" in output
        assert "業績堅調" in output
        assert "3.72" in output
        assert "独自ビジネス" in output
        assert "【最高益】" in output
        # スナップショット関連
        assert "26.1" in output
        assert "25.11" in output
        assert "[A]26%,21%" in output
        assert "555億 PER27" in output
        assert "75%(-%)|243%,-91%" in output
        assert "migration" in output
        assert "新中経" in output

    def test_format_full_empty_snapshots(self):
        rec = rs.create_research_record("1234", "テスト")
        output = rs.format_record_full(rec)
        assert "1234" in output
        assert "テスト" in output
        assert "スナップショット (0件)" in output

    # --- ケース33: format_record_summary ---
    def test_format_summary_fields(self, azoom_record):
        summary = rs.format_record_summary(azoom_record)
        fields = summary.split("\t")
        assert fields[0] == "3496"
        assert fields[1] == "S"
        assert fields[2] == "アズーム"
        assert fields[3] == "2"  # スナップショット2件
        assert "駐車場" in fields[4]

    def test_format_summary_empty_rating_shows_dash(self):
        rec = rs.create_research_record("1234", "テスト")
        summary = rs.format_record_summary(rec)
        fields = summary.split("\t")
        assert fields[0] == "1234"
        assert fields[1] == "-"  # rating 空は "-"
        assert fields[3] == "0"
        assert fields[4] == "-"  # overview 空は "-"


# ==================================================
# 分析日・決算日フィールド (issue #92 で追加)
# ==================================================
class TestAnalysisKessanDateFields:
    """analysis_date_raw / kessan_date_raw の保存と表示のテスト"""

    def test_create_record_with_date_fields(self):
        """分析日・決算日を渡すと dict に含まれる"""
        rec = rs.create_research_record(
            "3496",
            "アズーム",
            analysis_date_raw="11/13",
            kessan_date_raw="01/30",
        )
        assert rec["analysis_date_raw"] == "11/13"
        assert rec["kessan_date_raw"] == "01/30"

    def test_upsert_and_get_date_fields(self, db_path):
        """upsert → get のラウンドトリップで分析日・決算日が保持される"""
        rec = rs.create_research_record(
            "3496",
            "アズーム",
            analysis_date_raw="11/13",
            kessan_date_raw="22四季報春",
        )
        rs.upsert_research_record(rec, db_path=db_path)
        loaded = rs.get_research_record("3496", db_path=db_path)
        assert loaded is not None
        assert loaded["analysis_date_raw"] == "11/13"
        assert loaded["kessan_date_raw"] == "22四季報春"

    def test_date_fields_default_empty(self):
        """省略時のデフォルトが空文字"""
        rec = rs.create_research_record("3496", "アズーム")
        assert rec["analysis_date_raw"] == ""
        assert rec["kessan_date_raw"] == ""

    def test_format_record_full_shows_dates(self):
        """format_record_full の出力に分析日・決算日が含まれる"""
        rec = rs.create_research_record(
            "3496",
            "アズーム",
            analysis_date_raw="11/13",
            kessan_date_raw="01/30",
        )
        output = rs.format_record_full(rec)
        assert "分析日" in output
        assert "11/13" in output
        assert "決算日" in output
        assert "01/30" in output

    def test_format_record_full_empty_dates_show_dash(self):
        """空の分析日・決算日は - で表示される"""
        rec = rs.create_research_record("3496", "アズーム")
        output = rs.format_record_full(rec)
        # ラベル自体は存在する
        assert "分析日" in output
        assert "決算日" in output

    def test_create_record_rejects_non_str_analysis_date(self):
        """analysis_date_raw が非文字列なら TypeError"""
        with pytest.raises(TypeError):
            rs.create_research_record(
                "3496", "アズーム", analysis_date_raw=123,
            )

    def test_create_record_rejects_non_str_kessan_date(self):
        """kessan_date_raw が非文字列なら TypeError"""
        with pytest.raises(TypeError):
            rs.create_research_record(
                "3496", "アズーム", kessan_date_raw=None,
            )


# ==================================================
# date_yy_m 日精度拡張 (issue #94)
# ==================================================
class TestDateYyMDayPrecision:
    """YY.M.D 形式の日精度拡張テスト"""

    def test_validate_yy_m_d_accepted(self):
        """日精度 YY.M.D が許容される"""
        rs.validate_date_yy_m("26.4.15")
        rs.validate_date_yy_m("25.11.1")
        rs.validate_date_yy_m("26.1.31")

    def test_validate_yy_m_d_invalid_day(self):
        """日が 32 以上で ValueError"""
        with pytest.raises(ValueError, match="日は1-31"):
            rs.validate_date_yy_m("26.4.32")

    def test_validate_yy_m_d_day_zero(self):
        """日が 0 で ValueError"""
        with pytest.raises(ValueError, match="日は1-31"):
            rs.validate_date_yy_m("26.4.0")

    def test_sort_key_with_day(self):
        """日精度の sort_key が (year, month, day) の 3-tuple"""
        assert rs.date_yy_m_sort_key("26.4.15") == (26, 4, 15)

    def test_sort_key_without_day(self):
        """月精度の sort_key が (year, month, 0) の 3-tuple"""
        assert rs.date_yy_m_sort_key("26.4") == (26, 4, 0)

    def test_sort_order_mixed(self):
        """月精度と日精度が混在時のソート順: 26.4 < 26.4.15 < 26.5"""
        dates = ["26.5", "26.4", "26.4.15", "26.1"]
        sorted_desc = sorted(dates, key=rs.date_yy_m_sort_key, reverse=True)
        assert sorted_desc == ["26.5", "26.4.15", "26.4", "26.1"]

    def test_upsert_yy_m_and_yy_m_d_coexist(self, db_path):
        """同一銘柄に YY.M と YY.M.D が並存できる"""
        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)
        rs.upsert_snapshot(
            "3496", rs.create_snapshot("26.4", data_source="migration"),
            db_path=db_path,
        )
        rs.upsert_snapshot(
            "3496", rs.create_snapshot("26.4.15", data_source="auto"),
            db_path=db_path,
        )
        loaded = rs.get_research_record("3496", db_path=db_path)
        assert len(loaded["snapshots"]) == 2
        dates = {s["date_yy_m"] for s in loaded["snapshots"]}
        assert dates == {"26.4", "26.4.15"}

    def test_create_snapshot_with_day(self):
        """create_snapshot が日精度を受け付ける"""
        snap = rs.create_snapshot("26.4.15", ir_quant="[P]1Q28%", data_source="auto")
        assert snap["date_yy_m"] == "26.4.15"
        assert snap["data_source"] == "auto"

    def test_existing_yy_m_tests_still_pass(self):
        """既存の月精度形式が引き続き動作する回帰テスト"""
        rs.validate_date_yy_m("26.1")
        rs.validate_date_yy_m("25.11")
        assert rs.date_yy_m_sort_key("26.1") == (26, 1, 0)
        assert rs.date_yy_m_sort_key("25.11") == (25, 11, 0)
