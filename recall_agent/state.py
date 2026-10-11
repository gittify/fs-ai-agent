"""DynamoDB state for the recall agent.

One table, string keys `run_id` (partition) and `item_key` (sort).

Two kinds of rows:

  Run rows       run_id = "<run date>", item_key = "supplier#sunrise-foods-inc"
                 Holds the item's result (as a JSON string, since DynamoDB
                 rejects Python floats). A rerun with the same run_id skips
                 items that already have a FINAL result. NOT_CHECKED is not
                 final: a rerun retries it, which is the whole point of
                 recording it honestly instead of guessing.

  Notice rows    run_id = "NOTICES", item_key = "<item>|<recall numbers>"
                 A ledger so a hold notice is sent once per (item, recall),
                 not once per Monday while the recall stays in the window.
                 claim -> send -> finish(ok). A failed send can be retried; a
                 claim left PENDING by a crashed Lambda expires after
                 STALE_SECONDS so it cannot block a notice forever.

Run results are written with update_item, never put_item, so re-saving a
result can never wipe notice state.
"""
from __future__ import annotations

import json
import os
import re
import time

NOTICES = "NOTICES"
STALE_SECONDS = 15 * 60  # Lambda's maximum runtime
FINAL = ("HOLD", "CLEAR", "NEEDS_REVIEW")


def key_from(name: str, kind: str) -> str:
    return f"{kind}#{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}"


def item_key(item) -> str:
    return key_from(item.name, item.kind)


def notice_key(item, recall_numbers) -> str:
    return f"{item_key(item)}|{'+'.join(sorted(recall_numbers))}"


def _code(e: Exception) -> str | None:
    return getattr(e, "response", {}).get("Error", {}).get("Code")


class DynamoStore:
    def __init__(self, table):
        self.t = table

    @classmethod
    def from_env(cls):
        import boto3
        return cls(boto3.resource("dynamodb").Table(os.environ["STATE_TABLE"]))

    def load_final(self, run_id: str) -> dict[str, dict]:
        out, kwargs = {}, {"KeyConditionExpression": "run_id = :r",
                           "ExpressionAttributeValues": {":r": run_id}}
        while True:
            page = self.t.query(**kwargs)
            for row in page.get("Items", []):
                if row.get("decision") not in FINAL or "result_json" not in row:
                    continue
                try:
                    out[row["item_key"]] = json.loads(row["result_json"])
                except ValueError:
                    continue  # corrupt row: treat as not done and re-check it
            if "LastEvaluatedKey" not in page:
                return out
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def save_result(self, run_id: str, key: str, result: dict, now: int | None = None) -> None:
        self.t.update_item(
            Key={"run_id": run_id, "item_key": key},
            UpdateExpression="SET decision = :d, result_json = :j, updated_at = :t",
            ExpressionAttributeValues={":d": result["decision"], ":j": json.dumps(result),
                                       ":t": now or int(time.time())})

    def claim_notice(self, item, recall_numbers, now: int | None = None) -> bool:
        """True if this caller should send the notice; False if already sent or in flight."""
        now = now or int(time.time())
        try:
            self.t.update_item(
                Key={"run_id": NOTICES, "item_key": notice_key(item, recall_numbers)},
                UpdateExpression="SET notice_state = :p, claimed_at = :now",
                ConditionExpression=("attribute_not_exists(notice_state) OR notice_state = :f "
                                     "OR (notice_state = :p AND claimed_at < :stale)"),
                ExpressionAttributeValues={":p": "PENDING", ":f": "FAILED", ":now": now,
                                           ":stale": now - STALE_SECONDS})
            return True
        except Exception as e:
            if _code(e) == "ConditionalCheckFailedException":
                return False
            raise

    def finish_notice(self, item, recall_numbers, ok: bool, now: int | None = None) -> None:
        self.t.update_item(
            Key={"run_id": NOTICES, "item_key": notice_key(item, recall_numbers)},
            UpdateExpression="SET notice_state = :s, finished_at = :now",
            ExpressionAttributeValues={":s": "SENT" if ok else "FAILED", ":now": now or int(time.time())})
