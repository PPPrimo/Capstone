from lerobot.teleoperators.so_leader import SO101LeaderConfig, SO101Leader
from lerobot.robots.so_follower import SO101FollowerConfig, SO101Follower

import argparse
import os
import random
import time
import json
import threading
import requests

import websockets
import asyncio

motorId = ['shoulder_pan', 'shoulder_lift', 'elbow_flex', 'wrist_flex', 'wrist_roll', 'gripper']
base_url = os.getenv("PUBLISH_URL", "https://primowang.com/").rstrip("/")
base_url = os.getenv("PUBLISH_URL", "http://127.0.0.1:8000").rstrip("/")

api_key = "uapi_9ef5e39e2a.2BGWx7xyUsg2oNM0lwonlIm82moxUDWH_I7llgwZgv8"
api_key = "uapi_8139a97701.5VLxX7OxnZ2thJNQM26tnNQJahKWsGane9WMQW_OIhQ"

ingest_url = f"{base_url}/api/ingest"
latest_url = f"{base_url}/api/latest"
ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://") + f"/api/ws?api_key={api_key}"

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
    try:
        resp = requests.post(
            ingest_url,
            json=payload,
            headers={
                "X-API-Key": api_key,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/133.0.0.0",
            },
            timeout=5,
        )
        print(resp.status_code, resp.text)
    except Exception as exc:
        print("ERROR", exc)

def FollowerRecieve():
    global latest_command
    while True:
        try:
            resp = requests.get(
                latest_url,
                headers={
                    "X-API-Key": api_key,
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/133.0.0.0",
                },
                timeout=5,
            )
            data = resp.json()
            with command_lock:
                latest_command = data["payload"]
        except Exception as e:
            print("No data, retrying...", e)
        time.sleep(0.1)

def FollowerStream():
    """Connect to the server via WebSocket and receive push updates."""
    global latest_command

    async def FollowerLoop():
        
        global latest_command
        while True:
            try:
                async with websockets.connect(ws_url) as ws:
                    print("WebSocket connected")
                    async for raw in ws:
                        try:
                            rsp = json.loads(raw)
                            if rsp.get("ping"):
                                continue
                            payload = rsp.get("payload")
                            if payload:
                                with command_lock:
                                    latest_command = payload
                        except json.JSONDecodeError:
                            pass
            except Exception as e:
                print("WebSocket disconnected, retrying...", e)
                await asyncio.sleep(1)

    asyncio.run(FollowerLoop())

def FollowerAction():
    global latest_command
    while True:
        print(latest_command)
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
    
    
