"""Measure the served latency the target is actually stated in: N concurrent users.

Per-query latency measured in isolation hides the queueing that matters here -- the
GPU lock serializes reranking, so the third simultaneous caller waits behind two
full searches. This fires N queries at once and reports what each caller saw.

    python scripts_evaluation/bench_concurrency.py --concurrency 3 --rounds 4
"""

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import requests

QUERIES = [
    "who won the nobel prize in physics in 1987",
    "which city hosted the summer olympics the year the berlin wall fell",
    "a novelist who wrote under three pseudonyms and died in a boating accident",
    "the first artificial satellite launched by a country other than the US or USSR",
    "which chemical element was named after a village in scotland",
    "a bridge that collapsed twice in the same decade and was rebuilt in steel",
    "the mathematician who proved the four colour theorem with computer assistance",
    "which film won best picture the year its director died before the ceremony",
    "a species of bird declared extinct then rediscovered fifty years later",
    "the treaty that ended a war lasting thirty eight minutes",
    "which university was founded by a former slave in the nineteenth century",
    "the composer who wrote a symphony discovered in an attic in 1982",
]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:17070")
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--rounds", type=int, default=4)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--warmup", action="store_true", help="Discard the first round.")
    p.add_argument("--out", default="")
    cli = p.parse_args()

    session = requests.Session()

    def one(query):
        t0 = time.time()
        r = session.post(
            f"{cli.base}/search", json={"query": query}, timeout=cli.timeout
        )
        elapsed = time.time() - t0
        r.raise_for_status()
        return elapsed, len(r.json())

    latencies, wall_times = [], []
    rounds = cli.rounds + (1 if cli.warmup else 0)
    for rnd in range(rounds):
        batch = [
            QUERIES[(rnd * cli.concurrency + i) % len(QUERIES)]
            for i in range(cli.concurrency)
        ]
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=cli.concurrency) as pool:
            got = list(pool.map(one, batch))
        wall = time.time() - t0
        if cli.warmup and rnd == 0:
            print(f"  warmup round: wall {wall:.1f}s (discarded)", flush=True)
            continue
        per = [e for e, _ in got]
        latencies.extend(per)
        wall_times.append(wall)
        print(
            f"  round {len(wall_times)}: wall {wall:.1f}s  "
            f"per-caller {' '.join(f'{e:.1f}' for e in sorted(per))}  "
            f"results {[n for _, n in got]}",
            flush=True,
        )

    latencies.sort()
    summary = {
        "concurrency": cli.concurrency,
        "rounds": len(wall_times),
        "latency_mean": statistics.mean(latencies),
        "latency_p50": latencies[len(latencies) // 2],
        "latency_max": max(latencies),
        "wall_mean": statistics.mean(wall_times),
        "throughput_qps": cli.concurrency / statistics.mean(wall_times),
    }
    print(
        f"\nconcurrency={summary['concurrency']}  "
        f"per-caller latency: mean {summary['latency_mean']:.1f}s "
        f"p50 {summary['latency_p50']:.1f}s max {summary['latency_max']:.1f}s\n"
        f"round wall time: {summary['wall_mean']:.1f}s  "
        f"throughput: {summary['throughput_qps']:.2f} q/s"
    )
    if cli.out:
        with open(cli.out, "w") as fh:
            json.dump(summary, fh, indent=2)


if __name__ == "__main__":
    main()
