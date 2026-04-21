"""
bench_scanner.py — Ad-hoc benchmark for scan_universe parallelization.

Runs scan_universe at varying max_workers values and prints timing deltas.
Intended for one-off measurement, not CI.

Usage:
  python scripts/bench_scanner.py --tickers 25 --fast
  python scripts/bench_scanner.py --tickers 100
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import massive
from src.scanner import scan_universe


def run(tickers: list[str], max_workers: int, fast: bool) -> dict:
    skip = {"VOL_PREMIUM"} if fast else None
    t0 = time.time()
    results, stats = scan_universe(
        tickers=tickers,
        skip_strategies=skip,
        max_workers=max_workers,
    )
    wall_ms = (time.time() - t0) * 1000
    errored = sum(1 for r in results if r.get("error"))
    return {
        "max_workers": max_workers,
        "wall_ms": wall_ms,
        "avg_ticker_ms": stats["avg_per_ticker_ms"],
        "batch_quote_ms": stats["batch_quote_ms"],
        "errored": errored,
        "n": len(results),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", type=int, default=25)
    ap.add_argument("--fast", action="store_true", help="skip VOL_PREMIUM")
    ap.add_argument("--workers", type=str, default="1,5",
                    help="comma-separated worker counts to compare")
    args = ap.parse_args()

    universe = massive.get_sp500_tickers()[: args.tickers]
    print(f"Benchmarking {len(universe)} tickers, fast_mode={args.fast}")
    print(f"Universe sample: {universe[:5]}...")
    print()

    rows = []
    for w in (int(x) for x in args.workers.split(",")):
        print(f"--- max_workers={w} ---")
        r = run(universe, w, args.fast)
        rows.append(r)
        print(f"  wall:        {r['wall_ms']:>10.0f} ms")
        print(f"  avg/ticker:  {r['avg_ticker_ms']:>10.0f} ms")
        print(f"  batch quote: {r['batch_quote_ms']:>10.0f} ms")
        print(f"  errors:      {r['errored']}/{r['n']}")
        print()

    if len(rows) > 1:
        base = rows[0]
        print("=== Speedup vs first run ===")
        for r in rows[1:]:
            ratio = base["wall_ms"] / r["wall_ms"] if r["wall_ms"] else 0
            print(f"  w={r['max_workers']}: {ratio:.2f}x faster "
                  f"({base['wall_ms']:.0f}ms → {r['wall_ms']:.0f}ms)")


if __name__ == "__main__":
    main()
