# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Japanese stock market analysis system that scrapes financial data from various sources (Kabutan, Yahoo Finance Japan), analyzes stocks based on multiple criteria (fundamentals, momentum, technical indicators), and ranks them for investment opportunities. The system focuses on identifying high-growth stocks using screening criteria inspired by growth stock investing methodologies.

## Architecture

### Core Data Flow

1. **Data Acquisition** (`shintakane.py` main flow):
   - Scrapes daily new high prices from Kabutan (`get_todays_shintakane()`)
   - Scrapes volume surge stocks (`get_todays_dekidakaup()`)
   - Fetches earnings announcement updates (`update_todays_kessan()`)
   - Combines these sources into candidate stock lists

2. **Stock Database** (Pickle-based):
   - Central database: `data/stock_data/stocks.pickle`
   - Contains all stock master data, prices, earnings, indicators, theoretical prices
   - Accessed via `make_stock_db.py` module

3. **Data Update Pipeline** (`update_db_rows()` in `make_stock_db.py`):
   - **Master data**: Basic company info, sector, themes, market cap (`master.py`)
   - **Price data**: Daily/weekly prices from Yahoo, technical indicators from Kabutan (`price.py`)
   - **Earnings data**: Quarterly/annual results, growth rates (`gyoseki.py`)
   - **Indicator data**: PER, PSR, ROE, margins, credit balance (`shihyou.py`)
   - **Theoretical price**: Fair value calculations (`rironkabuka.py`)

4. **Analysis & Ranking** (`list_all_db()` in `make_stock_db.py`):
   - Calculates composite score: 40% earnings + 20% indicators + 25% momentum + 15% fundamentals
   - Updates stock rankings in database
   - Generates CSV output with detailed metrics

5. **Market Analysis** (`make_market_db.py`):
   - Tracks market indices (TOPIX, S&P500, etc.) from `data/sisu_data/`
   - Analyzes sector and theme strength
   - Creates market summary CSV

### Key Technical Concepts

**Momentum Analysis** (`price.py`):
- **RS (Relative Strength)**: Weighted comparison of current price vs 13/26/39/52-week prices
- **Momentum Point**: RS normalized against TOPIX RS using normal distribution
- **Sell Pressure Ratio**: Ratio of buying vs selling volume based on candlestick decomposition
- **Candlestick Volatility**: Price range standardized by average closing price
- **Pocket Pivot**: High volume up-day exceeding all recent down-day volumes near MA
- **Trend Template**: 7-point checklist including MA relationships, 52-week position, RS threshold

**Earnings Quality** (`gyoseki.py`):
- Growth rates for current quarter and full year (sales & operating profit)
- Progress rates comparing current quarter to same quarter previous year
- 5-year and 4-quarter historical growth consistency

**Cache Management** (UPD_* constants in `ks_util.py`):
- `UPD_CACHE (0)`: Use cached data if available
- `UPD_INTERVAL (1)`: Refresh if interval exceeded, respect file cache
- `UPD_REEVAL (2)`: Re-evaluate logic but use file cache
- `UPD_FORCE (3)`: Force fresh download, ignore all caches

### Module Responsibilities

- `shintakane.py`: Main script for new high/volume screening workflow
- `make_stock_db.py`: Database management, ranking, CSV generation
- `price.py`: Price data fetching & technical analysis (Yahoo & Kabutan)
- `gyoseki.py`: Earnings data parsing & growth scoring
- `shihyou.py`: Financial indicators (valuation, profitability, credit)
- `rironkabuka.py`: Theoretical price calculations
- `master.py`: Company master data & theme analysis
- `make_market_db.py`: Market indices & sector/theme ranking
- `ks_util.py`: Utilities (HTTP, logging, pickle DB, file operations)
- `kessan.py`: Earnings calendar management
- `portfolio.py`: Personal portfolio tracking
- `googledrive.py`: Upload results to Google Drive

## Common Development Commands

### Running the Main Analysis

