from __future__ import annotations

import os
import random
import time
import json
import urllib.request


def main() -> None:
    base_url = os.getenv("PUBLISH_URL", "http://127.0.0.1:8000").rstrip("/")
    api_key = "uapi_34d18c8ae3._F1S7w6wWczztQQsLjTNCv69Jaac4HeIMc6jBUGexx4"

    ingest_url = f"{base_url}/api/ingest"

    while True:
        payload = {
            "value": random.random(),
            "counter": int(time.time()),
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            ingest_url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                print(resp.status, body)
        except Exception as exc:
            print("ERROR", exc)
        time.sleep(0.01)


if __name__ == "__main__":
    main()
