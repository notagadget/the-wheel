# Scanner Speed Optimization Plan

**Current state**: ~2000ms/ticker × 100 tickers + 1s sleep = ~5 min full scan  
**Target**: Sub-1-minute full scan  
**Status**: Active optimization

## Profiling Results

### Baseline (pre-optimization)
- Common data fetch: 475ms (24%)
  - `daily_bars` (Tradier): 323ms ⚠️
  - `ticker_details` (Massive): 152ms
- Strategy evaluation: 1473ms (76%)
  - VOL_PREMIUM: 1107ms (54%) ⚠️⚠️
    - `current_iv`: 744ms (includes expirations 170ms + options_chain 280ms + quote 170ms)
    - `historical_iv`: 363ms
  - TECHNICAL: 451ms
    - `sma` (Massive): 199ms ← eliminated
    - `daily_bars_30d` (Massive): 152ms ← eliminated
  - ETF_COMPONENT: 115ms
    - `institutional_ownership` (yfinance): 115ms
- **Total: ~2000ms/ticker + 1s sleep**

### After Tier 1 Session 1 (Fast Mode on, 12-ticker run, 2026-04-19)
- **Avg/ticker: 589ms | Total: 19.5s** (sleep still present)

### After Tier 1 Session 2 — sleep removed (Fast Mode on, 28-ticker run, 2026-04-19)
- `daily_bars` (Tradier): 331ms (57% of ticker time)
- `ticker_details` (Massive): 153ms (26%)
- `institutional_ownership` (yfinance): 101ms (17%)
- SMA, RSI, avg_volume: 0ms (computed locally)
- Batch quotes: 323ms (amortized across all tickers)
- **Avg/ticker: 586ms | Min: 505ms | Max: 761ms | Total: 16.7s**
- **Projection (100 tickers, Fast Mode): ~59s ✅ sub-1-minute target hit**
- Projection (100 tickers, full scan w/ VOL_PREMIUM): ~170s = 2.8 min (needs parallelization)

### Known Constraints
- **Streamlit reruns script on every interaction** → clears `@lru_cache` between runs
- **Tradier batch endpoints**: `/v1/markets/quotes` supports comma-separated symbols ✓; `/v1/markets/history` does NOT
- **Rate limits**: Tradier 250 req/hr (~4 req/sec), Massive unknown, yfinance generous
- **Sequential Tradier calls in Fast Mode**: 1 call/ticker at 589ms/ticker = 1.7 req/sec — well under limit, sleep is unnecessary
- **One-time optimizations done**:
  - ✓ Batch quotes (saves ~2.1s per scan)
  - ✓ Added `@lru_cache` to `get_quote`, `get_historical_iv`, `get_expirations`, `get_options_chain`
  - ✓ Added caching to Massive functions (`ticker_details`, `get_sma`, `get_daily_bars`)
  - ✓ Added caching to yfinance (`get_institutional_ownership_pct`)
  - ✓ SMA-200 computed locally from Tradier bars (saves 199ms/ticker)
  - ✓ RSI bars sourced from Tradier daily_bars (saves 152ms/ticker)
  - ✓ Fast Mode toggle skips VOL_PREMIUM (saves ~1100ms/ticker)

---

## Tier 1: Quick Wins (Start Here)

### 1.0 Remove Inter-Ticker Sleep ✅ DONE
**Problem**: `time.sleep(1.0)` in `scan_universe()` adds 100s to a 100-ticker scan. Originally needed to respect Tradier's 250 req/hr limit, but with batch quotes and Fast Mode the actual Tradier rate in Fast Mode is only ~1.7 req/sec — far under the 4/sec ceiling.

**Solution**: Remove the sleep entirely. If rate-limit 429s appear, add exponential backoff in `tradier._get()` instead.

**Files to touch**:
- `src/scanner.py`: Remove `time.sleep(1.0)` from `scan_universe()` loop

