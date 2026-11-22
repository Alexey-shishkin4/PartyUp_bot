"""Measure discovery locally: python -m scripts.benchmark_feed --candidates 50 --games 100."""

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path

from app.config import ROOT
from app.db import Database
from app.recommender import AffinityModel
from app.schemas import ProfileInput
from app.service import Service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=int, default=50)
    parser.add_argument("--games", type=int, default=100)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    if not (1 <= args.candidates <= 2000 and 1 <= args.games <= 2000 and 1 <= args.runs <= 100):
        parser.error("Use 1–2000 candidates/games and 1–100 runs")
    with tempfile.TemporaryDirectory(prefix="partyup-benchmark-") as directory:
        db = Database("sqlite:///" + (Path(directory) / "benchmark.sqlite3").as_posix())
        try:
            db.initialize()
            service = Service(db, AffinityModel(ROOT / "data/affinity.json"))
            profile = ProfileInput(name="Benchmark Player", age=25, game_ids=[105600])
            games = [{"appid": 980000000 + index, "name": f"Benchmark Game {index}",
                      "playtime_forever": 600} for index in range(args.games)]
            actor_id = None
            for number in range(args.candidates + 1):
                user_id = service.save_profile(10000 + number, profile)
                service.import_games(user_id, games)
                if actor_id is None:
                    actor_id = user_id
            timings = []
            for _ in range(args.runs):
                start = time.perf_counter()
                service.feed(actor_id, limit=1, record_impressions=False)
                timings.append(time.perf_counter() - start)
            print(json.dumps({"candidates": args.candidates, "imported_games_per_profile": args.games,
                              "runs": args.runs, "median_seconds": round(statistics.median(timings), 4),
                              "min_seconds": round(min(timings), 4)}, indent=2))
        finally:
            db.engine.dispose()


if __name__ == "__main__":
    main()
