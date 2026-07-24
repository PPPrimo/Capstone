from lerobot.teleoperators.so_leader import SO101LeaderConfig, SO101Leader
from lerobot.robots.so_follower import SO101FollowerConfig, SO101Follower

from WebRepo.server.plot_logger import log_and_plot, flush_plot, set_layout, set_role, set_realtime_plot, plot_json

import argparse
import os
import time
import json
import threading

import websockets
import asyncio

from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer




"""Global Variables"""
motorId = ['shoulder_pan', 'shoulder_lift', 'elbow_flex', 'wrist_flex', 'wrist_roll', 'gripper']
teleop_device = None

base_url = os.getenv("PUBLISH_URL", "https://primowang.com/").rstrip("/")
api_key = "uapi_dc1ab5d444.bxAGSutbbRAuAM8gPw4Hj5AiCPBEdfKbxoczrki0ak4"
#base_url = os.getenv("PUBLISH_URL", "http://127.0.0.1:8000").rstrip("/")
#api_key = "uapi_8139a97701.5VLxX7OxnZ2thJNQM26tnNQJahKWsGane9WMQW_OIhQ"

latest_command = None
command_lock = threading.Lock()
payload = None
ExcutionPeriod = 0.01
_post_payload = None
_post_lock = threading.Lock()
"""Global Variables End"""

def get_all_states(device) -> dict[str, dict]:
    """Read position, velocity, and current for all motors.
    Returns: {motor_name: {"position": val, "velocity": val, "current": val}}
    """
    positions = device.get_position()
    velocities = device.get_velocity()
    currents = device.get_current()
    return {
        "timestamp": time.time(),
        "motors": {
            motor: {
                "position": positions[motor],
                "velocity": velocities[motor],
                "current": currents[motor],
            }
            for motor in positions
        }
    }

# ── WebRTC publisher (low-latency path) ──────────────────────────────────────

_STUN = RTCConfiguration(
    iceServers=[RTCIceServer(urls=["stun:stun.l.google.com:19302"])]
)
_webrtc_signal_url = (
    base_url.replace("http://", "ws://").replace("https://", "wss://")
    + f"/api/webrtc/signal?role=publisher&api_key={api_key}"
)