**Impact**: Fast Mode 100-ticker scan: 159s → ~59s (sub-1-minute target hit)  
**Effort**: 5 minutes  
**Risk**: None at current sequential throughput; add backoff only if 429s actually occur

---

### 1.1 Make Caching Work in Streamlit (`@st.cache_data`)
**Problem**: `@lru_cache` clears when Streamlit reruns the script (on every button click, slider change, etc.)

**Solution**: Replace `@lru_cache` with `@st.cache_data(ttl=...)` for:
- Daily-changing data (expirations, daily_bars, SMA, hist_iv): **ttl=86400** (1 day)
- Intraday data (quotes, options chain): **ttl=300** (5 min)

**Files to touch**:
- `src/tradier.py`: `get_expirations`, `get_options_chain`, `get_historical_iv`, `get_quote`
- `src/massive.py`: `get_ticker_details`, `get_sma`, `get_daily_bars`
- `src/yfinance_data.py`: `get_institutional_ownership_pct`
- `src/market_data.py`: `get_current_iv`

**Impact**: 2-3× faster on repeat scans (cache survives Streamlit reruns)  
**Effort**: 1-2 hours  
**Risk**: Low (Streamlit caching is well-tested)

**Note**: Requires importing `streamlit` in non-UI modules. Acceptable since they're already in the app codebase.

---

### 1.2 VOL_PREMIUM Optional ("Fast Mode")
**Problem**: VOL_PREMIUM strategy is 54% of scan time (1100ms/ticker)

**Solution**: Add UI toggle in Eligibility page:
```python
fast_mode = st.checkbox("⚡ Fast Mode (skip volatility analysis)", value=False)
```

Then in `scanner.py` `scan_ticker()`, accept `skip_strategies` param:
```python
if "VOL_PREMIUM" in skip_strategies:
    strategies.pop("VOL_PREMIUM", None)
```

**Files to touch**:
- `pages/5_Eligibility.py`: Add checkbox, pass to `scan_universe()`
- `src/scanner.py`: Add `skip_strategies` param to `scan_ticker()` and `scan_universe()`

**Impact**: 2× faster when enabled (~900ms/ticker)  
**Effort**: 30 min  
**Risk**: None

---

### 1.3 Compute SMA Locally from `daily_bars`
**Problem**: Separate API call to Massive `/v1/indicators/sma/{symbol}` costs 199ms/ticker

**Solution**: 
1. Fetch 200 days of bars in `_fetch_common_data()` (instead of 45)
2. In TECHNICAL strategy, compute SMA-200 from closes in Python:
```python
def compute_sma(bars, window=200):
    closes = [b["close"] for b in bars if b["close"]]
    return sum(closes[-window:]) / window if len(closes) >= window else None
```

**Files to touch**:
- `src/scanner.py`: `_fetch_common_data()`, `_evaluate_strategy()` TECHNICAL section

**Impact**: ~200ms saved per ticker (eliminates one API call)  
**Effort**: 1 hour  
**Risk**: Low (SMA computation is trivial)

---

## Tier 2: Medium Wins (After Tier 1)

### 2.1 Parallelize Tickers with ThreadPoolExecutor
**Problem**: Sequential ticker scans = wasted I/O time

**Solution**:
1. In `scan_universe()`, batch tickers into groups of 5-10
2. Use `ThreadPoolExecutor(max_workers=5)` to fan out `scan_ticker()` calls
3. Remove the 1s sleep (or reduce to 0.1s); rate limit becomes constraint
4. Implement exponential backoff for 429 (rate limit) responses from Tradier

**Files to touch**:
- `src/scanner.py`: `scan_universe()` (major refactor)
- `src/tradier.py`: Add retry logic to `_get()`

**Impact**: 5-10× speedup (if rate limits permit N=5 workers)  
**Effort**: 2-3 hours  
**Risk**: Medium (rate limiting, thread safety, progress callback complexity)

