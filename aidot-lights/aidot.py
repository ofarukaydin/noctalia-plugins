#!/usr/bin/env python3
"""LAN control for AiDot (Linkind / OREiN) Wi-Fi lights.

The cloud is used once, by `login`, to fetch each light's local AES key and
password. Everything else talks to the lights directly on TCP 10000.
Protocol reference: github.com/AiDot-Development-Team/python-AiDot.
"""

import argparse
import base64
import getpass
import hashlib
import json
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

APP_ID = "1383974540041977857"
API_URL = "https://prod-{region}-api.arnoo.com/v35"
PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCtQAnPCi8ksPnS1Du6z96PsKfN
p2Gp/f/bHwlrAdplbX3p7/TnGpnbJGkLq8uRxf6cw+vOthTsZjkPCF7CatRvRnTj
c9fcy7yE0oXa5TloYyXD6GkxgftBbN/movkJJGQCc7gFavuYoAdTRBOyQoXBtm0m
kXMSjXOldI/290b9BQIDAQAB
-----END PUBLIC KEY-----"""
DISCOVERY_KEY = b"T54uednca587".ljust(32, b"\0")
DEVICE_PORT = 10000
DISCOVERY_PORT = 6666
MAGIC = 0x1EED

DEFAULT_CREDENTIALS = "~/.config/noctalia/secrets/aidot.json"
CACHE_FILE = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser() / "aidot-lights" / "ips.json"


class LightError(Exception):
    pass


# --- crypto ---------------------------------------------------------------

def aes_encrypt(data: bytes, key: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(padder.update(data) + padder.finalize()) + enc.finalize()


def aes_decrypt(data: bytes, key: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(dec.update(data) + dec.finalize()) + unpadder.finalize()


def device_key(key_string: str) -> bytes:
    return key_string.encode()[:16].ljust(16, b"\0")


# --- files ----------------------------------------------------------------

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_private_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_credentials(path: str) -> dict:
    creds = load_json(Path(path).expanduser(), None)
    if not creds or not creds.get("devices"):
        raise LightError(f"No AiDot credentials at {path}. Run: aidot.py login")
    return creds


# --- cloud (login only) ---------------------------------------------------

def cloud_request(base: str, path: str, token: str | None = None, body: dict | None = None):
    headers = {"appId": APP_ID, "terminal": "app", "Content-Type": "application/json"}
    if token:
        headers["Token"] = token
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")
        raise LightError(f"AiDot cloud {path} failed ({err.code}): {detail}") from None


def cmd_login(args) -> None:
    email = args.email or input("AiDot email: ").strip()
    password = getpass.getpass("AiDot password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
    base = API_URL.format(region=args.region)
    public_key = serialization.load_pem_public_key(PUBLIC_KEY_PEM)
    encrypted = base64.b64encode(public_key.encrypt(password.encode(), rsa_padding.PKCS1v15())).decode()
    terminal_id = hashlib.md5(format(uuid.getnode(), "x").encode()).hexdigest()
    login = cloud_request(base, "/users/loginWithFreeVerification", body={
        "countryKey": f"region:{args.country}",
        "username": email,
        "password": encrypted,
        "terminalId": terminal_id,
        "webVersion": "0.5.0",
        "area": "Asia/Shanghai",
        "UTC": "UTC+8",
    })
    user_id, token = login.get("id"), login.get("accessToken")
    if not user_id or not token:
        raise LightError(f"Login rejected: {login}")

    devices = []
    for house in cloud_request(base, "/houses", token):
        if house.get("isOwner") is False:
            continue
        devices.extend(cloud_request(base, f"/devices?houseId={house['id']}", token) or [])
    products = {}
    if devices:
        ids = ",".join(sorted({d["productId"] for d in devices}))
        products = {p["id"]: p for p in cloud_request(base, f"/products/{ids}", token)}

    saved = []
    for d in devices:
        key = (d.get("aesKey") or [None])[0]
        if d.get("type") != "light" or not key:
            continue
        caps = {"rgbw": False, "cct": False, "cctMin": 2700, "cctMax": 6500}
        for module in products.get(d["productId"], {}).get("serviceModules", []):
            if module.get("identity") == "control.light.rgbw":
                caps["rgbw"] = True
            elif module.get("identity") == "control.light.cct":
                caps["cct"] = True
                prop = (module.get("properties") or [{}])[0]
                caps["cctMin"] = int(prop.get("minValue", caps["cctMin"]))
                caps["cctMax"] = int(prop.get("maxValue", caps["cctMax"]))
        saved.append({
            "id": d["id"],
            "name": d.get("name") or d["id"],
            "model": d.get("modelId"),
            "mac": d.get("mac"),
            "aesKey": key,
            "password": d.get("password"),
            "simpleVersion": d.get("simpleVersion"),
            "ip": (d.get("properties") or {}).get("ipAddress"),
            "caps": caps,
        })

    path = Path(args.credentials).expanduser()
    write_private_json(path, {"userId": user_id, "region": args.region, "devices": saved})
    print(f"Saved {len(saved)} light(s) to {path}:")
    for d in saved:
        print(f"  {d['name']}  ({d['model']}, {d['ip'] or 'ip unknown'})")


# --- discovery ------------------------------------------------------------

def discover(user_id: str, extra_targets=(), wait: float = 2.0) -> dict[str, str]:
    now = int(time.time() * 1000)
    request = aes_encrypt(json.dumps({
        "protocolVer": "2.0.0", "service": "device", "method": "devDiscoveryReq",
        "seq": str(now + 1)[-9:], "srcAddr": f"0.{user_id}", "tst": now,
        "payload": {"extends": {}, "localCtrFlag": 1, "timestamp": str(now)},
    }).encode(), DISCOVERY_KEY)
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", 0))
        for target in ("255.255.255.255", *extra_targets):
            try:
                sock.sendto(request, (target, DISCOVERY_PORT))
            except OSError:
                pass
        deadline = time.monotonic() + wait
        while (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(4096)
                payload = json.loads(aes_decrypt(data, DISCOVERY_KEY)).get("payload") or {}
            except socket.timeout:
                break
            except (ValueError, OSError):
                continue
            if payload.get("devId"):
                found[payload["devId"]] = addr[0]
    return found


# --- device session -------------------------------------------------------

class Session:
    def __init__(self, device: dict, user_id: str, ip: str, timeout: float = 2.5):
        self.device, self.user_id = device, user_id
        self.key = device_key(device["aesKey"])
        self.sock = socket.create_connection((ip, DEVICE_PORT), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.seq = 0
        self.asc = self._login()

    def close(self) -> None:
        self.sock.close()

    def _send(self, message: dict, msgtype: int = 1) -> None:
        body = aes_encrypt(json.dumps(message).encode(), self.key)
        self.sock.sendall(struct.pack(">HhI", MAGIC, msgtype, len(body)) + body)

    def _read_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise LightError("connection closed by light")
            buf += chunk
        return buf

    def _recv(self) -> dict:
        _, _, size = struct.unpack(">HHI", self._read_exact(8))
        return json.loads(aes_decrypt(self._read_exact(size), self.key))

    def _login(self) -> int:
        now = int(time.time() * 1000)
        self._send({
            "service": "device", "method": "loginReq", "seq": str(now)[-9:],
            "srcAddr": self.user_id, "deviceId": self.device["id"],
            "payload": {
                "userId": self.user_id, "password": self.device.get("password"),
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S.000000"), "ascNumber": 1,
            },
        })
        reply = self._recv()
        code = (reply.get("ack") or {}).get("code")
        if code != 200:
            raise LightError(f"light refused login (code {code})")
        return int((reply.get("payload") or {}).get("ascNumber") or 0) + 1

    def _action(self, method: str, attr) -> None:
        self.seq += 1
        payload = {"devId": "", "parentId": "", "userId": "", "password": "",
                   "attr": attr, "channel": "tcp", "ascNumber": self.asc}
        message = {"method": method, "service": "device", "clientId": "",
                   "srcAddr": f"0.{self.user_id}", "seq": f"nl{self.seq:07d}", "payload": payload,
                   "tst": int(time.time() * 1000), "deviceId": self.device["id"]}
        if self.device.get("simpleVersion") is not None:
            message["clientId"] = f"ha-{self.user_id}"
            payload.update(devId=self.device["id"], parentId=self.device["id"],
                           userId=self.user_id, password=self.device.get("password"))
        self._send(message)

    def read_attrs(self, wait: float, ack_method: str | None = None) -> dict:
        """Read replies until one carries attributes, or `wait` seconds pass.

        A set is acknowledged by `setDevAttrResp` without attributes; the state
        report follows only if something changed, so after the ack we just give
        it a moment to arrive.
        """
        deadline = time.monotonic() + wait
        while (remaining := deadline - time.monotonic()) > 0:
            self.sock.settimeout(remaining)
            try:
                reply = self._recv()
            except (socket.timeout, TimeoutError):
                break
            payload = reply.get("payload") or {}
            if payload.get("ascNumber"):
                self.asc = int(payload["ascNumber"])
            if payload.get("attr"):
                return payload["attr"]
            if ack_method and reply.get("method") == ack_method:
                code = (reply.get("ack") or {}).get("code")
                if code != 200:
                    raise LightError(f"light rejected the change (code {code})")
                deadline = min(deadline, time.monotonic() + 0.15)
        return {}

    def get(self) -> dict:
        self._action("getDevAttrReq", [])
        attrs = self.read_attrs(2.0)
        if not attrs:
            raise LightError("light did not report its state")
        return attrs

    def set(self, attr: dict) -> dict:
        self._action("setDevAttrReq", attr)
        return self.read_attrs(1.5, ack_method="setDevAttrResp")


# --- state formatting -----------------------------------------------------

def describe(device: dict, ip: str | None, attrs: dict | None, error: str | None = None) -> dict:
    out = {
        "id": device["id"], "name": device["name"], "model": device.get("model"),
        "ip": ip, "protocol": "aidot", "caps": device.get("caps", {}), "online": attrs is not None,
    }
    if error:
        out["error"] = error
    if attrs:
        if "OnOff" in attrs:
            out["on"] = bool(attrs["OnOff"])
        if "Dimming" in attrs:
            out["brightness"] = int(attrs["Dimming"])
        # A bulb reports CCT while in white mode and RGBW while in colour mode.
        if "CCT" in attrs:
            out["cct"] = int(attrs["CCT"])
            out["mode"] = "white"
        if "RGBW" in attrs:
            out["mode"] = "color"
            value = int(attrs["RGBW"]) & 0xFFFFFFFF
            r, g, b, w = (value >> 24) & 0xFF, (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF
            out["rgb"] = f"#{r:02x}{g:02x}{b:02x}"
            out["white"] = w
        if attrs.get("EffectMode") is not None:
            out["effect"] = attrs["EffectMode"]
    return out


class Lights:
    def __init__(self, credentials: str):
        self.creds = load_credentials(credentials)
        self.user_id = self.creds["userId"]
        self.devices = self.creds["devices"]
        self.ips = {**{d["id"]: d["ip"] for d in self.devices if d.get("ip")}, **load_json(CACHE_FILE, {})}
        self.rediscovered = False

    def select(self, targets: list[str]) -> list[dict]:
        if "all" in targets:
            return self.devices
        hits = []
        for target in targets:
            match = [d for d in self.devices
                     if target in (d["id"], d.get("mac")) or d["name"].lower() == target.lower()]
            if not match:
                raise LightError(f"No light matches {target!r}")
            hits.extend(d for d in match if d not in hits)
        return hits

    def rediscover(self) -> None:
        if self.rediscovered:
            return
        self.rediscovered = True
        found = discover(self.user_id, extra_targets=set(self.ips.values()))
        if found:
            self.ips.update(found)
            write_private_json(CACHE_FILE, self.ips)

    def run(self, device: dict, work) -> dict:
        """Open a session (rediscovering the IP once on failure) and run `work(session)`."""
        last_error = "no IP known; is it on this LAN?"
        for attempt in range(2):
            ip = self.ips.get(device["id"])
            if ip:
                try:
                    session = Session(device, self.user_id, ip)
                    try:
                        return describe(device, ip, work(session))
                    finally:
                        session.close()
                except (OSError, LightError, ValueError) as err:
                    last_error = str(err) or type(err).__name__
            if attempt == 0:
                self.rediscover()
        return describe(device, self.ips.get(device["id"]), None, last_error)

    def run_all(self, devices: list[dict], work) -> list[dict]:
        if any(d["id"] not in self.ips for d in devices):
            self.rediscover()
        with ThreadPoolExecutor(max_workers=max(1, len(devices))) as pool:
            return list(pool.map(lambda d: self.run(d, work), devices))


def build_attr(args, current_on: bool | None) -> dict:
    attr = {}
    if args.toggle:
        attr["OnOff"] = 0 if current_on else 1
    elif args.on or args.off:
        attr["OnOff"] = 1 if args.on else 0
    if args.brightness is not None:
        attr["Dimming"] = max(1, min(100, args.brightness))
    if args.cct is not None:
        attr["CCT"] = args.cct
    if args.rgb is not None:
        hex_value = args.rgb.lstrip("#")
        if len(hex_value) != 6:
            raise LightError("--rgb expects RRGGBB")
        value = (int(hex_value, 16) << 8) | max(0, min(255, args.white or 0))
        attr["RGBW"] = value - (1 << 32) if value >= 1 << 31 else value
    if attr and "OnOff" not in attr:
        attr["OnOff"] = 1  # changing colour or level on an off light turns it on, like the app
    return attr


def matter_lights():
    """The Matter backend when its server is running, else None (use the AiDot protocol)."""
    matter = load_sibling("matter")
    if not matter.available():
        return None
    try:
        return matter.MatterLights()
    except matter.MatterError as err:
        raise LightError(str(err)) from None


def cmd_status(args) -> None:
    if (m := matter_lights()) is not None:
        print(json.dumps({"devices": m.refresh(m.select(args.targets))}))
        return
    lights = Lights(args.credentials)
    print(json.dumps({"devices": lights.run_all(lights.select(args.targets), lambda s: s.get())}))


def cmd_set(args) -> None:
    if (m := matter_lights()) is not None:
        nodes = m.select(args.targets)
        current_on = any(d.get("on") for d in m.refresh(nodes)) if args.toggle else None
        attr = build_attr(args, current_on)
        if not attr:
            raise LightError("Nothing to set")
        print(json.dumps({"devices": m.apply(nodes, attr)}))
        load_music().forget_saved()
        return
    lights = Lights(args.credentials)
    devices = lights.select(args.targets)
    current_on = None
    if args.toggle:
        # Toggling a group follows the app: anything on -> all off, otherwise all on.
        states = lights.run_all(devices, lambda s: s.get())
        current_on = any(s.get("on") for s in states)
    attr = build_attr(args, current_on)
    if not attr:
        raise LightError("Nothing to set")

    def work(session):
        reported = session.set(attr)
        return {**attr, **reported}

    print(json.dumps({"devices": lights.run_all(devices, work)}))
    saved = CACHE_FILE.parent / "music-saved.json"
    if saved.exists():
        load_music().forget_saved()


def cmd_discover(args) -> None:
    creds = load_json(Path(args.credentials).expanduser(), {}) or {}
    found = discover(creds.get("userId", "0"), wait=args.wait)
    names = {d["id"]: d["name"] for d in creds.get("devices", [])}
    if creds:
        write_private_json(CACHE_FILE, {**load_json(CACHE_FILE, {}), **found})
    print(json.dumps([{"id": k, "ip": v, "name": names.get(k)} for k, v in found.items()], indent=2))


def load_sibling(name: str):
    # -I keeps the script directory off sys.path; the modules live beside this file.
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    sys.modules.setdefault("aidot", sys.modules[__name__])
    return __import__(name)


def load_music():
    return load_sibling("music")


def cmd_music(args) -> None:
    load_music().run(args.credentials, args.targets, args.style, args.pulse)


def cmd_music_stop(args) -> None:
    music = load_music()
    stopped = music.stop_running()
    # A clean stop deletes the saved state; if it is still there the sync was
    # killed mid-restore (e.g. plugin reload), so finish the restore here.
    recovered = music.recover(args.credentials)
    print(json.dumps({"stopped": stopped, "recovered": recovered}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--credentials", default=DEFAULT_CREDENTIALS)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="fetch local keys from the AiDot cloud (one time)")
    p.add_argument("--email")
    p.add_argument("--country", default="United States", help="country name as shown in the AiDot app")
    p.add_argument("--region", default="us", choices=["us", "eu", "jp"])
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("discover", help="find AiDot lights on the LAN")
    p.add_argument("--wait", type=float, default=2.0)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("status", help="print light state as JSON")
    p.add_argument("targets", nargs="*", default=["all"])
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("set", help="change light state")
    p.add_argument("targets", nargs="+", help="'all', or device ids, MACs, or names")
    power = p.add_mutually_exclusive_group()
    power.add_argument("--on", action="store_true")
    power.add_argument("--off", action="store_true")
    power.add_argument("--toggle", action="store_true")
    p.add_argument("--brightness", type=int, help="1-100")
    p.add_argument("--cct", type=int, help="colour temperature in kelvin")
    p.add_argument("--rgb", help="RRGGBB")
    p.add_argument("--white", type=int, default=0, help="white channel 0-255 with --rgb")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("music", help="colours follow the music playing on this PC (runs until stopped)")
    p.add_argument("targets", nargs="*", default=["all"])
    p.add_argument("--style", choices=["beat", "smooth", "melody", "auto"], default="beat")
    p.add_argument("--pulse", action="store_true", help="brightness follows loudness (default: full brightness)")
    p.set_defaults(func=cmd_music)

    p = sub.add_parser("music-stop", help="stop a running music sync and restore the lights")
    p.set_defaults(func=cmd_music_stop)

    args = parser.parse_args()
    try:
        args.func(args)
    except LightError as err:
        print(json.dumps({"error": str(err)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
