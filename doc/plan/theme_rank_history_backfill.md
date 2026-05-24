# テーマランク履歴 遡及再構築プラン (A+C)

## 背景

`scripts/make_market_db.py:24` で `THEME_STRENGTH_WINDOW_DAYS = 40` 営業日と定義されているが、現状の `market_db.theme_rank_history` は2日分しか蓄積されておらず、テーマカードの持続強度バーが実質的に「直近2日の合算」しか反映していない。

一方ファイルシステム上には:

- `data/market_data/theme_rank/theme_rank_YYMMDD.html` … 直近28営業日 (mtime ≤ 30日)
- `data/market_data/theme_rank/history/theme_rank_YYMMDD.html` … 30日超の過去アーカイブ (2023年以降ぶん全て)

が残っており、既存パーサーで遡及計算が可能。

加えて `_archive_old_theme_rank(days=30)` が30日超で `history/` に追い出してしまうため、ウィンドウ40日と整合していない（バグ）。

## ゴール (A+C)

1. **A**: 40営業日ぶんの `theme_rank_*.html` (`theme_rank/` + `history/` から不足分を補う) を読み込み、`market_db.theme_rank_history / theme_strength / theme_strength_days` を遡及再構築する
2. **C**: `_archive_old_theme_rank` のデフォルト `days` を `THEME_STRENGTH_WINDOW_DAYS` と揃え、今後 history/ に追い出される前に40営業日ぶんは `theme_rank/` 直下に残る運用に変更する

## 実装内容

### 1. `scripts/make_market_db.py` の修正 (最小)

`_archive_old_theme_rank` のデフォルト値を `THEME_STRENGTH_WINDOW_DAYS` と揃える:

```python
def _archive_old_theme_rank(theme_rank_dir, days=THEME_STRENGTH_WINDOW_DAYS):
    """{days}日以前のtheme_rank_YYMMDD.htmlをhistory/に移動"""
```

呼び出し側 (`get_theme_rank_list` 内) はデフォルト引数を使うので変更不要。

### 2. 新規ワンショット移行スクリプト `scripts/migrate_theme_rank_history.py`

責務:

- `theme_rank/` 直下と `theme_rank/history/` から `theme_rank_YYMMDD.html` を全て列挙
- 各ファイルの **mtime を `get_price_day()` に通して** 営業日日付 (YYYY-MM-DD) を導出
  - 本番 `_theme_rank_history_date()` (`make_market_db.py:180`) と同じロジックで揃え、17:00前スクレイピング分が前日扱いになる仕様と整合させる
  - ファイル名の YYMMDD は `backup_file` がスクレイピング時刻 mtime から付与するため、本番ロジックと不一致になる可能性があり採用しない
- 同一営業日に複数ファイルが該当した場合は mtime が新しい方を採用 (本番ロジックでも同日2回目の実行は上書きされる)
- 日付昇順にソートし、末尾 `THEME_STRENGTH_WINDOW_DAYS` 営業日ぶんを採用
- 各ファイルを `parse_theme_html()` で読み、既存の `update_theme_rank_history()` を逐次呼んで history を構築
- `market_db.theme_rank_history / theme_strength / theme_strength_days` を **常に上書き** 保存
  - 「既存が揃っているか」での no-op 分岐は設けない (日付ずれ・重複・欠損が混ざった壊れた状態を固定化するリスクのほうが大きい)
  - 冪等性は「同じ入力ファイル群に対して常に同じ結果を上書きする」ことで担保
- ドライランオプション `--dry-run` で構築予定の件数・日付範囲・上位10テーマだけ表示し、DB は更新しない

CLI:

```bash
cd scripts && python migrate_theme_rank_history.py --dry-run
cd scripts && python migrate_theme_rank_history.py
```

### 3. テスト

`tests/test_migrate_theme_rank_history.py` を新規作成。3本に抑える (parametrize で集約):

- ファイル mtime → 営業日変換が `get_price_day` と同じ結果になる (17時前後の境界2ケース)
- 過去ファイル群から `THEME_STRENGTH_WINDOW_DAYS` 日ぶんの history が末尾採用で構築される
- ドライランで DB が変更されない

実 HTML は使わず一時ディレクトリにダミーファイルを作り、`parse_theme_html` を monkeypatch する。
`update_theme_rank_history` 単体ロジックは既存テストで担保済みのため、ここでは移行スクリプトの I/O・日付変換フローのみ検証。

## 検証手順

1. `pytest tests/test_make_market_db.py tests/test_migrate_theme_rank_history.py -v`
2. `cd scripts && python migrate_theme_rank_history.py --dry-run` で件数を目視確認
3. 本実行後、`/market` を再生成し持続強度バーが上位テーマで days=40 相当になることを確認

## 影響範囲

- 既存パーサー (`parse_theme_html`)・強度計算 (`update_theme_rank_history`) は無変更
- DB スキーマ変更なし (`theme_rank_history` の値が増えるだけ)
- `_archive_old_theme_rank` のデフォルト引数のみ変更 (40日まで `theme_rank/` 直下に残る)
- 本番運用への影響: 翌日 `make_theme_data` 実行時に新しい強度値で `market_data.html` が再生成される
