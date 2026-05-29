# issue #297 実装プラン: 業態テーマを事業内容から LLM で自動提案

## ゴール

銘柄詳細画面で、業態テーマ未設定の銘柄に「🤖 提案」ボタンを出し、
事業内容テキスト (四季報特色・四季報コメント・株探概要) を軽量 LLM (`claude -p` Haiku) に渡して、
既定の業態テーママスターから 1〜2 件を提案する。提案は `<select>` にプリセットするだけで、
保存はユーザーが既存の保存ボタンで手動確定する。

検証可能なゴール:
- (A) 業態テーマ未設定 & 事業テキストありの銘柄で「🤖 提案」ボタンが表示される
- (B) ボタン押下 → API が事業テキストを LLM に渡し、マスター内テーマ 1〜2 件を JSON で返す
- (C) 返ったテーマが `<select>` に selected 反映される (保存は走らない)
- (D) 業態テーマ設定済み or 事業テキスト空の銘柄ではボタンが出ない
- (E) マスターに無いテーマを LLM が返しても無視される (ハルシネーション防止)

## スコープ外 (やらないこと)

- 提案の自動保存 (手動確定のみ)
- ポートフォリオ一覧画面への提案ボタン追加 (詳細画面のみ)
- テーママスター自体の自動生成・追加
- Anthropic SDK / API キー方式 (CLI 経由のみ)

## 運用前提 (設計判断の土台)

本 WebApp は `http://localhost:5001` で動く**単一ユーザーの調査ツール**。
起動は `python -m webapp.app` = **Flask 開発サーバの単一プロセス運用** (`app.run`、
gunicorn/uWSGI のマルチワーカーは使っていない。webapp/app.py 確認済み)。
`debug=True` 既定で threaded=True のため同一プロセス内のスレッド並行はあり得るが、
プロセスは 1 つなので `threading.Lock` が同時実行ガードとして機能する (codex 指摘1 への回答)。

公開サービスではないため、認証・マルチユーザー・本格的な DoS 対策・ジョブキュー化・
プロセス間 file lock は overcomplicated と判断 (CLAUDE.md: Simplicity First)。
ただし「誤操作・多重押下・直接 API 叩き」による LLM コスト暴発とワーカースレッド枯渇は
軽量ガードで防ぐ (下記 §2 のサーバー側検証 + プロセス内 同時実行 1 件制限)。
※ 将来マルチプロセス運用 (gunicorn 等) に切り替える場合は file lock 等への置換が必要、
  と §2 のコメントに残す。

## 前提となる既存実装 (確認済み)

- 詳細画面のテーマ select: `detail.html:57-80` (`portfolio_status_query and not portfolio_fallback_mode` の時のみ表示)
- テーママスター: `ps.list_themes()` → `[{"name", "description", "created_at"}, ...]` (`portfolio_shelve.py:940`)
- 現状テーマ: `detail.py:89` の `gyoutai_themes` (list[str])
- 事業テキスト 3 ソース:
  - research overview: `get_research_record(code_s)["overview"]` (四季報特色)
  - shikiho_comments: `get_research_record(code_s)["shikiho_comments"]` → `[{"period","comment"}, ...]`
  - stocks overview: `get_stock_data(code_s)["overview"]` (株探 1 行)
- LLM 起動の前例: `run_theme_news.py:67-90` (`subprocess.run(["claude", "-p", ...], timeout=, capture_output=True)`)
- detail route の context 注入: `detail.py:108-124`

## 実装

### 1. LLM 呼び出しヘルパー (新規モジュール)

`scripts/theme_suggest.py` を新規作成。

```
def build_business_text(research_overview, shikiho_comments, stocks_overview) -> str
    # 3 ソースを結合して 1 つの事業説明テキストにする。
    # 各ソースに見出しを付けて結合。全ソース空なら "" を返す。

def suggest_gyoutai_themes(business_text, theme_names, *, max_suggest=2, timeout_sec=45) -> list[str]
    # claude -p を subprocess 起動し、theme_names の中から business_text に最も合う
    # テーマを最大 max_suggest 件、JSON 配列で返させる。
    # - business_text が空なら [] を即返す (LLM 起動しない)
    # - theme_names が空なら [] を即返す
    # - プロンプトで「以下のリストからのみ選べ。リスト外は出力するな」と制約
    # - --output-format json で claude -p を起動、result フィールドから JSON 配列を抽出
    # - 返り値は theme_names に含まれるものだけにフィルタ (ハルシネーション防止 = ゴールE)
    # - 重複除去・max_suggest 件で打ち切り
    # - タイムアウト/異常終了/パース失敗時は [] を返す (UI 側でエラー表示)
```

