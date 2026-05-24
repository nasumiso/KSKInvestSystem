"""migrate_theme_rank_history.py の移行ロジックテスト

実 HTML は使わず、tmp_path にダミーファイルを配置し、
parse_theme_html を monkeypatch して I/O・日付変換のフローのみ検証する。
"""

import os
from datetime import datetime, timedelta

import pytest

import migrate_theme_rank_history as mig


def _touch(path, mtime_dt, content="<dummy/>"):
    """指定 mtime でダミーファイルを作成する。"""
    with open(path, "w") as f:
        f.write(content)
    ts = mtime_dt.timestamp()
    os.utime(path, (ts, ts))


class TestMtimeToBusinessDay:
    """mtime → 営業日変換が get_price_day と整合することを確認。"""

    @pytest.mark.parametrize("hour,expected_offset", [
        (16, -1),  # 17時前 → 前日扱い
        (18, 0),   # 17時以降 → 当日
    ])
    def test_get_price_day_boundary(self, tmp_path, hour, expected_offset):
        theme_dir = tmp_path / "theme_rank"
        theme_dir.mkdir()
        base = datetime(2026, 4, 10, hour, 0, 0)
        fpath = theme_dir / "theme_rank_260410.html"
        _touch(str(fpath), base)

        result = mig.collect_theme_rank_files(str(theme_dir))
        per_day = mig.select_latest_per_business_day(result)
        assert len(per_day) == 1
        expected_date = (base + timedelta(days=expected_offset)).date().isoformat()
        assert per_day[0][0] == expected_date


class TestBuildHistory:
    """過去ファイル群から window 日数ぶんの history を末尾採用で構築する。"""

    def test_window_truncation_and_construction(self, tmp_path, monkeypatch):
        # 50日分のダミーファイルを生成 (window=40 なので末尾40日が採用される想定)
        theme_dir = tmp_path / "theme_rank"
        theme_dir.mkdir()
        base = datetime(2026, 1, 1, 18, 0, 0)  # 17時以降で当日扱い
        for i in range(50):
            day = base + timedelta(days=i)
            fname = "theme_rank_%02d%02d%02d.html" % (
                day.year - 2000, day.month, day.day,
            )
            _touch(str(theme_dir / fname), day)

        # parse_theme_html はファイル内容に関わらず固定テーマ列を返す
        monkeypatch.setattr(
            mig, "parse_theme_html",
            lambda html: ["AI", "半導体", "防衛"],
        )

        history, strength, strength_days = mig.build_history(
            str(theme_dir), window_days=40,
        )

        assert len(history) == 40
        # 末尾採用: 末尾日付は base + 49日
        last_day = (base + timedelta(days=49)).date().isoformat()
        assert history[-1][0] == last_day
        # AI は 31 - 1 = 30 pt × 40日 = 1200 pt
        assert strength["AI"] == 30 * 40
        assert strength_days["AI"] == 40

    def test_history_and_subdir_merged_and_dedup_by_day(self, tmp_path, monkeypatch):
        """theme_rank/ と history/ の両方から読み、同一営業日は mtime 新しい方を採用。"""
        theme_dir = tmp_path / "theme_rank"
        history_dir = theme_dir / "history"
        history_dir.mkdir(parents=True)

        # 同じ営業日 2026-04-10 に対し history/ 側 (古) と theme_rank/ 側 (新) を用意
        old_mtime = datetime(2026, 4, 10, 18, 0, 0)
        new_mtime = datetime(2026, 4, 10, 19, 30, 0)
        _touch(str(history_dir / "theme_rank_260410.html"), old_mtime, "<old/>")
        _touch(str(theme_dir / "theme_rank_260410.html"), new_mtime, "<new/>")

        captured = []
        def fake_parse(html):
            captured.append(html)
            return ["AI"]
        monkeypatch.setattr(mig, "parse_theme_html", fake_parse)

        history, _, _ = mig.build_history(str(theme_dir), window_days=40)

        assert len(history) == 1
        # 新しい方 (theme_rank/ 直下) が採用される
        assert "<new/>" in captured
        assert "<old/>" not in captured


class TestDryRun:
    """--dry-run 時に DB が更新されないことを確認。"""

    def test_dry_run_skips_db_write(self, tmp_path, monkeypatch):
        market_dir = tmp_path / "market_data"
        theme_dir = market_dir / "theme_rank"
        theme_dir.mkdir(parents=True)
        _touch(
            str(theme_dir / "theme_rank_260410.html"),
            datetime(2026, 4, 10, 18, 0, 0),
        )

        monkeypatch.setattr(mig, "parse_theme_html", lambda html: ["AI"])
        monkeypatch.setattr(mig, "DATA_DIR", str(tmp_path))

        # ShelveDB を呼ばれたら例外で検知
        class _Boom:
            def __enter__(self): raise AssertionError("DB should not be opened in dry-run")
            def __exit__(self, *a): pass
        monkeypatch.setattr(mig, "ShelveDB", lambda *a, **kw: _Boom())
        monkeypatch.setattr("sys.argv", ["migrate_theme_rank_history.py", "--dry-run"])

        # 例外なく完了すれば OK
        mig.main()
