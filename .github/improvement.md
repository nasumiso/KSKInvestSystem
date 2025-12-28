# .github/inprovement.md

**目的:** リポジトリの運用・保守性・可観測性を向上させ、安全に機能追加やリファクタリングできる状態にする。

## 要約（現状の課題） ⚠️
- 単一の `data/stock_data/stocks.pickle` にDBを保存 → 同時書き込み・破損リスクがある。  
- CI / 自動化テストが無く、変更の安全性が担保されていない。  
- Google Drive 認証に古いライブラリ（`oauth2client` 等）を使用。Secrets 管理が不明瞭。  
- パーサの回帰テストが不足（`data/cache_data` の HTML を fixtures にしていない）。

## 優先度：高（すぐ着手推奨） ✅
1. **Pickle → SQLite（段階的移行）**  
   - 追加: `scripts/db.py`（DB抽象化レイヤ）を作成。  
   - マイグレーション: `scripts/migrate_pickle_to_sqlite.py`（`--dry-run` 推奨）。  
   - 切替方法: write-through（暫定的に両方へ書く）→ 段階的に読み元をSQLiteへ変更→ ピクルをアーカイブ。  
   - 参照: `scripts/make_stock_db.py`（読み/書きの一元化ポイント）

2. **CI の追加（GitHub Actions）**  
   - 最低セット: `black`/`isort`, `flake8`, `pytest`（smoke tests/fixtures）, `mypy`（段階導入）  
   - PRテンプレートに「DBスキーマ変更の注意」を追加

3. **HTTP/スクレイピング処理の堅牢化**  
   - `tenacity` 等でリトライ/バックオフを導入、`use_requests_session()` を全体で統一、キャッシュ導入検討

4. **認証とSecrets管理の改善**  
   - `google-auth` に移行、Credentials は GitHub Secrets / 環境変数で管理、`scripts/googledrive.py` をテスト可能に

## 優先度：中（価値大、作業中〜中期） ⚠️
- 各スクレイパ／パーサをクラス化して入出力を明確化（型での明示） → テスト容易化  
- `data/cache_data/` の HTML を fixtures 化して回帰テスト追加（`pytest`）  
- 実行環境を再現（`Dockerfile` or `pyproject.toml` + README の手順）

## 優先度：低（長期） 💡
- 型導入（`mypy`）、構造化ログ / メトリクス、`pre-commit` hooks、スケジューラの将来検討（Airflow 等）

## 具体的な短期タスク（着手例） 🎯
1. `scripts/db.py` スケルトン + `scripts/migrate_pickle_to_sqlite.py`（dry-run）を作成する PR  
2. `.github/workflows/ci.yml`（lint + smoke tests）を追加する PR  
3. `scripts/googledrive.py` を `google-auth` に移行する PR（Secrets 設定ドキュメント添付）

> 注: ここに書いた改善案は現行コード（例: `scripts/make_stock_db.py`, `scripts/price.py`, `scripts/googledrive.py`, `data/cache_data/`）の実装パターンに基づく具体提案です。実施前に小さな PoC でリスクを確認してください。