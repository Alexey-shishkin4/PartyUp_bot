"""Train item-item cosine affinity on Kaggle Steam Video Games; never export account IDs.

Usage: python scripts/train_model.py --csv ../../work/dataset/steam-200k.csv
"""
import argparse
import csv
import hashlib
import json
import math
import random
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy import sparse

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "https://www.kaggle.com/datasets/tamber/steam-video-games"


def key(value):
    return re.sub(r"[^\w]+", " ", value.casefold()).strip()


def fit(users, titles):
    rows, cols, values = [], [], []
    for row, games in enumerate(users):
        selected = sorted(games.items(), key=lambda item: item[1], reverse=True)[:100]
        for title, hours in selected:
            if title in titles:
                rows.append(row)
                cols.append(titles[title])
                values.append(min(math.log1p(hours), 7))
    matrix = sparse.csr_matrix((values, (rows, cols)), shape=(len(users), len(titles)), dtype=np.float32)
    row_norm = np.sqrt(np.asarray(matrix.multiply(matrix).sum(axis=1)).ravel())
    matrix = sparse.diags(1 / np.maximum(row_norm, 1e-8)) @ matrix
    norm = np.sqrt(np.asarray(matrix.multiply(matrix).sum(axis=0)).ravel())
    matrix = matrix @ sparse.diags(1 / np.maximum(norm, 1e-8))
    similarity = (matrix.T @ matrix).tocsr()
    similarity.setdiag(0)
    similarity.eliminate_zeros()
    return similarity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "app/resources/affinity.json")
    args = parser.parse_args()
    users, count, names = defaultdict(dict), 0, {}
    with args.csv.open(encoding="utf-8-sig", newline="") as file:
        for row in csv.reader(file):
            count += 1
            if len(row) < 4 or row[2] != "play":
                continue
            try:
                hours = float(row[3])
            except ValueError:
                continue
            if hours <= 0 or not math.isfinite(hours):
                continue
            title = key(row[1])
            names[title] = row[1]
            users[row[0]][title] = max(hours, users[row[0]].get(title, 0))
    ordered = list(sorted(users))
    rng = random.Random(42)
    rng.shuffle(ordered)
    cutoff = max(1, int(len(ordered) * 0.8))
    train = [users[identity] for identity in ordered[:cutoff]]
    test = [users[identity] for identity in ordered[cutoff:]]
    frequencies = Counter(title for row in train for title in row)
    titles = {title: index for index, title in enumerate(sorted(title for title, n in frequencies.items() if n >= 3))}
    similarity = fit(train, titles)
    popularity = np.zeros(len(titles))
    for title, index in titles.items():
        popularity[index] = frequencies[title]
    hits, baseline, evaluated = 0, 0, 0
    for games in test:
        available = sorted(title for title in games if title in titles)
        if len(available) < 4:
            continue
        heldout = rng.choice(available)
        known = [title for title in available if title != heldout]
        vector = np.zeros(len(titles), dtype=np.float32)
        for title in known:
            vector[titles[title]] = min(math.log1p(games[title]), 7)
        predicted = np.asarray(similarity @ vector).ravel()
        popular = popularity.copy()
        indices = [titles[title] for title in known]
        predicted[indices] = popular[indices] = -np.inf
        top = np.argsort(-predicted, kind="stable")[:10]
        pop_top = np.argsort(-popular, kind="stable")[:10]
        hits += int(titles[heldout] in top)
        baseline += int(titles[heldout] in pop_top)
        evaluated += 1
    # Refit for serving after the independent-user evaluation; same fixed item vocabulary.
    full = fit(list(users.values()), titles)
    inverse = {index: title for title, index in titles.items()}
    neighbors = {}
    for title, index in titles.items():
        row = full.getrow(index)
        order = np.argsort(-row.data, kind="stable")[:30]
        neighbors[title] = {inverse[int(row.indices[i])]: round(float(row.data[i]), 6)
                            for i in order if row.data[i] >= 0.025}
    metrics = {"evaluation": "held-out users (20%), one held-out played game per user, >=4 known items",
               "seed": 42, "evaluated_users": evaluated, "candidate_games": len(titles),
               "hit_rate_at_10": round(hits / max(evaluated, 1), 4),
               "popularity_hit_rate_at_10": round(baseline / max(evaluated, 1), 4),
               "limitations": ["2017 dataset; no temporal split is possible", "Measures game recovery, not human compatibility",
                               "Only users with >=4 in-vocabulary games; full catalog candidates, no sampled negatives"]}
    payload = {"version": "kaggle-steam-cosine-v1", "source": SOURCE,
               "source_updated": "2017-03-09", "source_sha256": hashlib.sha256(args.csv.read_bytes()).hexdigest(),
               "license": "ODbL-1.0; contents DbCL-1.0", "training_rows": count,
               "training_users": len(users), "games": len(titles), "trained_at": datetime.now(timezone.utc).isoformat(),
               "metrics": metrics, "neighbors": neighbors}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (args.output.parent / "model_report.json").write_text(json.dumps({k: v for k, v in payload.items() if k != "neighbors"},
                                                                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"users": len(users), "games": len(titles), **metrics}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