async def _webrtc_publisher_async():
    """Connect to the signaling server, create one RTCPeerConnection per browser
    subscriber, and stream motor telemetry over an unordered DataChannel
    """
    peers: dict[str, dict] = {}  # sub_id -> {"pc": RTCPeerConnection, "channel": channel}

    async def _send_loop(channel):
        while channel.readyState == "open":
            with _post_lock:
                data = _post_payload
            if data is not None:
                try:
                    channel.send(json.dumps(data))
                except Exception:
                    break
            await asyncio.sleep(0.01)  # ~100 Hz

    async def _setup_peer(ws, sub_id: str):
        pc = RTCPeerConnection(configuration=_STUN)
        # ordered=False + maxRetransmits=0 → fire-and-forget like UDP
        channel = pc.createDataChannel("telemetry", ordered=False, maxRetransmits=0)
        peers[sub_id] = {"pc": pc, "channel": channel}

        @channel.on("open")
        def on_open():
            print(f"[WebRTC] DataChannel open → {sub_id[:8]}")
            asyncio.ensure_future(_send_loop(channel))

        @channel.on("close")
        def on_close():
            print(f"[WebRTC] DataChannel closed ← {sub_id[:8]}")
            peers.pop(sub_id, None)

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        # Wait for all ICE candidates to be embedded in the SDP
        ice_done = asyncio.Event()
        @pc.on("icegatheringstatechange")
        def on_ice():
            if pc.iceGatheringState == "complete":
                ice_done.set()
        if pc.iceGatheringState == "complete":
            ice_done.set()
        try:
            await asyncio.wait_for(ice_done.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

        await ws.send(json.dumps({
            "type":    "offer",
            "sdp":     pc.localDescription.sdp,
            "sdpType": pc.localDescription.type,
            "target":  sub_id,
        }))

    while True:
        try:
            async with websockets.connect(_webrtc_signal_url) as ws:
                print("[WebRTC] Signaling connected")
                async for raw in ws:
                    msg = json.loads(raw)
                    t = msg.get("type")

                    if t == "subscriber_ready":
                        sub_id = msg["sub_id"]
                        print(f"[WebRTC] New subscriber: {sub_id[:8]}")
                        asyncio.ensure_future(_setup_peer(ws, sub_id))

                    elif t == "answer":
                        sub_id = msg.get("from")
                        peer = peers.get(sub_id)
                        if peer:
                            await peer["pc"].setRemoteDescription(
                                RTCSessionDescription(
                                    sdp=msg["sdp"], type=msg["sdpType"]
                                )
                            )
                            print(f"[WebRTC] Peer connected: {sub_id[:8]}")

                    elif t == "subscriber_left":
                        sub_id = msg.get("sub_id")
                        peer = peers.pop(sub_id, None)
                        if peer:
                            await peer["pc"].close()
                            print(f"[WebRTC] Subscriber left: {sub_id[:8]}")

        except Exception as exc:
            print(f"[WebRTC] Signaling error: {exc}, retrying...")
            await asyncio.sleep(2)


def webrtc_publisher_thread():
    asyncio.run(_webrtc_publisher_async())

def LeaderSend(teleop_device):
    global payload, _post_payload
    payload = get_all_states(teleop_device)
    with _post_lock:
        _post_payload = payload

async def _webrtc_follower_async():
    """Connect to the signaling server as a subscriber (using API key).
    Receives a WebRTC offer from the Leader, completes the handshake, and
    updates latest_command via the unreliable/unordered DataChannel.
    """
    _signal_url = (
        base_url.replace("http://", "ws://").replace("https://", "wss://")
        + f"/api/webrtc/signal?role=subscriber&api_key={api_key}"
    )
    while True:
        pc = None
        try:
            async with websockets.connect(_signal_url) as ws:
                print("[WebRTC Follower] Signaling connected")
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "offer":
                        continue

                    if pc is not None:
                        await pc.close()

                    pc = RTCPeerConnection(configuration=_STUN)

                    @pc.on("datachannel")
                    def on_datachannel(channel):
                        @channel.on("message")
                        def on_message(data):
                            global latest_command
                            try:
                                with command_lock:
                                    latest_command = json.loads(data)
                            except Exception:
                                pass

                    await pc.setRemoteDescription(
                        RTCSessionDescription(sdp=msg["sdp"], type=msg["sdpType"])
                    )
                    answer = await pc.createAnswer()
                    await pc.setLocalDescription(answer)

                    # Trickle-free: embed all ICE candidates before sending the answer
                    ice_done = asyncio.Event()
                    _pc = pc  # capture for the callback closure
                    @pc.on("icegatheringstatechange")
                    def on_ice():
                        if _pc.iceGatheringState == "complete":
                            ice_done.set()
                    if pc.iceGatheringState == "complete":
                        ice_done.set()
                    try:
                        await asyncio.wait_for(ice_done.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        pass

                    await ws.send(json.dumps({
                        "type":    "answer",
                        "sdp":     pc.localDescription.sdp,
                        "sdpType": pc.localDescription.type,
                    }))
                    print("[WebRTC Follower] Handshake complete — DataChannel incoming")

        except Exception as exc:
            print(f"[WebRTC Follower] Error: {exc}, retrying...")
            if pc is not None:
                try:
                    await pc.close()
                except Exception:
                    pass
            await asyncio.sleep(2)


def webrtc_follower_thread():
    asyncio.run(_webrtc_follower_async())

def sync_states_to_follower(device, states: dict):
    """Convert a get_all_states() payload received over WebRTC into a
    RobotAction dict and send it to the follower arm."""
    if states is None or "motors" not in states:
        return
    action = {f"{motor}.pos": data["position"] for motor, data in states["motors"].items()}
    device.send_action(action)

def FollowerRecieve(teleop_device):
    with command_lock:
        states = latest_command
    if states is not None:
        sync_states_to_follower(teleop_device, states)

def main():
    global ExcutionPeriod
    global teleop_device

    ### Input
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--type",
        type=str,
        required=True,
        help="Mode type --type(e.g., Follower or Leader)"
    )

    parser.add_argument(
        "--COM",
        type=str,
        required=True,
        help="COM Port --COM(e.g COM2...)"
    )

    args = parser.parse_args()


    if args.type == "Follower":
        print("entered")
        teleop_config = SO101FollowerConfig(
            port=args.COM,
            id="PrimoFollower",
        )
        teleop_device = SO101Follower(teleop_config)
        teleop_device.connect()

        def _recieve_loop():
            while True:
                try:
                    FollowerRecieve(teleop_device)
                except Exception as exc:
                    print("FollowerRecieve error:", exc)
                time.sleep(ExcutionPeriod)  # 10 ms read interval
        send_thread = threading.Thread(target=_recieve_loop, daemon=True)
        send_thread.start()
        recieve_thread = threading.Thread(target=webrtc_follower_thread, daemon=True)
        recieve_thread.start()

        ### Ploting feature
        set_role("follower")
        set_realtime_plot(False)        
        set_layout(rows=6, cols=2)  # 12 series (6 motors × pos+current) in 2 rows of 6
        while True:
            with command_lock:
                states = latest_command
            if states is not None:
                t = time.time()
                for m in motorId:
                    log_and_plot(t, states["motors"][m]["position"], "time", "position", f"{m}_position")
                    log_and_plot(t, states["motors"][m]["velocity"], "time", "velocity", f"{m}_velocity")
                    #log_and_plot(t, states["motors"][m]["current"],  "time", "current",  f"{m}_current")
                flush_plot()
            time.sleep(ExcutionPeriod)

    elif args.type == "Leader":
        teleop_config = SO101LeaderConfig(
            port=args.COM,
            id="PrimoLeader",
        )
        teleop_device = SO101Leader(teleop_config)
        teleop_device.connect()
        def _send_loop():
            while True:
                try:
                    LeaderSend(teleop_device)
                except Exception as exc:
                    print("LeaderSend error:", exc)
                time.sleep(ExcutionPeriod)  # 10 ms read interval

        send_thread = threading.Thread(target=_send_loop, daemon=True)
        send_thread.start()
        webrtc_thread = threading.Thread(target=webrtc_publisher_thread, daemon=True)
        webrtc_thread.start()

        ### Ploting feature
        set_role("leader")
        set_realtime_plot(False)        
        set_layout(rows=6, cols=2)  # 12 series (6 motors × pos+current) in 2 rows of 6
        while True:
            if payload is not None:
                t = time.time()
                for m in motorId:
                    log_and_plot(t, payload["motors"][m]["position"], "time", "position", f"{m}_position")
                    log_and_plot(t, payload["motors"][m]["velocity"], "time", "velocity", f"{m}_velocity")
                    #log_and_plot(t, payload[m]["current"],  "time", "current",  f"{m}_current")
                flush_plot()
            time.sleep(ExcutionPeriod)


if __name__ == "__main__":
    main()
    
    