```bash
# Full analysis (scrape + analyze + rank)
cd scripts
python shintakane.py

# Just analyze existing data without scraping
python shintakane.py analyze

# Update & rank all stocks in database
python make_stock_db.py list_all_db
```

### Database Operations

```bash
# Update specific stocks
# Edit code_list in make_stock_db.py main() under command == "update"
python make_stock_db.py update

# View specific stock data
# Edit code_list in make_stock_db.py main() under command == "list"
python make_stock_db.py list

# Clean up delisted stocks
python make_stock_db.py reflesh

# Backup database
python make_stock_db.py backup
```

### Testing Individual Modules

```bash
# Test price data fetching for specific stock
# Edit code_list in price.py main()
python price.py

# Test market data update
python make_market_db.py
```

### Automated Execution

The cron script `shintakane_cron.sh` runs both main scripts:
1. `shintakane.py` - Scrapes new candidates & updates their data
2. `make_stock_db.py` - Updates top 100 stocks & portfolio, generates ranking CSV

## Data Storage

- **Database**: `data/stock_data/stocks.pickle` (main stock DB)
- **Cache**: `data/cache_data/` (HTTP response cache)
- **Price History**: `data/stock_data/yahoo/price/` and `data/stock_data/kabutan/price/`
- **Market Indices**: `data/sisu_data/` (.txt files with historical index prices)
- **Scraped Lists**: `data/shintakane_data/` (daily new high/volume surge CSV)
- **Results**: `data/shintakane_result_data/` and `data/code_rank_data/` (output CSV)
- **Logs**: `logs/` directory (rotating daily logs)

## Important Notes

### Scraping Sources & Format Changes

Yahoo Finance Japan and Kabutan frequently change their HTML formats. When price/earnings data fails:
1. Check `parse_price_text_yahoo_new()` in `price.py` for Yahoo format
2. Check `convert_kabutan_*_html()` functions in `shintakane.py` for Kabutan format
3. Recent changes handled: Yahoo switched to StyledNumber components (addressed in parse_price_text_yahoo_new)

### Session Management

HTTP requests use three session patterns:
- **Thread-local**: `use_requests_session()` context manager for single-threaded sequential requests
- **Global**: `use_requests_global_session()` for multi-threaded concurrent requests
- **Direct**: Falls back to `requests.get()` when no session active

Choose appropriate session type in `update_db_rows()` via `sync` parameter.

### Code Identifier Format

- All stock codes stored as **strings** (`code_s`), not integers
- Format: `"0001"` to `"9999"` for 4-digit codes
- Also supports alphanumeric codes like `"215A"` for certain securities
- Legacy `code` (int) keys exist but are deprecated

### Update Intervals & Decision Logic

Each data type (master, price, gyoseki, shihyo, rironkabuka) has:
- `has_*_data()`: Checks if data exists and is recent enough
- `get_*_data()`: Fetches from cache or web based on UPD parameter
- Earnings/indicators have special logic to force update after earnings announcement dates

### Multi-threading Considerations

`update_db_rows_async()` uses ThreadPoolExecutor (5 workers) for parallel data fetching. Ensure:
- Use `use_requests_global_session()` context manager
- Access to shared `stocks` dict is thread-safe (reads during fetch, writes after join)
- Semaphore limits concurrent HTTP requests to 3 (`MAX_REQUESTS` in `ks_util.py`)

## Python Environment

- **Python 3.9+** (uses virtual environment at `.venv/`)
- Key dependencies: `requests`, `scipy`, Google API libraries for Drive upload
- All dependencies in `requirements.txt`
- Originally Python 2, migrated to Python 3 (pickle encoding handled via `convert_pickle_latin1_to_utf8()`)

## Special Behaviors

- **Price day logic**: Before 18:00, price data is considered previous day (`get_price_day()` in `ks_util.py`)
- **Earnings updates**: Stocks auto-refresh when current date passes stored earnings announcement date
- **ETF filtering**: ETF codes loaded from `data/ETF_code.txt` and excluded from stock analysis
- **Logging**: All output goes through custom logger (`ks_util.py`), not direct print statements
