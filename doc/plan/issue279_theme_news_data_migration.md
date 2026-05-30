# プラン: theme-news 生成データを $KS_DATA_DIR 配下に移行 (issue #279)

## 目的

theme-news の **生成データ** (`history/*.md`, `events.json`, `calendar.html`, `*.meta.json`, `*.running`, `*.done`) を、コードと混在している `.claude/skills/theme-news/` から **`$KS_DATA_DIR/theme_news/`** に移行する。

`market_data.html` が `$KS_DATA_DIR/code_rank_data/` にある設計思想 (doc/ARCHITECTURE.md「データパス解決」) に揃え、データとコードの責任を分離する。

### 解決する課題

- 生成データが Git 追跡されるリスク (現状は手動 add 依存で事故の元)
- worktree クリーンや誤った `rm` での履歴消失リスク
- 履歴データはバックアップしたいが Git に入れたくない → データホルダー (`$KS_DATA_DIR` = Google Drive 同期) に置く

## 方針 (確定事項)

- **移行先ディレクトリ名**: `$KS_DATA_DIR/theme_news/` (snake_case、`code_rank_data/` と統一)
- **本 PR のスコープ**: コード書き換え + 既存データの新パスへのコピー まで。**旧データの削除はしない** (issue 推奨の「並行稼働 1-2 週間 → 旧削除」の前半。旧削除は別 PR/手動)
- **symlink は採用しない** (issue 指定): AI が `.claude/skills/theme-news/history` を見て「ここがデータ保管場所」と誤学習するのを防ぐ。データ実体の場所が SKILL.md / コードを読んで分かる方が長期保守上望ましい

## 移行先ディレクトリ構成

```
$KS_DATA_DIR/theme_news/
├── events.json                 (生成データ: 株カレンダー素材)
├── calendar.html               (生成データ: render_calendar.py 出力)
├── calendar-archive.md         (生成データ: 30日経過エントリの append-only アーカイブ)
└── history/
    ├── YYYY-MM-DD.md           (日次要約)
    ├── YYYY-MM-DD.md.done      (完了マーカー)
    ├── YYYY-MM-DD.md.running   (実行中マーカー)
    └── YYYY-MM-DD.md.meta.json (usage メタ)
```

`.claude/skills/theme-news/` に残すのは **コードのみ**: `SKILL.md`, `render_calendar.py`, `calendar_template.html`。

**生成データ vs コードの確定** (`git ls-files .claude/skills/theme-news/` で実機確認済み、追跡中ファイル):
- 生成データ (移行・追跡解除対象): `events.json`, `calendar.html`, `calendar-archive.md`, `history/*` (+ 非追跡の `*.running`/`*.done`/`*.meta.json`)
- コード (据え置き・追跡維持): `SKILL.md`, `render_calendar.py`, `calendar_template.html`
- 注: `calendar-archive` の実ファイルは **`.md`** (SKILL.md L318 は `calendar-archive.html` と誤記している → 後述の通り `.md` + 新パスに修正する)

---

## データパス解決の共通化

すべて `ks_util.DATA_DIR` (`_resolve_data_dir`、`$KS_DATA_DIR` 優先 → git common-dir → `ROOT/data`) を起点にする。`make_market_db.py` の `os.path.join(DATA_DIR, "code_rank_data", ...)` と同じパターン。

新たに `theme_news` のサブパスを返す小さな定数/ヘルパーを 1 箇所に定義し、3 つの利用元 (run_theme_news.py / market.py / render_calendar.py) がそれを参照する形にして、パス定義の三重化を避ける。

### パス定義の置き場所

`ks_util.py` に以下を追加 (DATA_DIR と同じモジュールに置き、全利用元から import 可能にする):

```python
# theme-news 生成データのルート ($KS_DATA_DIR/theme_news/)
THEME_NEWS_DIR = os.path.join(DATA_DIR, "theme_news")
THEME_NEWS_HISTORY_DIR = os.path.join(THEME_NEWS_DIR, "history")
THEME_NEWS_EVENTS_JSON = os.path.join(THEME_NEWS_DIR, "events.json")
THEME_NEWS_CALENDAR_HTML = os.path.join(THEME_NEWS_DIR, "calendar.html")
```

