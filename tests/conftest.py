import pytest

from recall_agent import handler

SENDER, COORD = "recall-agent@example.com", "coordinator@example.com"


class FakeSES:
    def __init__(self):
        self.sent, self.fail_to = [], set()

    def send_email(self, **kw):
        to = kw["Destination"]["ToAddresses"]
        if any(a in self.fail_to for a in to):
            raise RuntimeError("MessageRejected: address not verified")
        simple = kw["Content"]["Simple"]
        self.sent.append({"from": kw["FromEmailAddress"], "to": to,
                          "cc": kw["Destination"].get("CcAddresses", []),
                          "subject": simple["Subject"]["Data"], "body": simple["Body"]["Text"]["Data"]})


@pytest.fixture(autouse=True)
def ses(monkeypatch):
    fake = FakeSES()
    monkeypatch.setattr(handler, "make_ses", lambda: fake)
    monkeypatch.setenv("SENDER_EMAIL", SENDER)
    monkeypatch.setenv("COORDINATOR_EMAIL", COORD)
    return fake