claude -p の呼び出し方 (`run_theme_news.py` 踏襲):
```
cmd = ["claude", "-p", <prompt>, "--model", "haiku", "--output-format", "json"]
result = subprocess.run(cmd, cwd=PROJECT_ROOT, timeout=timeout_sec,
                        check=False, capture_output=True, text=True)
```
- プロンプトはテーマ一覧と事業テキストを埋め込んだ単一文字列。ツール不要なので `--allowed-tools` は付けない (Web 検索等させない)。
- `--output-format json` の stdout 末尾 JSON から `result` を取り出し、その中の JSON 配列をパース。
  - LLM 出力が ```json フェンス付きの可能性に備え、配列部分 `[...]` を正規表現で抽出してから json.loads。

ログ: `log_print`/`log_warning`/`log_error` を使用 (print 不可)。
個別銘柄の中間値・プロンプト全文は `log_debug`。

### 2. API エンドポイント (memo.py に追加)

`POST /stock/<code_s>/suggest_themes` を `memo_bp` に追加 (AJAX, JSON 応答)。

```
@memo_bp.route("/stock/<code_s>/suggest_themes", methods=["POST"])
def post_suggest_themes(code_s):
    # 0. 同時実行ガード: モジュールレベルの threading.Lock を acquire(blocking=False)。
    #    既に実行中なら 429 {"ok": False, "error": "他の提案処理を実行中です"} を返す
    #    (try/finally で必ず release)。単一ユーザー前提なので「同時 1 件」で十分。
    # 1. record = get_research_record(code_s); stock = get_stock_data(code_s)
    #    どちらも無ければ 404 {"ok": False, "error": "銘柄が見つかりません"}
    # 2. サーバー側ガード (codex 指摘1): ボタン表示条件を API でも再検証する。
    #    - 業態テーマが既に設定済み (gyoutai_themes に非空値あり) → 409
    #      {"ok": False, "error": "業態テーマが既に設定済みです"}
    #      (portfolio_record.memo.gyoutai_themes を detail.py:86-89 と同じ方法で取得)
    # 3. business_text = build_business_text(record.overview, record.shikiho_comments,
    #                                        stock.overview)
    #    business_text 空 → 200 {"ok": True, "themes": [], "reason": "no_business_text"}
    # 4. theme_names = [t["name"] for t in ps.list_themes()]
    #    theme_names 空 → 200 {"ok": True, "themes": [], "reason": "no_master"}
    # 5. themes = suggest_gyoutai_themes(business_text, theme_names)
    # 6. 200 {"ok": True, "themes": themes}
    # 例外時 → 500 {"ok": False, "error": str(e)}