- `scripts/` 配下 (run_theme_news.py, market.py) は `from ks_util import THEME_NEWS_HISTORY_DIR, ...` で直接 import
- skill 配下の `render_calendar.py` は既存の sys.path 追加パターン (resolve_today 内で scripts を sys.path に入れて ks_util を import している、L32-35) を関数外に一般化して DATA_DIR/定数を取得

---

## コード変更詳細

### 1. `scripts/ks_util.py`

`DATA_DIR` 定義の直後に上記 `THEME_NEWS_*` 定数を追加。

### 2. `scripts/run_theme_news.py` (L21-22)

```python
# 変更前
PROJECT_ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = PROJECT_ROOT / ".claude" / "skills" / "theme-news" / "history"
# 変更後
from ks_util import THEME_NEWS_HISTORY_DIR  # (既存 import 行にまとめる)
HISTORY_DIR = Path(THEME_NEWS_HISTORY_DIR)
```

`_today_history_path` 等はすべて `HISTORY_DIR` 経由なので、定義差し替えで連鎖的に新パスになる。`HISTORY_DIR` が無ければ書き込み時に `mkdir -p` する処理が既にあるか確認し、なければ追加 (history 書き込み箇所)。

### 3. `scripts/webapp/routes/market.py` (L26-29)

```python
# 変更前
_THEME_NEWS_HISTORY_DIR = _PROJECT_ROOT / ".claude" / "skills" / "theme-news" / "history"
_CALENDAR_EVENTS_JSON = _PROJECT_ROOT / ".claude" / "skills" / "theme-news" / "events.json"
# 変更後
from ks_util import THEME_NEWS_HISTORY_DIR, THEME_NEWS_EVENTS_JSON
_THEME_NEWS_HISTORY_DIR = Path(THEME_NEWS_HISTORY_DIR)
_CALENDAR_EVENTS_JSON = Path(THEME_NEWS_EVENTS_JSON)
```

`_RUN_THEME_NEWS_SCRIPT` (run_theme_news.py 本体のパス) は **コード** なので変更しない (従来どおり `_PROJECT_ROOT / "scripts" / "run_theme_news.py"`)。

### 4. `.claude/skills/theme-news/render_calendar.py` (L18-21)

`EVENTS_JSON` / `OUTPUT_HTML` を `$KS_DATA_DIR/theme_news/` 配下に変更。`TEMPLATE_HTML` (calendar_template.html) は **コード** なので SKILL_DIR のまま据え置き。

```python
# DATA_DIR を取得 (resolve_today と同じ sys.path パターンを共通化)
from ks_util import THEME_NEWS_EVENTS_JSON, THEME_NEWS_CALENDAR_HTML
EVENTS_JSON = Path(THEME_NEWS_EVENTS_JSON)
TEMPLATE_HTML = SKILL_DIR / "calendar_template.html"   # コード: 据え置き
OUTPUT_HTML = Path(THEME_NEWS_CALENDAR_HTML)
```

- skill 配下から scripts/ks_util を import するため、ファイル先頭で sys.path に scripts を追加する処理を関数外に出す (現状 resolve_today 内のみ)
- **import 失敗時は即エラー終了** (旧パスにフォールバックしない、論点3 で確定)。`from ks_util import ...` が失敗したら `sys.exit(1)` で明示終了し、データ分裂を防ぐ
- 出力先ディレクトリが無ければ `mkdir -p` する (`OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)`)

### 5. `.claude/skills/theme-news/SKILL.md` (L55, 67, 255, 291, 322)

ドキュメント内のパス参照を `$KS_DATA_DIR/theme_news/` に書き換える。**Glob/Read/Write ツールは環境変数を展開しない**ため、AI が実際にアクセスする手順では「Bash で `$KS_DATA_DIR` を展開して絶対パスを得てから Glob/Read/Write する」書き方にする (symlink 不採用の代替)。

