from lerobot.teleoperators.so_leader import SO101LeaderConfig, SO101Leader
from lerobot.robots.so_follower import SO101FollowerConfig, SO101Follower

import os
import random
import time
import json
import urllib.request


# robot_config = SO101FollowerConfig(
#     port="/dev/tty.usbmodem58760431541",
#     id="Primo",
# )

teleop_config = SO101LeaderConfig(
    port="COM9",
    id="Primo",
)

# robot = SO101Follower(robot_config)
teleop_device = SO101Leader(teleop_config)
# robot.connect()
teleop_device.connect()
motorId = ['shoulder_pan', 'shoulder_lift', 'elbow_flex', 'wrist_flex', 'wrist_roll', 'gripper']

base_url = os.getenv("PUBLISH_URL", "http://127.0.0.1:8000").rstrip("/")
api_key = "uapi_34d18c8ae3._F1S7w6wWczztQQsLjTNCv69Jaac4HeIMc6jBUGexx4"

ingest_url = f"{base_url}/api/ingest"

while True:
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
