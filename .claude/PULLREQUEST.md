# 改善提案（統合版）

このドキュメントは、株式市場分析システムの改善提案を優先度順にまとめたものです。

**評価基準:** 工数対効果、リスク軽減度、実装難易度を総合的に判断

---

## 最優先（すぐに着手推奨） ✅

### 1. **データ永続化の段階的改善**

**現状の問題:**
- Pickleファイル (`stocks.pickle`) による単一ファイルDB
  - 1件更新でも全銘柄（数千件）を読み書き必要
  - データ破損リスク、並行アクセス制御なし
  - 差分更新・履歴管理が困難

**段階的移行アプローチ:**
1. **ステップ1（即時）: pickle → shelve**
   - Python標準ライブラリ（追加依存なし）
   - 部分的読み書き可能、移行コスト低
   - 工数: 1日

2. **ステップ2（短期）: shelve → SQLite**
   - クエリによる高速検索・フィルタリング
   - トランザクション、障害復旧機能
   - 実装方法: `scripts/db.py`（DB抽象化層）を作成 → `scripts/migrate_pickle_to_sqlite.py`（dry-run対応）
   - 切替: write-through（両方へ書く）→ 段階的に読み元をSQLiteへ → pickleアーカイブ
   - 工数: 1週間

3. **ステップ3（必要時）: SQLite → PostgreSQL**
   - マルチユーザー対応、大規模データ向け

**参照:** `scripts/make_stock_db.py`（読み書きの一元化ポイント）

---

### 2. **設定管理の外部化**

**現状の問題:**
- 定数が各モジュールにハードコード（`INTERVAL_DAY = 7`, `MAX_REQUESTS = 3`, スコア比率・閾値等）
- データディレクトリパスが固定
- 運用変更のたびにコード修正が必要

**改善案:**
```
config/
├── default.yaml      # デフォルト設定
├── production.yaml   # 本番環境設定
└── development.yaml  # 開発環境設定
```
- 環境変数による設定切り替え
- Pydantic等による設定値のバリデーション
- 工数: 1日

**参照:** `scripts/shintakane.py`, `scripts/make_stock_db.py`, `scripts/ks_util.py`

---

### 3. **エラーハンドリングとリトライ戦略の強化**

**現状の問題:**
- スクレイピング失敗時の処理が統一されていない
- HTMLフォーマット変更への対応が各パーサー内にハードコード
- セマフォによるレート制限はあるが、429エラー等への動的対応がない

**改善案:**
- `tenacity`等でリトライ/バックオフ導入
- スクレイパー基底クラスでフォールバック機能実装
- HTMLパーサーの戦略パターン（複数バージョンのパーサーを試行）
- Circuit Breakerパターンでサービス障害時の連鎖防止
- `use_requests_session()` を全体で統一
- 工数: 2日

**参照:** `scripts/price.py`, `scripts/master.py`, `scripts/gyoseki.py`, `scripts/shihyou.py`

---

### 4. **CI/CDパイプラインの導入**

**現状の問題:**
- 自動化テストが無く、変更の安全性が担保されていない
- コードフォーマット・リントの統一なし

**改善案:**
- GitHub Actions による CI 追加
  - `black`/`isort`（コードフォーマット）
  - `flake8`（リント）
  - `pytest`（smoke tests + fixtures）
  - `mypy`（型チェック、段階導入）
- PRテンプレートに「DBスキーマ変更の注意」を追加
- 工数: 1日

**実装例:** `.github/workflows/ci.yml`

---

## 中優先度（価値大、中期実施） ⚠️

### 5. **キャッシュ戦略の一元化**

**現状の問題:**
- UPD_*定数による4段階制御は存在するが、実装が各モジュールに分散
- キャッシュ有効期限ロジックが関数ごとにハードコード
- ファイルキャッシュとメモリキャッシュの整合性管理が不明確

**改善案:**
- キャッシュマネージャーの導入（TTL、バージョン付き、無効化ポリシーを一元管理）
- デコレーターパターンでキャッシュロジックを分離
- 工数: 3日

