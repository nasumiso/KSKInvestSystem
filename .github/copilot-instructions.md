# GitHub Copilot / AI agent instructions for shintakane

## Purpose
Provide concise, actionable guidance so an AI coding agent can be immediately productive in this repository.

## High-level architecture (big picture)
- This repo is a small, single-hosted data pipeline + analysis tool for Japanese stock research.
- Main components:
  - scripts/ : core Python modules and orchestration scripts (entrypoints like `shintakane.py`, `make_stock_db.py`).
  - data/ : persistent CSVs, cached HTML, and the single DB pickle `data/stock_data/stocks.pickle`.
  - logs/ : application logs (per-script, rotated by `ks_util.setup_logger`).
  - cron / launchd integration: `shintakane_cron.sh` and `com.k_sohara.shintakane.cron.plist` schedule nightly runs.
- Data flow: crawler/scraper -> (cache_data) -> per-module parsers (e.g., `master.py`, `rironkabuka.py`) -> consolidated DB pickle `stocks.pickle` -> CSV exports / Google Drive uploads (`googledrive.py`).

## Important files & responsibilities (quick map)
- `scripts/shintakane.py` — high-level orchestration: collects "新高値"/出来高急増 lists, composes candidate lists, calls database updates, generates market CSVs.
- `scripts/make_stock_db.py` — core DB update logic (table-based updates: `master`, `price`, `gyoseki`, `shihyo`, `rironkabuka`).
- `scripts/ks_util.py` — utilities used across the project (constants: `DATA_DIR`, `UPD_INTERVAL`, logging helpers `log_print` / `log_warning`, `get_price_day`, session helpers). Read this first.
- `scripts/master.py`, `scripts/price.py`, `scripts/gyoseki.py`, `scripts/shihyou.py`, `scripts/rironkabuka.py` — per-domain scrapers/parsers and data transformers.
- `scripts/googledrive.py` — Google Drive upload/update; requires OAuth credentials under `data/googledrive/`.
- `shintakane_cron.sh` & `com.k_sohara.shintakane.cron.plist` — how the system is scheduled and run on macOS (launchd).
- `data/stock_data/stocks.pickle` — canonical project DB (pickle). Many functions read/write this file.

## Conventions & patterns to follow (specific)
- **コードコメント・`docstring`は日本語で記述してください。** 簡潔で分かりやすい日本語を使い、技術用語やコード・識別子は英語のまま残して問題ありません（例: `get_price_day()` や外部ライブラリ名）。
- stock identifier: `code_s` (string) is the canonical key for stocks in the DB and across functions — prefer `code_s` over numeric `code`.
- Update modes: use `UPD_INTERVAL`, `UPD_REEVAL`, `UPD_FORCE` (defined in `ks_util.py`) to control freshness when calling update functions.
- Logging: prefer `log_print`, `log_warning`, `log_error` helpers (they wire to the centralized logger and rotate files automatically).
- DB updates: prefer `update_db_rows(code_list, upd=..., tables=[...], sync=Boolean)` from `make_stock_db.py` — prefer async `sync=False` for bulk operations when safe.
- Date/price handling: `ks_util.get_price_day(dt)` applies the project-specific rule where price timestamps before 18:00 count as previous trading day.
- Caching: HTML caches live under `data/cache_data` and cached kabutan files are referenced by cache helpers (e.g., `rironkabuka.get_kabutan_base_html`). Respect existing cache files when debugging.

## External integrations & secrets
- Google Drive API: `scripts/googledrive.py` uses client secret + credentials in `data/googledrive/` — keep these secrets out of commits. The repo expects the credential files at those paths.
- Web scraping: the code scrapes public sites (kabutan, etc.). Be conservative with concurrency and obey site rules (the project uses a semaphore and small `MAX_REQUESTS`).

## Developer workflows (how to run & debug)
1. Local environment
   - Activate local virtualenv (this repo uses `.venv`):
     - `source .venv/bin/activate`
2. Manual runs (quick)
   - Update today's data: (from repo root)
     - `cd scripts && python shintakane.py` — runs main orchestration
     - `cd scripts && python make_stock_db.py` — runs DB update routines
   - Run a single-module update (example): to re-evaluate price/gyoseki for codes `['7203','9984']` use `update_db_rows([...], upd=UPD_REEVAL, tables=['price','gyoseki'])` in an interactive session or small script.
3. Scheduled runs
   - macOS launchd: copy `com.k_sohara.shintakane.cron.plist` to `~/Library/LaunchAgents/` and load/unload with `launchctl`.
   - Shell-based cron runner: `shintakane_cron.sh` activates `.venv` and runs both `shintakane.py` and `make_stock_db.py` — check `logs/` for outputs.
4. Debugging
   - Check `logs/<scriptname>.log` (TimRotatingFileHandler, 7 days retention).
   - Use `ks_util.setup_logger()` to initialize logging when running scripts interactively.
   - For network-heavy tasks, prefer `sync=False` (async) paths for throughput debugging.

## Testing & QA
- This repo lacks a standardized test runner (no `pytest.ini` or CI config). There are small functional/test scripts in `scripts/` (e.g., `test_functional.py`).
- To run lightweight checks: `python scripts/test_functional.py` or run module-level `main()` functions for smoke tests.
- When adding tests, follow the repository style: small script-like tests, assert-based checks, and use the project's `log_print` for visibility.

## Code change guidance for AI code-editing
- Keep backward compatibility for the stocks pickle schema. Update `make_stock_db.py` load/save logic if changing fields, and add a migration snippet when necessary.
- Preserve `ks_util` helper semantics: do not replace `log_print` with print() directly; use `get_price_day()` for date-sensitive logic.
- Prefer non-blocking/parallel HTTP patterns already in use: `use_requests_session()` and `update_db_rows_async()`.
- When changing external API auth (Google Drive), update `scripts/googledrive.py` paths and make sure to document credential file locations in `data/googledrive/`.

## Quick examples (copyable)
- Activate env and run the main pipeline:

    source .venv/bin/activate && cd scripts && python shintakane.py

- Update specific tables for codes (interactive REPL example):

    >>> from scripts.make_stock_db import update_db_rows
    >>> update_db_rows(['7203','9984'], upd=2, tables=['price','gyoseki'], sync=False)

- Inspect current DB pickle (explore safely):

    >>> from scripts.make_stock_db import STOCKS_PICKLE
    >>> import pickle
    >>> stocks = pickle.load(open(STOCKS_PICKLE,'rb'))
    >>> list(stocks.keys())[:10]

## Notes & constraints discovered
- Some modules use legacy libraries (e.g., `oauth2client`, `apiclient`); consider modernizing carefully and ensure credentials still work.
- The DB is a single pickle file — concurrent writes should be avoided; use provided update APIs.
- Sensitive files (Google credential files) live in `data/googledrive/` and must not be committed.

---
If you'd like, I can iterate on tone, add OP-level quickstart steps, or extract more concrete examples from `price.py` and `master.py`. Any sections unclear or incomplete?