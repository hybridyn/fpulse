"""CLI entrypoint for the eval harness.

Usage:
    python -m fpulse.eval.run [--category CATEGORY] [--save-to PATH]

Without flags: runs every category, saves a timestamped report under
eval_results/, and prints a summary table to stdout.

Also writes the most recent run as a stable artifact at
`<FPULSE_DATA_DIR>/eval/latest.json` so the trust page (Gate 4) can
surface the empirical pass rate without needing to know the timestamped
report path.

Exit code: 0 if all cases passed, 1 if any failed (CI-friendly).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .runner import run_all, run_category


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="F-Pulse AI eval harness")
    p.add_argument("--category", help="Run only one category (default: all)")
    p.add_argument("--save-to", help="JSON report path (default: eval_results/<ts>.json)")
    p.add_argument("--no-save", action="store_true", help="Don't write a report")
    # Risk C (review #3): tightenable CI gate. Default 0.0 means "any
    # failure makes the run exit 1" — same behavior as before this flag
    # existed (binary: all pass or fail). Set to 0.85 in CI to require
    # 85% category pass rate before merging prompt changes.
    p.add_argument(
        "--min-pass-rate",
        type=float,
        default=0.0,
        help=(
            "Minimum overall pass rate in [0, 1] to consider the run successful. "
            "When set, the exit code reflects this threshold instead of the "
            "all-or-nothing default. Use 0.85 in CI to catch prompt regressions "
            "without flake-induced false alarms."
        ),
    )
    # Per-category floor — separately from the overall rate. Catches
    # the cascade case where one category drops to 0% but the total
    # still passes the overall threshold (e.g. sanitization regression
    # buried under healthy planner_intent scores).
    p.add_argument(
        "--min-category-pass-rate",
        type=float,
        default=0.0,
        help=(
            "Minimum per-category pass rate in [0, 1]. Any category dropping "
            "below this floor fails the run, even if the overall rate is fine. "
            "Defends against single-category regressions hidden by aggregates."
        ),
    )
    args = p.parse_args(argv)

    save_to = args.save_to
    if not save_to and not args.no_save:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        save_to = f"eval_results/{ts}.json"

    if args.category:
        results = run_category(args.category, save_to=save_to)
    else:
        results = run_all(save_to=save_to)

    # Summary table
    print()
    print(f"{'Category':<22} {'Case':<32} {'Score':>6} {'Time':>8} {'Status':>8}")
    print("-" * 80)
    by_cat: dict[str, list] = {}
    for r in results:
        by_cat.setdefault(r.category, []).append(r)

    for cat in sorted(by_cat):
        for r in by_cat[cat]:
            status = "PASS" if r.passed else ("FAIL" if not r.error else "ERROR")
            print(f"{r.category:<22} {r.case:<32} {r.score:>6.2f} {r.elapsed_ms:>6}ms {status:>8}")

    print("-" * 80)
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    avg = sum(r.score for r in results) / max(total, 1)
    print(f"  {passed}/{total} passed  ·  avg score {avg:.2f}")

    if save_to:
        print(f"\n  Report: {save_to}")

    # Gate 4 — write a stable summary at <data_dir>/eval/latest.json so
    # the /api/trust/eval-summary endpoint can surface it. This summary
    # is intentionally smaller than the per-case report — it's what the
    # public trust page renders, so we strip the per-case prompts.
    try:
        data_dir = Path(os.environ.get("FPULSE_DATA_DIR", "data"))
        eval_dir = data_dir / "eval"
        eval_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "ran": True,
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "total": total,
            "passed": passed,
            "pass_rate": round(passed / max(total, 1), 4),
            "avg_score": round(avg, 4),
            "by_category": {
                cat: {
                    "total": len(items),
                    "passed": sum(1 for r in items if r.passed),
                    "avg_score": round(
                        sum(r.score for r in items) / max(len(items), 1), 4,
                    ),
                }
                for cat, items in by_cat.items()
            },
        }
        with open(eval_dir / "latest.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
    except OSError as exc:
        # Non-fatal — the timestamped report was already saved by the runner.
        print(f"  warning: could not write latest.json: {exc}", file=sys.stderr)

    # ── Exit code resolution ─────────────────────────────────────────
    # Three layers of gating, in order:
    #   1. If both threshold flags are zero, fall back to the legacy
    #      "any failure → exit 1" behavior so existing CI keeps working.
    #   2. Overall pass rate gate (--min-pass-rate).
    #   3. Per-category pass rate gate (--min-category-pass-rate).
    # The strictest applicable gate wins.
    overall_rate = passed / max(total, 1)
    legacy_mode = args.min_pass_rate <= 0.0 and args.min_category_pass_rate <= 0.0

    if legacy_mode:
        ok = (passed == total)
    else:
        ok = overall_rate >= args.min_pass_rate
        if ok and args.min_category_pass_rate > 0.0:
            for cat, items in by_cat.items():
                cat_passed = sum(1 for r in items if r.passed)
                cat_rate = cat_passed / max(len(items), 1)
                if cat_rate < args.min_category_pass_rate:
                    print(
                        f"  GATE FAILED: category {cat!r} pass-rate "
                        f"{cat_rate:.2%} < {args.min_category_pass_rate:.0%}",
                        file=sys.stderr,
                    )
                    ok = False

    if not legacy_mode:
        if ok:
            print(
                f"  GATE OK: {overall_rate:.2%} overall ≥ {args.min_pass_rate:.0%}",
            )
        else:
            print(
                f"  GATE FAILED: {overall_rate:.2%} overall < {args.min_pass_rate:.0%}",
                file=sys.stderr,
            )

    print()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