**参照:** `scripts/ks_util.py`, `scripts/price.py`

---

### 6. **データモデルの明確化**

**現状の問題:**
- `stocks` dict に多種のキーが混在、更新箇所が分散
- 型ヒントは部分的（コード全体に一貫性なし）
- スキーマ管理が不明確（辞書構造の暗黙的な契約）
- スクレイピングしたデータの妥当性チェックが不十分

**改善案:**
- dataclass/Pydantic で `Stock`, `PriceSeries`, `Earnings`, `Indicators` の構造を定義
- 型制約・バリデーション導入
```python
from pydantic import BaseModel, validator

class StockPrice(BaseModel):
    code_s: str
    price: int
    volume: float

    @validator('price')
    def price_must_be_positive(cls, v):
        if v <= 0:
            raise ValueError('price must be positive')
        return v
```
- 工数: 5日

**参照:** `scripts/make_stock_db.py`

---

### 7. **ソース抽象化とパーサの整理**

**現状の問題:**
- Yahoo/KabutanのHTML解析が各モジュールに散在
- フォーマット変更の影響が広範囲に及ぶ
- 回帰テストが不足（`data/cache_data` の HTML を fixtures にしていない）

**改善案:**
- 取得・パースを「ソースアダプタ」に集約
- バージョニング/フォールバック機能
- 各スクレイパ/パーサをクラス化して入出力を明確化（型での明示）
- `data/cache_data/` の HTML を fixtures 化して回帰テスト追加
- 工数: 1週間

**参照:** `scripts/price.py`, `scripts/master.py`, `scripts/gyoseki.py`, `scripts/shihyou.py`

---

### 8. **依存関係の整理と ks_util.py の分割**

**現状の問題:**
- `make_stock_db.py`が`price.py`, `gyoseki.py`, `shihyou.py`等を直接インポート
- 各モジュールが`ks_util.py`の多数の関数に依存
- `ks_util.py` がゴッドオブジェクト化（HTTP、ログ、ファイルIO、日付処理等）
- モジュール間の責任範囲が不明確

**改善案:**
- 依存性注入（DI）パターンの導入
- インターフェース/抽象基底クラスによる疎結合化
- `ks_util.py`を機能別モジュールに分割
  - `http_client.py`
  - `cache_manager.py`
  - `logger.py`
  - `date_utils.py`
  - `file_utils.py`
- 工数: 3日

---

### 9. **認証とSecrets管理の改善**

**現状の問題:**
- Google Drive 認証に古いライブラリ（`oauth2client` 等）を使用
- Secrets 管理が不明瞭

**改善案:**
- `google-auth` に移行
- Credentials は GitHub Secrets / 環境変数で管理
- `scripts/googledrive.py` をテスト可能に
- 工数: 2日

**参照:** `scripts/googledrive.py`

---

### 10. **テスタビリティの向上**

**現状の問題:**
- HTTPリクエストが実際の外部サービスに依存（モック化が困難）
- ユニットテストが`test_functional.py`のみで統合テストが主体
- 副作用を持つ関数（DBへの直接書き込み）が多い

**改善案:**
- リポジトリパターンでデータアクセスを抽象化
- HTTPクライアントをインターフェース化（`requests_mock`や`vcr.py`の活用）
- 純粋関数と副作用の分離（関数型プログラミングの原則）
- 工数: 1週間

---

## 低優先度（長期的改善） 💡

### 11. **パイプライン分割と再実行性**

**現状の問題:**
- `shintakane.py` が「収集→更新→分析→出力」を一気通貫で行う
- 再実行単位が粗く、失敗時の部分再実行が困難

**改善案:**
- ステージ分割（収集/更新/分析/出力）
- Idempotentな再実行設計
- 工数: 1週間

**参照:** `scripts/shintakane.py`

---

### 12. **レイヤー分離の導入**

**現状の問題:**
- データ取得（スクレイピング）、ビジネスロジック（分析・ランキング）、データ永続化が各モジュール内で混在
- 例: `price.py`がYahoo/Kabutanからのスクレイピングと技術指標計算の両方を担当

