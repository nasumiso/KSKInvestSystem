設計・アーキテクチャの改善点（影響度順）

  - 永続化の再設計: pickle に依存した単一ファイルDBは壊れやすく差分更新が難しい。SQLite などの
    RDB（あるいはParquet＋メタDB）に移行して、履歴・差分更新・障害復旧を可能にする（scripts/
    make_stock_db.py）。
  - パイプライン分割と再実行性: shintakane.py が「収集→更新→分析→出力」を一気通貫で行っており再実
    行単位が粗い。ステージ分割とIDempotentな再実行を導入すると、失敗や部分更新に強くなる（scripts/
    shintakane.py）。
  - ソース抽象化: Yahoo/KabutanのHTML解析が各モジュールに散在し、フォーマット変更の影響が広い。取
    得・パースを「ソースアダプタ」に集約し、バージョニング/フォールバックを持たせる（scripts/
    price.py, scripts/master.py, scripts/gyoseki.py, scripts/shihyou.py）。
  - データモデルの明確化: stocks dictに多種のキーが混在し、更新箇所が分散している。dataclass 等で
    Stock, PriceSeries, Earnings, Indicators の構造を定義し、検証・型制約を入れる（scripts/
    make_stock_db.py）。
  - キャッシュ・更新ポリシーの統一: UPD_* とファイルキャッシュの判定が各所に分散。TTL/バージョン付
    きキャッシュと取得戦略を一元化し、重複ロジックを減らす（scripts/ks_util.py, scripts/
    price.py）。
  - 並列更新の安全性: update_db_rows_async が並列更新を行うが、共有データの更新責務が曖昧。取得と
    書き込みを分離し、キュー＋集約で一貫性を担保する（scripts/make_stock_db.py）。
  - 観測性の強化: 現状ログ中心。出力CSVやDB更新にメタ情報（取得日時/ソース/件数/失敗数）を添付し、
    再現性と異常検知を上げる（scripts/ks_util.py, 出力系）。
  - 設定の外部化: スコア比率・閾値がコードに散在。config.yaml化して運用変更を安全に（scripts/
    shintakane.py, scripts/make_stock_db.py）。
