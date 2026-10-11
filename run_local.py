"""Run the agent over a few sample items against real Bedrock + openFDA.

    export BEDROCK_MODEL_ID=<model or inference-profile id enabled in your account>
    export AWS_REGION=us-east-1
    python run_local.py
    FDA_FAULT=rate_limit FDA_FAULT_TERM="Sunrise" python run_local.py   # failure demo
"""
import json

from recall_agent.agent import make_client, run_item
from recall_agent.matcher import Item

ITEMS = [
    Item("Sunrise Foods Inc", "supplier", lots=["LOT 7710B"]),
    Item("tahini", "ingredient", lots=["T-2209-14"]),
    Item("Acme Spice Traders", "supplier"),
]

if __name__ == "__main__":
    client = make_client()
    for item in ITEMS:
        print(json.dumps(run_item(client, item), indent=2))
