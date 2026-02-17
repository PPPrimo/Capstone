from lerobot.teleoperators.so_leader import SO101LeaderConfig, SO101Leader
from lerobot.robots.so_follower import SO101FollowerConfig, SO101Follower

import argparse
import os
import random
import time
import json
import urllib.request

motorId = ['shoulder_pan', 'shoulder_lift', 'elbow_flex', 'wrist_flex', 'wrist_roll', 'gripper']
base_url = os.getenv("PUBLISH_URL", "http://127.0.0.1:8000").rstrip("/")
api_key = "uapi_34d18c8ae3._F1S7w6wWczztQQsLjTNCv69Jaac4HeIMc6jBUGexx4"

ingest_url = f"{base_url}/api/ingest"
latest_url = f"{base_url}/api/latest"

# robot_config = SO101FollowerConfig(
#     port="/dev/tty.usbmodem58760431541",
#     id="Primo",
# )
# robot = SO101Follower(robot_config)
# robot.connect()

def LeaderSend(teleop_device):
    positions = teleop_device.get_position()
    velocity = teleop_device.get_velocity()
    current = teleop_device.get_current()

    payload = {
        m: {"position": positions[m], "velocity": velocity[m], "current": current[m]}
        for m in motorId
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

def FollowerRecieve():
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
            try:
                obj = json.loads(body)
                LeaderStatus = obj["payload"]
                print(LeaderStatus)
                #print(json.dumps(obj, indent=2, sort_keys=True))
            except json.JSONDecodeError:
                LeaderStatus = None
                print(body)
    except Exception as exc:
        print("ERROR", exc)
    return LeaderStatus

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--type",
        type=str,
        required=True,
        help="Mode type (e.g., Follower or Leader)"
    )
    args = parser.parse_args()
    if args.type == "Follower":
        while True:
            FollowerRecieve()
            time.sleep(0.1)

    elif args.type == "Leader":
        teleop_config = SO101LeaderConfig(
            port="COM9",
            id="Primo",
        )
        teleop_device = SO101Leader(teleop_config)
        teleop_device.connect()
        while True:
            LeaderSend(teleop_device)
            time.sleep(0.1)


if __name__ == "__main__":
    main()
    
    
