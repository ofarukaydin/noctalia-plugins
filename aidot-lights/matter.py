"""Matter control through a local python-matter-server (ws://127.0.0.1:5580/ws).

Preferred over the AiDot protocol when the server is running: ~0.1 s per
command instead of ~0.2 s, transitions can be 0 (no built-in fade), and
several controllers (Apple Home, this PC, the AiDot app) coexist.
Measured on these bulbs: ~9-11 updates/s per bulb with one command in flight.

Standard library only (a minimal WebSocket client), so the system Python
needs nothing extra.
"""

import base64
import colorsys
import json
import os
import socket
import struct
import subprocess
import threading
import time

URL_HOST, URL_PORT, URL_PATH = "127.0.0.1", 5580, "/ws"

ON_OFF, LEVEL, COLOR = 6, 8, 768
EXECUTE_IF_OFF = {"optionsMask": 1, "optionsOverride": 1}  # apply colour/level even while off


class MatterError(Exception):
    pass


# --- minimal WebSocket client --------------------------------------------

class WebSocket:
    def __init__(self, timeout: float = 3.0):
        self.sock = socket.create_connection((URL_HOST, URL_PORT), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {URL_PATH} HTTP/1.1\r\nHost: {URL_HOST}:{URL_PORT}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise MatterError("server closed the handshake")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise MatterError("server refused the WebSocket upgrade")
        self.buffer = head.split(b"\r\n\r\n", 1)[1]
        self.send_lock = threading.Lock()
        self.sock.settimeout(None)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 1 << 16:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self.send_lock:
            self.sock.sendall(header + mask + masked)

    def send(self, message: dict) -> None:
        self._send_frame(0x1, json.dumps(message).encode())

    def _read(self, n: int) -> bytes:
        while len(self.buffer) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise MatterError("connection to the Matter server closed")
            self.buffer += chunk
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data

    def receive(self) -> dict:
        message = b""
        while True:
            b1, b2 = self._read(2)
            opcode, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            payload = self._read(n)  # server frames are never masked
            if opcode == 0x8:
                raise MatterError("Matter server closed the connection")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode in (0x1, 0x2, 0x0):
                message += payload
                if b1 & 0x80:
                    return json.loads(message)


# --- client ---------------------------------------------------------------

class Client:
    """Request/response over one WebSocket; safe to use from several threads."""

    def __init__(self):
        self.ws = WebSocket()
        self.ws.receive()  # server info banner
        self.pending: dict[str, list] = {}
        self.lock = threading.Lock()
        self.counter = 0
        self.closed = False
        threading.Thread(target=self._reader, daemon=True).start()

    def close(self) -> None:
        self.closed = True
        self.ws.close()

    def _reader(self) -> None:
        try:
            while True:
                message = self.ws.receive()
                with self.lock:
                    slot = self.pending.pop(message.get("message_id"), None)
                if slot:
                    slot[1] = message
                    if slot[2]:
                        slot[2](message)
                    slot[0].set()
        except (MatterError, OSError, ValueError):
            self.closed = True
            with self.lock:
                for slot in self.pending.values():
                    slot[1] = {"error_code": -1, "details": "connection lost"}
                    slot[0].set()
                self.pending.clear()

    def request(self, command: str, args: dict | None = None, callback=None) -> list:
        if self.closed:
            raise MatterError("connection to the Matter server closed")
        with self.lock:
            self.counter += 1
            mid = str(self.counter)
            slot = [threading.Event(), None, callback]
            self.pending[mid] = slot
        self.ws.send({"message_id": mid, "command": command, "args": args or {}})
        return slot

    def call(self, command: str, args: dict | None = None, timeout: float = 10.0):
        slot = self.request(command, args)
        if not slot[0].wait(timeout):
            raise MatterError(f"{command} timed out")
        if "error_code" in slot[1]:
            raise MatterError(f"{command}: {slot[1].get('details') or slot[1]['error_code']}")
        return slot[1].get("result")

    def command(self, node: int, cluster: int, name: str, payload: dict | None = None, callback=None) -> list:
        return self.request("device_command", {"node_id": node, "endpoint_id": 1, "cluster_id": cluster,
                                               "command_name": name, "payload": payload or {}}, callback)


CONTAINER = "aidot-matter"
START_STAMP = os.path.expanduser("~/.cache/aidot-lights/matter-start")


def _port_open() -> bool:
    try:
        with socket.create_connection((URL_HOST, URL_PORT), timeout=0.3):
            return True
    except OSError:
        return False


def available() -> bool:
    """True when the Matter server answers, starting its container if needed.

    docker.service isn't enabled at boot here (only docker.socket), so the
    container's restart policy never fires after a reboot. `docker start`
    activates the daemon through the socket. Tried at most once a minute so a
    missing container doesn't slow down every status poll.
    """
    if _port_open():
        return True
    try:
        if time.time() - os.path.getmtime(START_STAMP) < 60:
            return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(START_STAMP), exist_ok=True)
    with open(START_STAMP, "w"):
        pass
    try:
        started = subprocess.run(["docker", "start", CONTAINER], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
    deadline = time.time() + 20  # the server needs a few seconds to load its fabric
    while started and time.time() < deadline:
        if _port_open():
            time.sleep(2)  # let it reconnect to the bulbs before the first command
            return True
        time.sleep(0.5)
    return False


# --- light model ----------------------------------------------------------

def device_id(node: int) -> str:
    return f"matter-{node}"


def describe(node: dict) -> dict:
    """Same shape as aidot.describe() so the plugin doesn't care which protocol is used."""
    a = node.get("attributes", {})
    n = node["node_id"]
    ct_min, ct_max = a.get("1/768/16395") or 153, a.get("1/768/16396") or 555
    out = {
        "id": device_id(n), "name": a.get("0/40/5") or f"Matter light {n}", "model": a.get("0/40/3"),
        "ip": None, "protocol": "matter", "online": bool(node.get("available")),
        "caps": {"rgbw": True, "cct": True, "cctMin": round(1e6 / ct_max, -2), "cctMax": round(1e6 / ct_min, -2)},
    }
    if not out["online"]:
        out["error"] = "not reachable over Matter"
        return out
    out["on"] = bool(a.get("1/6/0"))
    out["brightness"] = max(1, round((a.get("1/8/0") or 0) * 100 / 254))
    if a.get("1/768/7"):
        out["cct"] = round(1e6 / a["1/768/7"])
    hue, sat = a.get("1/768/0") or 0, a.get("1/768/1") or 0
    r, g, b = (round(c * 255) for c in colorsys.hsv_to_rgb(hue / 254, sat / 254, 1.0))
    out["rgb"] = f"#{r:02x}{g:02x}{b:02x}"
    out["mode"] = "white" if a.get("1/768/8") == 2 else "color"
    return out


def snapshot(node: dict) -> dict:
    """The attributes needed to put a light back exactly as it was."""
    a = node.get("attributes", {})
    return {"on": bool(a.get("1/6/0")), "level": a.get("1/8/0") or 254, "mode": a.get("1/768/8"),
            "hue": a.get("1/768/0") or 0, "sat": a.get("1/768/1") or 0,
            "x": a.get("1/768/3"), "y": a.get("1/768/4"), "ct": a.get("1/768/7") or 370}


def hue_sat(rgb_hex: str) -> tuple[int, int]:
    r, g, b = (int(rgb_hex.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, s, _ = colorsys.rgb_to_hsv(r, g, b)
    return round(h * 254), round(s * 254)


def color_commands(hue: int | None = None, sat: int | None = None, ct_mireds: int | None = None,
                   level: int | None = None, on: bool | None = None, transition: int = 0) -> list[tuple]:
    """(cluster, command, payload) tuples; colour and level before power so off lights keep them."""
    cmds = []
    if ct_mireds is not None:
        cmds.append((COLOR, "MoveToColorTemperature",
                     {"colorTemperatureMireds": ct_mireds, "transitionTime": transition, **EXECUTE_IF_OFF}))
    if hue is not None:
        cmds.append((COLOR, "MoveToHueAndSaturation",
                     {"hue": hue, "saturation": sat if sat is not None else 254, "transitionTime": transition, **EXECUTE_IF_OFF}))
    if level is not None:
        cmds.append((LEVEL, "MoveToLevel", {"level": max(1, min(254, level)), "transitionTime": transition, **EXECUTE_IF_OFF}))
    if on is not None:
        cmds.append((ON_OFF, "On" if on else "Off", {}))
    return cmds


def restore_commands(saved: dict) -> list[tuple]:
    cmds = []
    if saved.get("mode") == 2:
        cmds += color_commands(ct_mireds=saved["ct"])
    elif saved.get("mode") == 1 and saved.get("x") is not None:
        cmds.append((COLOR, "MoveToColor", {"colorX": saved["x"], "colorY": saved["y"], "transitionTime": 0, **EXECUTE_IF_OFF}))
    else:
        cmds += color_commands(hue=saved["hue"], sat=saved["sat"])
    cmds += color_commands(level=saved["level"], on=saved["on"])
    return cmds


class MatterLights:
    """Status and changes for the CLI, mirroring aidot.Lights."""

    def __init__(self):
        self.client = Client()
        self.nodes = {n["node_id"]: n for n in self.client.call("get_nodes") or []}
        if not self.nodes:
            raise MatterError("no lights paired with the Matter server")

    def select(self, targets: list[str]) -> list[dict]:
        if "all" in targets:
            return list(self.nodes.values())
        hits = []
        for target in targets:
            match = [n for n in self.nodes.values()
                     if target in (device_id(n["node_id"]), str(n["node_id"]))
                     or (n["attributes"].get("0/40/5") or "").lower() == target.lower()]
            if not match:
                raise MatterError(f"No light matches {target!r}")
            hits += [n for n in match if n not in hits]
        return hits

    def run(self, nodes: list[dict], cmds_for) -> None:
        slots = []
        for n in nodes:
            for cluster, name, payload in cmds_for(n):
                slots.append(self.client.command(n["node_id"], cluster, name, payload))
        for slot in slots:
            slot[0].wait(5)

    def refresh(self, nodes: list[dict]) -> list[dict]:
        return [describe(self.client.call("get_node", {"node_id": n["node_id"]})) for n in nodes]

    def apply(self, nodes: list[dict], attr: dict) -> list[dict]:
        """Apply an aidot-style attribute dict (OnOff / Dimming / CCT / RGBW)."""
        kwargs = {"transition": 3}  # a short 0.3 s ease for manual changes
        if "CCT" in attr:
            kwargs["ct_mireds"] = max(153, min(555, round(1e6 / attr["CCT"])))
        if "RGBW" in attr:
            value = attr["RGBW"] & 0xFFFFFFFF
            kwargs["hue"], kwargs["sat"] = hue_sat(f"#{value >> 8:06x}")
        if "Dimming" in attr:
            kwargs["level"] = round(attr["Dimming"] * 254 / 100)
        if "OnOff" in attr:
            kwargs["on"] = bool(attr["OnOff"])
        self.run(nodes, lambda n: color_commands(**kwargs))
        # The server's cached attributes lag the change by up to a second, so
        # report what was just set on top of the last known state.
        out = []
        for d in self.refresh(nodes):
            if "on" in kwargs:
                d["on"] = kwargs["on"]
            if "level" in kwargs:
                d["brightness"] = attr["Dimming"]
            if "ct_mireds" in kwargs:
                d["cct"], d["mode"] = attr["CCT"], "white"
            if "hue" in kwargs:
                d["rgb"], d["mode"] = f"#{(attr['RGBW'] & 0xFFFFFFFF) >> 8:06x}", "color"
            out.append(d)
        return out
