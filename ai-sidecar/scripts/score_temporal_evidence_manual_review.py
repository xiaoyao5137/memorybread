"""计算两份阶段 A 人工标注的一致性，不输出来源正文。Python 3.9 兼容。"""

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

FIELDS = ("decision", "status")


def load_rows(path: Path) -> Dict[str, Dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    result = {}
    for row in rows:
        case_id = str(row.get("id") or "").strip()
        if not case_id or case_id in result:
            raise ValueError("duplicate or empty id in %s" % path)
        result[case_id] = row
    return result


def cohen_kappa(left: List[str], right: List[str]) -> Optional[float]:
    if len(left) != len(right) or not left:
        return None
    total = len(left)
    observed = sum(a == b for a, b in zip(left, right)) / float(total)
    left_counts = Counter(left)
    right_counts = Counter(right)
    labels = set(left_counts) | set(right_counts)
    expected = sum(
        (left_counts[label] / float(total)) * (right_counts[label] / float(total))
        for label in labels
    )
    if expected == 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return round((observed - expected) / (1.0 - expected), 6)


def score(
    left_rows: Dict[str, Dict[str, str]], right_rows: Dict[str, Dict[str, str]]
) -> Tuple[Dict[str, object], bool]:
    ids = sorted(set(left_rows) | set(right_rows))
    missing_left = [case_id for case_id in ids if case_id not in left_rows]
    missing_right = [case_id for case_id in ids if case_id not in right_rows]
    incomplete = []
    field_results = {}
    disagreements = set()
    for field in FIELDS:
        left_values = []
        right_values = []
        for case_id in ids:
            left = str((left_rows.get(case_id) or {}).get(field) or "").strip()
            right = str((right_rows.get(case_id) or {}).get(field) or "").strip()
            if not left or not right:
                incomplete.append({"id": case_id, "field": field})
                continue
            left_values.append(left)
            right_values.append(right)
            if left != right:
                disagreements.add(case_id)
        agreement = (
            sum(a == b for a, b in zip(left_values, right_values))
            / float(len(left_values))
            if left_values
            else None
        )
        field_results[field] = {
            "compared": len(left_values),
            "agreement": round(agreement, 6) if agreement is not None else None,
            "cohen_kappa": cohen_kappa(left_values, right_values),
        }
    complete = not missing_left and not missing_right and not incomplete
    result = {
        "schema_version": "memorybread.temporal-manual-review.v1",
        "case_count": len(ids),
        "complete": complete,
        "missing_left": missing_left,
        "missing_right": missing_right,
        "incomplete": incomplete,
        "fields": field_results,
        "disagreement_ids": sorted(disagreements),
        "passes_recommended_gate": complete
        and all((field_results[field]["cohen_kappa"] or 0) >= 0.80 for field in FIELDS),
    }
    return result, complete


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left", required=True, type=Path)
    parser.add_argument("--right", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result, complete = score(load_rows(args.left), load_rows(args.right))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
