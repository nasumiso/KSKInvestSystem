"""make_market_db の機能テスト

update_market_db() → create_market_csv() の一連フローを結合テストする。
HTTP通信・Google Driveはモック、shelve DB・CSV出力は tmp_path で実行。
"""

import pytest
import os
import csv
from datetime import datetime, timedelta, date
from unittest.mock import patch, MagicMock

import make_market_db
import db_shelve

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load_fixture(name):
    """フィクスチャHTMLを読み込む"""
    with open(os.path.join(FIXTURES_DIR, name), encoding="utf-8") as f:
        return f.read()


def _make_index_db_dict():
    """市場指数（TOPIX等）の最小限のデータ"""
    return {
        "rs_raw": 1.1,
        "price": 2800,
        "trend_template": [],
        "distribution_days": ["26/03/03", "26/03/09"],
        "followthrough_days": ["26/03/05"],
        "direction_signal": "buy,26/03/10",
        "spr_buygagher": 52,
        "spr_20": 50,
        "spr_5": 48,
        "rv_20": 3.5,
        "rv_5": 3.2,
    }


def _setup_tmp_dirs(tmp_path):
    """テスト用ディレクトリ構成を作成"""
    market_data_dir = tmp_path / "market_data"
    market_data_dir.mkdir()
    code_rank_dir = tmp_path / "code_rank_data"
    code_rank_dir.mkdir()
    return market_data_dir, code_rank_dir


def _write_theme_rank_cache(market_data_dir, html, backup_name=None):
    """テーマランクHTMLをキャッシュファイルとして配置"""
    cache_path = market_data_dir / "theme_rank.html"
    cache_path.write_text(html, encoding="utf-8")
    if backup_name:
        backup_path = market_data_dir / backup_name
        backup_path.write_text(html, encoding="utf-8")
    return str(cache_path)


def _read_csv(csv_path):
    """CSVを読み込んでリストのリストとして返す"""
    with open(csv_path, encoding="utf-8") as f:
        return list(csv.reader(f))


@pytest.fixture
def market_env(tmp_path):
    """make_market_db の機能テスト用環境を構築するフィクスチャ

    Returns:
        dict: tmp_path, market_data_dir, code_rank_dir, csv_path
    """
    market_data_dir, code_rank_dir = _setup_tmp_dirs(tmp_path)
    csv_path = str(code_rank_dir / "market_data.csv")
    shelve_path = str(market_data_dir / "market_db_shelve")

    today_html = _load_fixture("theme_rank_today.html")
    prev_html = _load_fixture("theme_rank_prev.html")

    # 当日HTMLをキャッシュとして配置
    cache_path = _write_theme_rank_cache(market_data_dir, today_html)

    # 数日前HTMLをバックアップとして配置（YYMMDDの日付付き）
    prev_date = datetime.today() - timedelta(days=3)
    backup_name = "theme_rank_%02d%02d%02d.html" % (
        prev_date.year - 2000, prev_date.month, prev_date.day
    )
    (market_data_dir / backup_name).write_text(prev_html, encoding="utf-8")

    # パッチ対象をまとめて適用
    patches = []

    # DATA_DIR をtmp_pathに差し替え
    patches.append(patch.object(make_market_db, "DATA_DIR", str(tmp_path)))
    patches.append(patch("ks_util.DATA_DIR", str(tmp_path)))

    # shelve DBをtmp_pathに差し替え
    patches.append(patch.object(db_shelve, "MARKET_SHELVE", shelve_path))
    # db_shelveのシングルトンキャッシュをリセット
    patches.append(patch.object(db_shelve, "_market_db", None))

    # make_market_db内のキャッシュをリセット
    patches.append(patch.object(make_market_db, "_market_db_cache", None))

    # http_get_html — テーマランクHTMLを返す（実際のHTTP通信をしない）
    patches.append(patch("make_market_db.http_get_html", return_value=today_html))

    # 市場指数取得をモック（HTTP通信をしない）
    index_data = _make_index_db_dict()
    patches.append(patch.object(
        make_market_db, "make_topix_db",
        return_value={"topix": index_data.copy()},
    ))
    patches.append(patch.object(
        make_market_db, "make_mothers_db",
        return_value={"mothers": index_data.copy()},
    ))
    patches.append(patch.object(
        make_market_db, "make_nikkei_db",
        return_value={"nikkei225": index_data.copy()},
    ))
    patches.append(patch.object(
        make_market_db, "make_nasdaq_db",
        return_value={"nasdaq": index_data.copy()},
    ))

    # Google Drive、決算、適宜開示をモック
    patches.append(patch("googledrive.upload_csv"))
    patches.append(patch("kessan.make_kessan_csv", return_value=[]))
    patches.append(patch("disclosure.update_disclosure_all", return_value=[]))

    # backup_fileをモック（テスト環境ではバックアップ不要）
    patches.append(patch("make_market_db.backup_file"))

    for p in patches:
        p.start()

    yield {
        "tmp_path": tmp_path,
        "market_data_dir": market_data_dir,
        "code_rank_dir": code_rank_dir,
        "csv_path": csv_path,
        "shelve_path": shelve_path,
        "today_html": today_html,
        "prev_html": prev_html,
    }

    for p in patches:
        p.stop()

    # シングルトンキャッシュのクリーンアップ
    make_market_db._market_db_cache = None
    db_shelve._market_db = None


