from __future__ import annotations

import json
import os
import time
import urllib.request


def main() -> None:
    base_url = os.getenv("PUBLISH_URL", "http://127.0.0.1:8000").rstrip("/")
    api_key = "uapi_34d18c8ae3._F1S7w6wWczztQQsLjTNCv69Jaac4HeIMc6jBUGexx4"

    interval_s = 0.2
    latest_url = f"{base_url}/api/latest"

    while True:
        req = urllib.request.Request(
            latest_url,
            method="GET",
            headers={
                "Accept": "application/json",
                "X-API-Key": api_key,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                print(body)
                try:
                    obj = json.loads(body)
                    print(json.dumps(obj, indent=2, sort_keys=True))
                except json.JSONDecodeError:
                    print(body)
        except Exception as exc:
            print("ERROR", exc)

        time.sleep(interval_s)


if __name__ == "__main__":
    main()
