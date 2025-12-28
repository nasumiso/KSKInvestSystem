# Repository Guidelines

## Project Structure & Module Organization
`scripts/` holds the core Python modules and entry points (e.g., `shintakane.py`, `make_stock_db.py`). Data and cache files live under `data/` (stock DB, market indices, scraped CSV outputs), and runtime logs are written to `logs/`. Tests are minimal; `tests/` exists but is currently empty. Workspace settings are in `.vscode/` and `.code-workspace` files.

## Build, Test, and Development Commands
Run commands from `scripts/` unless noted:
- `python shintakane.py`: scrape candidates and update rankings.
- `python shintakane.py analyze`: analyze existing data without scraping.
- `python make_stock_db.py list_all_db`: update DB and generate ranking CSV.
- `python make_stock_db.py update`: update specific stocks (edit `code_list` in `make_stock_db.py`).
- `python make_market_db.py`: refresh market index summaries.

Dependencies are managed via `requirements.txt`; use a local `.venv/` with Python 3.9+.

## Coding Style & Naming Conventions
Python code uses 4-space indentation and snake_case for functions and variables. Filenames are lower_snake_case (e.g., `make_stock_db.py`). Linting is configured with `.flake8` and ignores `F403`, `F405`, and `E501`. Prefer small, focused functions with explicit data flow through the stock DB dictionary.

## Testing Guidelines
There is no formal test suite or coverage requirement. Use module-level scripts for manual verification (e.g., run `python price.py` after editing price parsers). If you add tests, place them under `tests/` and keep names descriptive (e.g., `test_price_parsing.py`).

## Commit & Pull Request Guidelines
Recent commit messages are short and direct, often in Japanese (e.g., “フォーマット変更対応”). Follow that convention: concise summary of the change, no scope prefixes required. For PRs, include:
- What data or output changed (e.g., CSVs in `data/code_rank_data/`).
- Steps to reproduce or verify (commands and inputs).
- Screenshots or sample rows if output formats changed.

## Data, Cache, and Logs
Primary DB: `data/stock_data/stocks.pickle`. HTTP cache: `data/cache_data/`. Outputs are written to `data/shintakane_result_data/` and `data/code_rank_data/`. Logs rotate in `logs/`; check them when scraping or parsing fails.