**Test plan**:
- Start with N=2, measure actual requests/sec to Tradier
- Calculate safe N = (250 req/hr) / (per-ticker API calls) = (250/3600) / 12 ≈ 0.006 req/sec → too slow
- Actually, need to think harder: if we parallelize 5 tickers, each doing 12 API calls = 60 concurrent req/sec → way over limit
- Better: queue with semaphore limiting concurrent Tradier calls to 4/sec

---

### 2.2 SQLite-Backed Persistent Cache
**Problem**: Even `@st.cache_data` clears on app restart

**Solution**:
1. Cache tables in `db/wheel.db`:
   - `scan_cache(symbol, date, data_type, value, ttl_expires_at)`
2. Lazy-refresh: check `ttl_expires_at` on read; if stale, re-fetch and update
3. TTL: 1 day for daily_bars, SMA, expirations; 5 min for quotes

**Files to touch**:
- `db/schema.sql`: Add `scan_cache` table
- `src/scanner.py`: Wrap expensive functions with cache check/update

**Impact**: Near-instant results on repeat scans for same tickers  
**Effort**: 3-4 hours  
**Risk**: Medium (cache invalidation logic, schema migration)

---

## Tier 3: Investigate & Bigger Changes

### 3.1 Research Batch Endpoints via MCP
**Action items**:
- Use `mcp__Massive_Market_Data__search_endpoints` to find batch SMA, batch daily_bars, batch ticker_details
- Check Tradier MCP for any batch endpoints beyond quotes

**Expected findings**:
- Massive likely has batch endpoints (worth investigating)
- Tradier doesn't support batch for history, expirations, or options chains

---

### 3.2 Eliminate `get_current_iv()` (Reconsider VOL_PREMIUM)
**Alternative approach**:
- Current: Fetch options chain → extract ATM implied volatility (3 API calls: expirations + chain + quote)
- Alternative: Use historical volatility from daily_bars (already free)
- Compute IV rank: (current_hv - 52w_low) / (52w_high - 52w_low) * 100

**Impact**: 700ms+ saved per ticker (eliminates options chain call)  
**Risk**: High (changes IV methodology; needs stakeholder review)

**Decision point**: Only pursue if Tier 1+2 don't hit 1-minute target

---

## Implementation Order (Recommended)

1. **Session 1**: Tier 1.2 (VOL_PREMIUM toggle) + Tier 1.3 (local SMA)
   - Low risk, quick wins, gets you to ~900ms/ticker
   - Test: Full scan should be 1.5-2 min

2. **Session 2**: Tier 1.1 (Streamlit caching)
   - More involved but high impact
   - Test: Run scan twice; second run should be 2-3× faster

3. **Session 3**: Tier 3.1 (research batch endpoints)
   - Low effort, might uncover more batch opportunities

4. **Session 4**: Tier 2.1 (parallelization) if still needed
   - Complex; only if above doesn't hit sub-1-minute target

5. **Session 5+**: Tier 2.2 (SQLite cache), Tier 3.2 (rethink IV) as needed

---

## Metrics to Track

After each change, measure:
- Per-ticker time (avg, min, max)
- Total scan time for 10 and 100 tickers
- Time to first result
- Time on cache hit vs cache miss

Log in Eligibility page:
```
Scan completed in 34.2s
- 10 tickers: 1720ms avg/ticker
- First scan (uncached): 2100ms avg
- Second scan (cached): 900ms avg
```

---

## Blockers & Questions

1. Can we import `streamlit` in `src/` modules? (Confirm with architecture)
2. What's Massive API rate limit? (Needed for parallelization math)
3. Is IV rank (from HV) acceptable as VOL_PREMIUM metric? (Product decision)
4. How many tickers in typical universe? (Affects parallelization benefit)

---

## Notes

- The 1s sleep between tickers was added to respect Tradier's 250 req/hr limit, but with batch quotes and parallelization, that constraint changes
- All timings are from `src/scanner.py` detailed profiling; trust those numbers
- Streamlit's cache decorators require `ttl` parameter; set conservatively to avoid stale data
