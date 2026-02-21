from lerobot.teleoperators.so_leader import SO101LeaderConfig, SO101Leader
from lerobot.robots.so_follower import SO101FollowerConfig, SO101Follower

import argparse
import os
import random
import time
import json
import threading
import requests

import urllib.request
import sseclient

motorId = ['shoulder_pan', 'shoulder_lift', 'elbow_flex', 'wrist_flex', 'wrist_roll', 'gripper']
base_url = os.getenv("PUBLISH_URL", "http://127.0.0.1:8000").rstrip("/")
api_key = "uapi_45a7a7f90d.5vcTTRVFFnCZ27ue6e6ia1B1plYDShGWl6ObNZfcUUQ"

ingest_url = f"{base_url}/api/ingest"
latest_url = f"{base_url}/api/latest"
stream_url = f"{base_url}/api/stream"

latest_command = None
command_lock = threading.Lock()

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

def FollowerStream():
    global latest_command
    while True:
        try:
            response = requests.get(
                stream_url,
                headers={"X-API-Key": api_key},
                stream=True,
                timeout=30,
            )

            client = sseclient.SSEClient(response)

            for event in client.events():
                if not event.data:
                    continue

                obj = json.loads(event.data)
                LeaderStatus = obj["payload"]
                cmd = LeaderStatus
                with command_lock:
                    latest_command = cmd
                
        except Exception as e:
            print("Stream disconnected, retrying...", e)
            time.sleep(2)

def FollowerAction():
    global latest_command
    while True:
        time.sleep(0.1)
        

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
        recieve_thread = threading.Thread(target=FollowerStream, daemon=True)
        recieve_thread.start()
        FollowerAction()

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
    
    
