"""A TestClient that adds a fresh `Idempotency-Key` to creation requests that carry none.

The three creation routes REQUIRE the header (428 without it; issue #8, ADR 0014).
Tests that are about something else post as before and get a unique key per call
(so two identical posts stay two logical submissions). Tests that are about
idempotency pass their own header, or set `client.auto_key = False` to exercise
the missing-key refusal.
"""
import uuid

from fastapi.testclient import TestClient

CREATION_PATHS = ("/laboratory/orders/", "/laboratory/orders/unified", "/laboratory/samples/")


class KeyedClient(TestClient):
    auto_key = True

    def request(self, method, url, **kwargs):
        path = str(url).split("?", 1)[0]
        if self.auto_key and method.upper() == "POST" and path.endswith(CREATION_PATHS):
            headers = dict(kwargs.get("headers") or {})
            if not any(k.lower() == "idempotency-key" for k in headers):
                headers["Idempotency-Key"] = f"test-{uuid.uuid4().hex}"
            kwargs["headers"] = headers
        return super().request(method, url, **kwargs)