**改善案:**
```
layers/
├── data_sources/      # スクレイピング専用（Yahoo, Kabutan, etc）
├── repositories/      # データアクセス層（DB操作の抽象化）
├── domain/           # ビジネスロジック（指標計算、ランキング）
├── services/         # ユースケース層（分析フロー制御）
└── presentation/     # CSV出力、レポート生成
```
- 工数: 2週間

---

### 13. **並列更新の安全性向上**

**現状の問題:**
- `update_db_rows_async` が並列更新を行うが、共有データの更新責務が曖昧
- スレッド間のデータ競合リスク

**改善案:**
- 取得と書き込みを分離
- キュー＋集約で一貫性を担保
- 工数: 3日

**参照:** `scripts/make_stock_db.py`

---

### 14. **観測性の強化**

**現状の問題:**
- ログ中心の運用
- 異常検知や再現性が低い

**改善案:**
- 出力CSVやDB更新にメタ情報を添付（取得日時/ソース/件数/失敗数）
- 構造化ログ導入
- メトリクス収集
- 工数: 5日

**参照:** `scripts/ks_util.py`（ログシステム）、出力系

---

### 15. **非同期処理の改善**

**現状の問題:**
- `ThreadPoolExecutor`による並列化は実装済みだが、スレッド数（5）が固定
- セマフォ制限（3）とスレッド数（5）の関係が非自明
- I/O待機が多いスクレイピングでasyncioが未活用

**改善案:**
- `asyncio` + `aiohttp`への移行検討
- 動的なワーカー数調整
- 工数: 1週間

---

### 16. **スケジューリングとワークフロー管理**

**現状の問題:**
- `shintakane_cron.sh`による単純なcron実行
- 依存関係のあるタスク（市場データ更新→銘柄分析）の順序制御がシェルスクリプト頼み
- 失敗時のリトライや通知機能なし

**改善案:**
- Airflow、Luigi、Prefect等のワークフローエンジン導入
- タスク依存関係の明示的な定義
- 実行履歴、ログ、アラートの統合管理
- 工数: 1週間

---

### 17. **実行環境の再現性向上**

**現状の問題:**
- 環境構築手順が不明瞭
- 依存関係のバージョン管理が不十分

**改善案:**
- `Dockerfile` 作成
- `pyproject.toml` による依存管理
- README に環境構築手順を明記
- 工数: 2日

---

### 18. **開発体験の向上**

**改善案:**
- `pre-commit` hooks 導入（自動フォーマット、リント）
- 型ヒントの全面導入
- `mypy` の strict モード対応
- 工数: 5日

---

## 実装ロードマップ（推奨）

### フェーズ1（最初の1週間）
1. pickle → shelve（1日）
2. 設定の外部化（1日）
3. エラーハンドリング強化（2日）
4. CI/CD導入（1日）

**期待効果:** 評価 5.25 → 6.5 / 10

### フェーズ2（2-4週目）
5. shelve → SQLite（1週間）
6. キャッシュ戦略一元化（3日）
7. データモデル明確化（5日）

**期待効果:** 評価 6.5 → 7.5 / 10

### フェーズ3（2-3ヶ月）
8. ソース抽象化（1週間）
9. ks_util.py分割（3日）
10. テスタビリティ向上（1週間）
11. 認証/Secrets改善（2日）

**期待効果:** 評価 7.5 → 8.0 / 10

### フェーズ4（長期）
残りの低優先度項目を必要に応じて実施

---

## 具体的な着手タスク例 🎯

1. `scripts/db.py` スケルトン + `scripts/migrate_pickle_to_sqlite.py`（dry-run）を作成する PR
2. `.github/workflows/ci.yml`（lint + smoke tests）を追加する PR
3. `config/default.yaml` + 設定読み込みモジュールを作成する PR
4. `scripts/googledrive.py` を `google-auth` に移行する PR（Secrets設定ドキュメント添付）

---

**注意事項:**
- 各改善は小さなPoCでリスクを確認してから本格実施してください
- 既存機能を壊さないよう、段階的な移行を心がけてください
- テストカバレッジを上げながら進めてください
