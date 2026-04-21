#!/usr/bin/env python3
"""Benchmark Tier 1.1: st.cache_data persistence across Streamlit reruns.

Simulates two sequential scans (cold cache, then warm cache) and measures timing.
"""

import time
import sys
sys.path.insert(0, ".")

from src.scanner import scan_universe


def main():
    # Test with a small set of tickers
    tickers = ["AAPL", "MSFT", "GOOGL", "NVDA", "META"]

    print("\n" + "=" * 70)
    print("TIER 1.1 BENCHMARK: @st.cache_data persistence")
    print("=" * 70)

    print(f"\nTesting with {len(tickers)} tickers: {', '.join(tickers)}")
    print("\nNote: Running outside Streamlit uses MemoryCacheStorageManager")
    print("      Cache persists within this script, simulating rerun behavior.\n")

    # FIRST SCAN (cold cache)
    print("-" * 70)
    print("FIRST SCAN (cold cache)")
    print("-" * 70)
    start_1 = time.time()
    results_1 = list(scan_universe(tickers, fast_mode=True))
    elapsed_1 = time.time() - start_1

    avg_ticker_1 = (elapsed_1 * 1000) / len(tickers) if results_1 else 0
    print(f"✓ Completed {len(results_1)} tickers in {elapsed_1:.2f}s")
    print(f"  → {avg_ticker_1:.0f}ms/ticker")

    # SECOND SCAN (warm cache)
    print("\n" + "-" * 70)
    print("SECOND SCAN (warm cache)")
    print("-" * 70)
    start_2 = time.time()
    results_2 = list(scan_universe(tickers, fast_mode=True))
    elapsed_2 = time.time() - start_2

    avg_ticker_2 = (elapsed_2 * 1000) / len(tickers) if results_2 else 0
    print(f"✓ Completed {len(results_2)} tickers in {elapsed_2:.2f}s")
    print(f"  → {avg_ticker_2:.0f}ms/ticker")

    # COMPARISON
    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    speedup = elapsed_1 / elapsed_2 if elapsed_2 > 0 else 0
    time_saved = elapsed_1 - elapsed_2

    print(f"\nCold cache (first run):  {elapsed_1:.2f}s ({avg_ticker_1:.0f}ms/ticker)")
    print(f"Warm cache (second run): {elapsed_2:.2f}s ({avg_ticker_2:.0f}ms/ticker)")
    print(f"\nSpeedup: {speedup:.1f}×")
    print(f"Time saved: {time_saved:.2f}s ({time_saved/elapsed_1*100:.0f}%)")

    if speedup >= 2:
        print(f"\n✅ EXCELLENT: {speedup:.1f}× speedup achieved!")
    elif speedup >= 1.5:
        print(f"\n✓ GOOD: {speedup:.1f}× speedup achieved.")
    elif speedup > 1:
        print(f"\n~ MODEST: {speedup:.1f}× speedup.")
    else:
        print(f"\n✗ No speedup detected.")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