具体的な書き換え方針:
- L55 (history Glob): 「`$KS_DATA_DIR/theme_news/history/*.md` を Glob」+ 補足「`$KS_DATA_DIR` は Bash の `echo $KS_DATA_DIR` 等で実パスに展開してから Glob する」
- L67 (calendar.html Read): 同様に `$KS_DATA_DIR/theme_news/calendar.html`
- L255 (history Write): `$KS_DATA_DIR/theme_news/history/{今日}.md`、ディレクトリ無ければ `mkdir -p`
- L291, L322 (render_calendar 実行): `python .claude/skills/theme-news/render_calendar.py` は**コードのパスなので不変** (スクリプト本体は skill 配下のまま、出力先だけが新パスになる)
- L318 (calendar-archive 追記): 現状 `calendar-archive.html` と**誤記**しているが実ファイルは `calendar-archive.md`。これを `$KS_DATA_DIR/theme_news/calendar-archive.md` に修正 (ファイル名の `.md` 訂正 + 新パス化を同時に行う)

### 6. テスト

- `tests/test_run_theme_news.py`: `HISTORY_DIR` を monkeypatch で tmp_path に差し替えている既存パターンを維持。差し替え対象が `ks_util.THEME_NEWS_HISTORY_DIR` 経由になる点に追随 (run_theme_news の `HISTORY_DIR` を直接 setattr していれば変更不要の可能性、実装時に確認)
- `tests/test_webapp_routes.py`: theme-news 関連の mock パスを新パスに追随
- `tests/test_render_calendar.py` (あれば): EVENTS_JSON/OUTPUT_HTML の差し替えに追随

---

## データ移行 (コピー)

既存データを新パスにコピーする一度きりの移行スクリプト or 手順。**コピーであって移動ではない** (旧データは残す)。

移行は **2 段階** に分ける (codex 指摘2 への対応):

**(a) 初回コピー** — 既存履歴を取りこぼさず新パスへ複製 (既存非破壊)

```bash
SRC=.claude/skills/theme-news
DST=$KS_DATA_DIR/theme_news
mkdir -p "$DST/history"
cp -n "$SRC/events.json"         "$DST/" 2>/dev/null || true
cp -n "$SRC/calendar.html"       "$DST/" 2>/dev/null || true
cp -n "$SRC/calendar-archive.md" "$DST/" 2>/dev/null || true
cp -rn "$SRC/history/."          "$DST/history/" 2>/dev/null || true
```

**(b) 切替直前の再同期** — コード切替 (新パス参照) を本番反映する直前に、初回コピー後に旧パスへ増えた最新データを取りこぼさないよう **新しい方で上書き同期** する。`cp -n` ではなく更新日時ベースで反映する:

```bash
# rsync があれば (--update: dest より新しい src のみ上書き、既存の新しい方は保持)
rsync -a --update "$SRC/events.json" "$SRC/calendar.html" "$SRC/calendar-archive.md" "$DST/"
rsync -a --update "$SRC/history/" "$DST/history/"
# rsync 不可なら cp -u (newer のみ上書き) で代替
```

- 初回コピー (a) は冪等・既存非破壊 (`cp -n`)。再同期 (b) は「より新しい src で上書き」(`--update`/`-u`) なので、切替時点で新旧パスが最新一致する
- 移行スクリプトを Python で書く場合も (a)=既存非破壊コピー / (b)=mtime 比較で newer のみ上書き、の 2 モードを用意 (例: `--resync` フラグ)
- **旧データ削除はこの PR では行わない**

### Git 追跡の解除 (codex 指摘1 への対応)

`.gitignore` 追記だけでは **既に追跡済み**の生成データ (`events.json`, `calendar.html`, および過去に add されていれば `history/*` 等) は追跡解除されない。以下を両方行う:

1. **index から外す**: `git rm --cached` で追跡解除 (作業ツリーのファイルは消さない)
   ```bash
   git rm --cached .claude/skills/theme-news/events.json
   git rm --cached .claude/skills/theme-news/calendar.html
   git rm --cached .claude/skills/theme-news/calendar-archive.md
   # history 配下に追跡済みファイルがあれば: git rm -r --cached .claude/skills/theme-news/history
   ```
   - `git ls-files .claude/skills/theme-news/` で確認済みの追跡中生成データは `events.json` / `calendar.html` / `calendar-archive.md` の 3 つ (history 配下は現状未追跡)。実装時にも再確認してから rm --cached する