@pytest.mark.functional
class TestUpdateMarketDbFlow:
    """update_market_db → create_market_csv の結合テスト"""

    def test_update_and_create_csv(self, market_env):
        """初回実行: テーマランク取得→DB保存→CSV生成の一連フロー"""
        # update_market_db実行
        market_db = make_market_db.update_market_db()

        # DBに必須キーが保存されていること
        assert "theme_rank" in market_db
        assert "theme_rank_diff" in market_db
        assert "access_date_theme_rank" in market_db
        assert "topix" in market_db
        assert len(market_db["theme_rank"]) == 30

        # CSV生成
        make_market_db.create_market_csv(market_db)

        # CSVが生成されていること
        csv_path = market_env["csv_path"]
        assert os.path.exists(csv_path)

        rows = _read_csv(csv_path)

        # テーマランクセクションが含まれること
        assert rows[0][0] == "■ テーマランク"

        # ランク行にテーマ名が含まれること
        rank_row = rows[1]
        assert rank_row[0] == "ランク"
        # テーマ名が含まれる（ラベル付き）
        rank_text = ",".join(rank_row)
        assert "AI" in rank_text
        assert "半導体" in rank_text

        # 市場セクションが含まれること
        market_headers = [r[0] for r in rows if r]
        assert "■市場" in market_headers

    def test_idempotent_same_day(self, market_env):
        """同日2回実行でprev_theme_rankが上書きされず差分ラベルが安定する

        テーマランク差分バグの回帰テスト:
        同日に2回実行した場合、prev_theme_rankが当日データで上書きされて
        全テーマが差分0になるバグがあった。
        """
        # 1回目実行
        market_db1 = make_market_db.update_market_db()
        diff1 = market_db1["theme_rank_diff"]

        # キャッシュリセット（2回目実行のため）
        make_market_db._market_db_cache = None
        db_shelve._market_db = None

        # 2回目実行（同日）
        market_db2 = make_market_db.update_market_db()
        diff2 = market_db2["theme_rank_diff"]

        # モメンタム順位が同じであること
        assert market_db1["theme_rank"] == market_db2["theme_rank"]

        # 差分ラベルが1回目と2回目で同じであること（冪等性）
        assert diff1 == diff2

    def test_day_change_diff(self, market_env):
        """日付変更をまたいで実行: prev_theme_rank退避→差分ラベルが正しく計算される"""
        # 1回目実行（「昨日」のデータとして）
        market_db1 = make_market_db.update_market_db()
        yesterday_rank = list(market_db1["theme_rank"])

        # キャッシュリセット
        make_market_db._market_db_cache = None
        db_shelve._market_db = None

        # DBのaccess_date_theme_rankを「昨日」に書き換えて日付変更をシミュレート
        with db_shelve.get_market_db() as db:
            db_dict = db.export_to_dict()
            db_dict["access_date_theme_rank"] = datetime.today() - timedelta(days=1)
            db.import_from_dict(db_dict)

        # キャッシュリセット
        make_market_db._market_db_cache = None
        db_shelve._market_db = None

        # テーマ順序を変えた別のHTMLで2回目実行（「今日」のデータ）
        today2_html = market_env["prev_html"]  # 順序が異なるHTMLを使用
        with patch("make_market_db.http_get_html", return_value=today2_html):
            # キャッシュファイルも更新
            cache_path = market_env["market_data_dir"] / "theme_rank.html"
            cache_path.write_text(today2_html, encoding="utf-8")
            market_db2 = make_market_db.update_market_db()

        # prev_theme_rankが退避されていること
        assert "prev_theme_rank" in market_db2
        # 退避されたprev_theme_rankは1回目のモメンタム順位と同じ
        make_market_db._market_db_cache = None
        db_shelve._market_db = None
        with db_shelve.get_market_db() as db:
            saved = db.export_to_dict()
        assert saved["prev_theme_rank"] == yesterday_rank

        # 差分ラベルが計算されていること
        diff = market_db2["theme_rank_diff"]
        # 少なくとも1つは非0の差分があるはず（テーマ順序が異なるので）
        non_zero_diffs = [v for v in diff.values() if v is not None and v != 0]
        assert len(non_zero_diffs) > 0, "日付変更後に差分ラベルが全て0になっている"

        # CSV生成して差分ラベルが反映されていること
        make_market_db.create_market_csv(market_db2)
        rows = _read_csv(market_env["csv_path"])
        rank_row = rows[1]
        rank_text = ",".join(rank_row)
        # ↑または↓のラベルが含まれるはず
        assert "↑" in rank_text or "↓" in rank_text, (
            "CSV内に差分ラベルが含まれていない: %s" % rank_text
        )
