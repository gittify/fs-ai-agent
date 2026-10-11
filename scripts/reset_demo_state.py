"""Delete demo state so a demo can be rehearsed again from scratch.

    python scripts/reset_demo_state.py --table <TableName output> \\
        --run-dates 2026-10-13 2026-10-14 2026-10-15 --yes

Removes only the run rows for the dates you list, plus the hold-notice ledger (so the same recall can
send its notice again). It never scans or clears the whole table. Refuses to run without --yes.
"""
from __future__ import annotations

import argparse
import sys

NOTICES = "NOTICES"


def reset(table, run_dates, include_notices: bool = True) -> int:
    deleted = 0
    for run_id in [*run_dates, *([NOTICES] if include_notices else [])]:
        kwargs = {"KeyConditionExpression": "run_id = :r", "ExpressionAttributeValues": {":r": run_id},
                  "ProjectionExpression": "run_id, item_key"}
        while True:
            page = table.query(**kwargs)
            with table.batch_writer() as batch:
                for row in page.get("Items", []):
                    batch.delete_item(Key={"run_id": row["run_id"], "item_key": row["item_key"]})
                    deleted += 1
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return deleted


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--table", required=True)
    ap.add_argument("--run-dates", nargs="+", required=True, metavar="YYYY-MM-DD")
    ap.add_argument("--keep-notices", action="store_true", help="do not clear the hold-notice ledger")
    ap.add_argument("--yes", action="store_true", help="required: confirms you want to delete these rows")
    args = ap.parse_args(argv)
    if not args.yes:
        print("Refusing to delete without --yes", file=sys.stderr)
        return 2
    import boto3
    n = reset(boto3.resource("dynamodb").Table(args.table), args.run_dates, not args.keep_notices)
    print(f"deleted {n} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