2. **.gitignore に追記**: 今後追跡されないよう生成データパターンを明示
   ```
   .claude/skills/theme-news/events.json
   .claude/skills/theme-news/calendar.html
   .claude/skills/theme-news/calendar-archive.md
   .claude/skills/theme-news/history/
   .claude/skills/theme-news/*.running
   .claude/skills/theme-news/*.done
   .claude/skills/theme-news/*.meta.json
   ```
   - `calendar_template.html` / `render_calendar.py` / `SKILL.md` は **コードなので ignore しない** (除外対象を生成データに限定)

新パス `$KS_DATA_DIR/theme_news/` は元々 Git 外 (KS_DATA_DIR は Google Drive) なので追跡対象外。

注: この `git rm --cached` により、本 PR は会話冒頭から残っていた theme-news 生成データの「未コミット変更が常に出る」状態も解消する (追跡を外すため diff に出なくなる)。

---

## 論点 / 要確認

1. **calendar-archive.md の扱い** (確定): 実ファイルは `calendar-archive.md` (SKILL.md L318 は `.html` と誤記)。中身は「30日経過エントリの append-only アーカイブ、grep 用途」で theme-news 実行時に追記される = **生成データ**。移行対象・`git rm --cached` 対象・`.gitignore` 対象に含める。SKILL.md L318 の誤記 (`.html`→`.md`) も本 PR で訂正する
2. **既存テストの monkeypatch 方式**: `HISTORY_DIR` をモジュール変数として setattr しているなら、ks_util 定数化後も `run_theme_news.HISTORY_DIR` への setattr は効く。実装時に確認し、必要なら fixture を ks_util 経由に調整
3. **render_calendar.py の ks_util import 失敗時の挙動** (確定: 即エラー終了): skill 配下から scripts を sys.path 追加して ks_util を import する。**import に失敗したら旧パスにフォールバックせず、明示的にエラーで終了する** (`sys.exit` or raise)。
   - 理由: 出力先パスを間違えるとデータ分裂・移行漏れに気づけない (codex 指摘)。旧パスへ黙って書くのが最悪。render_calendar は scripts と同居するリポジトリ内ツールで、ks_util が取れない = 異常事態なので即座に気づける方が安全
   - `resolve_today` の date.today フォールバック (日付は近似でも実害小) とは扱いを変える: パス定数は近似不可なのでフォールバックしない

---

## 検証ポイント

1. `pytest tests/test_run_theme_news.py tests/test_webapp_routes.py -v` 通過 (render_calendar テストがあれば追加)
2. データ移行スクリプト実行 → `$KS_DATA_DIR/theme_news/history/*.md` に既存履歴がコピーされる (元データは残存)
3. `python .claude/skills/theme-news/render_calendar.py` → `$KS_DATA_DIR/theme_news/calendar.html` が生成される
4. webapp 起動 → `/market` ページで Sources・カレンダー表示が崩れていない (新パスから読めている)
5. 過去履歴がゼロ件も消失していない (コピー元・先の件数一致)
6. `.claude/skills/theme-news/` にコードのみ残る状態を確認 (旧データは並行稼働のため当面残すが、コードが新パスを見ていることを確認)

---

## ロールバック

- コード変更は git revert で戻せる (パス定数の差し替えのみ)
- データはコピーなので、新パスを消しても旧パスのデータは無傷
- 旧データ削除を本 PR でしないため、移行失敗時も即座に旧運用に戻せる

---

## 受け入れ基準 (issue #279)

- [x] theme-news 手動実行で `$KS_DATA_DIR/theme_news/history/*.md` が生成される
- [x] /market ページで Sources・カレンダー表示が崩れていない
- [x] 過去履歴がゼロ件も消失していない (コピーで担保)
- [ ] `.claude/skills/theme-news/` にコードのみ残る ← **旧データ削除は別 PR (並行稼働後)**。本 PR では「コードが新パスを参照する」状態まで