```
- サーバー側ガード (codex 指摘1 対応): クライアントの表示条件に依存せず、API 側で
  「業態テーマ未設定」「事業テキストあり」を再検証する。直接 API を叩いても
  設定済み銘柄では LLM を起動しない (コスト暴発の入口を塞ぐ)。
- 同時実行 1 件制限 (codex 指摘2 対応): `threading.Lock` で多重押下・並行リクエストを弾く。
  単一プロセス (app.run) 運用なのでプロセス内 Lock で同時実行をプロセス全体に対して
  1 件に制限できる。単一ユーザーなのでジョブキュー化は不要と判断。
  ※ コメントに「マルチプロセス運用へ移行する場合は file lock 等に置換が必要」と明記する。
- タイムアウト (codex 指摘2 対応): `suggest_gyoutai_themes` の `timeout_sec` は 60→**45** に短縮。
  ワーカーが過度に長くブロックされないようにする (Haiku の分類は通常数秒で返る)。
- 既存 memo.py の import に `get_research_record`, `get_stock_data` (helpers 経由), `portfolio_shelve`,
  theme_suggest を追加。helpers 経由で取れるものは helpers から import (既存スタイルに合わせる)。

### 3. detail route の context (detail.py)

ボタン表示条件をテンプレートに渡すため、context に下記を追加:
```
gyoutai_themes_unset = (not any(gyoutai_themes))   # 全スロット未設定か
has_business_text = bool(build_business_text(...))  # 事業テキストが 1 つでもあるか
```
- `build_business_text` は theme_suggest から import。
- research overview / shikiho_comments は `record` (= get_research_detail の戻り) から、
  stocks overview は `stock` から取得。
- ボタン表示判定はテンプレート側で `gyoutai_themes_unset and has_business_text` を見る。

### 4. テンプレート (detail.html)

`detail.html:76-78` の ✏️ リンクの隣 (テーマ select 群の後) に提案ボタンを追加:
```
{% if gyoutai_themes_unset and has_business_text %}
<button type="button" class="gyoutai-suggest-btn" data-code="{{ record.code_s }}"
        title="事業内容から業態テーマを LLM 提案">🤖 提案</button>
{% endif %}
```
JS (既存の inline script ブロックに追記、または detail.html 末尾の script 内):
```
- ボタン click → fetch POST /stock/<code>/suggest_themes (X-Requested-With ヘッダ)
- ボタンを「提案中...」に変えて disable (二重実行防止)
- 応答 themes[] を gyoutai-input-detail の select に順に selected 設定
  (既存値が無い前提だが、空きスロットに前から詰める)
- themes 空なら「候補なし」を一時表示
- 失敗時はアラート or ボタン脇に「提案失敗」表示
- 保存はしない (ユーザーが既存の保存フローで確定)
```
- select への反映方法: `gyoutai-input-detail` クラスの select を順に取得し、
  themes[i] を value に持つ option を selected にする。
  value が option に無い場合 (マスター未登録) はスキップ — ただし API 側でフィルタ済みなので基本発生しない。

## 検証

CLAUDE.md / testing.md のマッピングに従う。

- 新規 `theme_suggest.py` のユニットテスト (`tests/test_theme_suggest.py`, 5 本以下 parametrize):
  - `build_business_text`: 全空→""、一部あり→結合される (parametrize で数ケース)
  - `suggest_gyoutai_themes`: business_text 空→[]、theme_names 空→[]、
    LLM 返り値のフィルタ (マスター外除去・重複除去・max 件打ち切り) を subprocess mock で検証
  - subprocess は monkeypatch でモック (実際の claude -p は呼ばない)
- WebApp ルート: `pytest tests/test_webapp_routes.py -v` (memo.py 変更のため)
  - suggest_themes エンドポイントの 200/空応答パスを 1〜2 ケース追加 (theme_suggest をモック)
- テンプレート変更はブラウザ目視 (testing.md: HTML/JS のみはブラウザ確認):
  - 未設定 & 事業テキストあり銘柄でボタン表示 → 押下 → select に反映を確認
  - 設定済み銘柄 / 事業テキスト空銘柄でボタン非表示を確認
  - スクリーンショットは `.playwright-mcp/` 配下に保存

## 影響範囲・互換性

- 新規ファイル 1 (theme_suggest.py) + テスト 1
- 既存変更: memo.py (route 追加), detail.py (context 2 つ追加), detail.html (ボタン + JS)
- 既存の保存フロー・テーママスター・DB スキーマは一切変更しない (読み取りのみ)
- claude -p は read-only 用途 (ツール無し)。DB 書き込みなし。
- LLM 失敗は [] フォールバックで UI が壊れない (ゴール E と整合)

## コスト注記 (疎通確認で判明)

- `claude -p` は CLAUDE.md 等のシステムコンテキストを毎回ロードするため、
  1 回あたり cacheCreation 込みで概算 ~$0.06 (実測: input 3 / output 13 tokens でも
  cache_creation 51,612 tokens)。純粋な API 呼び出しより割高。
- 単一ユーザーが「迷ったときだけ」押す用途なので許容範囲。多重押下は §2 の Lock で抑止済み。
- モデルは `--model haiku` で有効 (実体 `claude-haiku-4-5-20251001`) を疎通確認済み。

## 解決済みの確認事項

- `claude -p --model haiku` のモデル指定は現環境で**有効** (疎通確認済み、実体 claude-haiku-4-5-20251001)。
