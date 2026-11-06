#!/usr/bin/env python3
"""Zero-dependency WebUI & WebSocket RPC proxy for Leigod Plugin Linux."""

import argparse
import base64
import configparser
import json
import os
import socket
import struct
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "index.html"
LEIGOD_DIR = Path("/opt/leigod")
STATE_DIR = Path(os.environ.get("LEIGOD_STATE_DIR", "/var/lib/leigod"))
PROFILES_FILE = STATE_DIR / "saved_profiles.json"


def ws_rpc_call(host: str, port: int, path: str, payload: dict, timeout: float = 4.0) -> dict:
    """Minimal RFC 6455 WebSocket client using only the Python standard library."""
    raw_key = base64.b64encode(os.urandom(16)).decode("ascii")
    with socket.create_connection((host, port), timeout=timeout) as sock:
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {raw_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(handshake.encode("ascii"))
        header_buf = b""
        while b"\r\n\r\n" not in header_buf:
            chunk = sock.recv(1024)
            if not chunk:
                raise ConnectionError("WebSocket handshake closed unexpectedly")
            header_buf += chunk

        status_line = header_buf.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
        if "101" not in status_line:
            raise ConnectionError(f"WebSocket handshake rejected: {status_line}")

        body = json.dumps(payload).encode("utf-8")
        mask_key = os.urandom(4)
        header = bytearray([0x81])
        length = len(body)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask_key)
        masked_body = bytes(b ^ mask_key[i % 4] for i, b in enumerate(body))
        sock.sendall(bytes(header) + masked_body)

        first_two = sock.recv(2)
        if len(first_two) < 2:
            raise ConnectionError("Empty WebSocket frame from daemon")
        r_len = first_two[1] & 0x7F
        if r_len == 126:
            r_len = struct.unpack("!H", sock.recv(2))[0]
        elif r_len == 127:
            r_len = struct.unpack("!Q", sock.recv(8))[0]
        masked = (first_two[1] & 0x80) != 0
        srv_mask = sock.recv(4) if masked else b""
        data = bytearray()
        while len(data) < r_len:
            chunk = sock.recv(r_len - len(data))
            if not chunk:
                break
            data.extend(chunk)
        if masked:
            data = bytearray(b ^ srv_mask[i % 4] for i, b in enumerate(data))
        return json.loads(data.decode("utf-8", errors="replace"))


def load_saved_profiles() -> list:
    if PROFILES_FILE.is_file():
        try:
            data = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def save_profiles(profiles: list) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PROFILES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(PROFILES_FILE)
    except Exception:
        pass


def scan_and_capture_active_profiles() -> list:
    """Scan /opt/leigod/config and daemon's PrivateTmp (/proc/<pid>/root/tmp/acc) for active *_acc_conf.json."""
    profiles = load_saved_profiles()
    existing_keys = {p.get("id") for p in profiles}
    candidate_dirs = [LEIGOD_DIR / "config", Path("/tmp/acc")]

    try:
        res = subprocess.run(["pgrep", "-f", "/opt/leigod/acc-gw.router.amd64"], capture_output=True, text=True, timeout=2)
        for pid in res.stdout.split():
            if pid.isdigit():
                candidate_dirs.append(Path(f"/proc/{pid}/root/tmp/acc"))
                candidate_dirs.append(Path(f"/proc/{pid}/root/opt/leigod/config"))
    except Exception:
        pass

    changed = False
    core_meta = {}
    for d in candidate_dirs:
        core_file = d / "acc_core_conf.json"
        if core_file.is_file():
            try:
                core_meta = json.loads(core_file.read_text(encoding="utf-8"))
                acc_list = core_meta.get("acceleration") or []
                for item in acc_list:
                    if not isinstance(item, dict):
                        continue
                    for dev_key, dev_val in item.items():
                        if not isinstance(dev_val, dict) or not dev_val.get("acc_conf"):
                            continue
                        raw_text = dev_val["acc_conf"]
                        parsed = json.loads(raw_text) if isinstance(raw_text, str) else raw_text
                        game_info = parsed.get("game") or {}
                        game_id = str(game_info.get("id") or dev_val.get("server") or dev_key)
                        game_area = str(game_info.get("area") or dev_val.get("game_area") or "默认区服")
                        catalog = int(dev_val.get("device_type") or dev_val.get("catalog_type") or 2)
                        sub_catalog = int(dev_val.get("sub_type") or 0)
                        prof_id = f"{catalog}-{sub_catalog}-{game_id}-{game_area}"
                        entry = {
                            "id": prof_id,
                            "name": f"{dev_key} 加速 ({game_area.upper()} | {game_id[:8]}...)",
                            "catalog": catalog,
                            "subCatalog": sub_catalog,
                            "gameId": game_id,
                            "area": game_area,
                            "conf": raw_text if isinstance(raw_text, str) else json.dumps(raw_text),
                            "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                        }
                        if prof_id in existing_keys:
                            for idx, old in enumerate(profiles):
                                if old.get("id") == prof_id and old.get("conf") != entry["conf"]:
                                    entry["name"] = old.get("name", entry["name"])
                                    profiles[idx] = entry
                                    changed = True
                        else:
                            profiles.insert(0, entry)
                            existing_keys.add(prof_id)
                            changed = True
                break
            except Exception:
                pass

    for d in candidate_dirs:
        if not d.is_dir():
            continue
        try:
            for f in d.iterdir():
                if f.name.endswith("_acc_conf.json") or f.name == "acc_conf.json":
                    raw_text = f.read_text(encoding="utf-8").strip()
                    if not raw_text:
                        continue
                    parsed = json.loads(raw_text)
                    game_id = str(parsed.get("game_id") or core_meta.get("game_id") or f.stem)
                    game_area = str(parsed.get("game_area") or core_meta.get("game_area") or "默认区服")
                    catalog = int(parsed.get("catalog_type") or core_meta.get("catalog_type") or 1)
                    sub_catalog = int(parsed.get("sub_type") or core_meta.get("sub_type") or 1)
                    prof_id = f"{catalog}-{sub_catalog}-{game_id}-{game_area}"
                    entry = {
                        "id": prof_id,
                        "name": f"游戏 #{game_id} ({game_area})",
                        "catalog": catalog,
                        "subCatalog": sub_catalog,
                        "gameId": game_id,
                        "area": game_area,
                        "conf": raw_text,
                        "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    if prof_id in existing_keys:
                        for idx, old in enumerate(profiles):
                            if old.get("id") == prof_id and old.get("conf") != raw_text:
                                entry["name"] = old.get("name", entry["name"])
                                profiles[idx] = entry
                                changed = True
                    else:
                        profiles.insert(0, entry)
                        existing_keys.add(prof_id)
                        changed = True
        except Exception:
            pass

    if changed:
        save_profiles(profiles)
    return profiles


def probe_udp_6066() -> str:
    try:
        out = subprocess.check_output(["ss", "-ulnp"], text=True, timeout=2)
        if ":6066 " in out:
            return "监听正常 (6066/udp)"
    except Exception:
        pass
    return "未检测到 6066/udp 监听"


def check_firewall_residuals() -> bool:
    try:
        res = subprocess.run(["iptables", "-t", "mangle", "-S", "OUTPUT"], capture_output=True, text=True, timeout=2)
        if "0x10" in res.stdout or "GAMEACC" in res.stdout:
            return True
    except Exception:
        pass
    return False


def read_host_status() -> dict:
    service_state = "unknown"
    service_active = False
    try:
        res = subprocess.run(
            ["systemctl", "is-active", "leigod_plugin.service"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        service_state = res.stdout.strip() or "inactive"
        service_active = res.returncode == 0
    except Exception:
        pass

    mac = ""
    for mac_path in (Path("/sys/class/net/wlan0/address"), STATE_DIR / "device-mac"):
        if mac_path.is_file():
            try:
                mac = mac_path.read_text(encoding="utf-8").strip()
                if mac:
                    break
            except Exception:
                pass

    tun_if = "未创建 (开启加速后生成)"
    active_tun = ""
    try:
        res = subprocess.run(["ip", "-o", "link", "show", "type", "tun"], capture_output=True, text=True, timeout=2)
        lines = [line.split(":")[1].strip().split("@")[0] for line in res.stdout.splitlines() if ":" in line]
        if lines:
            tun_if = ", ".join(lines)
            active_tun = lines[0]
    except Exception:
        pass

    # Read real traffic counters (prefer active tun interface, fallback to default route interface)
    traffic_iface = active_tun
    if not traffic_iface:
        try:
            res = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True, timeout=2)
            for part in res.stdout.split():
                if part == "dev":
                    idx = res.stdout.split().index("dev")
                    if idx + 1 < len(res.stdout.split()):
                        traffic_iface = res.stdout.split()[idx + 1]
                        break
        except Exception:
            pass

    rx_bytes = 0
    tx_bytes = 0
    if traffic_iface:
        try:
            rx_bytes = int(Path(f"/sys/class/net/{traffic_iface}/statistics/rx_bytes").read_text().strip())
            tx_bytes = int(Path(f"/sys/class/net/{traffic_iface}/statistics/tx_bytes").read_text().strip())
        except Exception:
            pass

    version = ""
    ver_ini = LEIGOD_DIR / "config/acc_version.ini"
    if ver_ini.is_file():
        cp = configparser.ConfigParser()
        try:
            cp.read(ver_ini, encoding="utf-8")
            version = cp.get("info", "version", fallback="")
        except Exception:
            pass

    token = ""
    user_id = ""
    acc_ini = LEIGOD_DIR / "config/accelerator.ini"
    if acc_ini.is_file():
        cp = configparser.ConfigParser()
        try:
            cp.read(acc_ini, encoding="utf-8")
            for section in cp.sections():
                for k, v in cp.items(section):
                    if k.lower() in ("token", "bind_token", "rtoken") and v:
                        token = v
                    elif k.lower() in ("user_id", "userid") and v:
                        user_id = v
        except Exception:
            pass

    profiles = scan_and_capture_active_profiles()

    return {
        "service_active": service_active,
        "service_state": service_state,
        "mac": mac,
        "tun_interface": tun_if,
        "traffic_iface": traffic_iface,
        "rx_bytes": rx_bytes,
        "tx_bytes": tx_bytes,
        "has_gameacc_chain": check_firewall_residuals(),
        "udp_6066": probe_udp_6066(),
        "version": version,
        "token": token,
        "user_id": user_id,
        "profiles": profiles,
    }


class WebUIHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            content = INDEX_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if self.path == "/api/host-status":
            self._send_json(200, read_host_status())
            return
        if self.path == "/api/profiles":
            self._send_json(200, {"profiles": scan_and_capture_active_profiles()})
            return
        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        if self.path == "/api/ws-rpc":
            try:
                req_obj = self._read_json()
                cmd_upper = str(req_obj.get("cmd", "")).upper() if isinstance(req_obj, dict) else ""
                if cmd_upper in (
                    "OPTROUTER",
                    "OPT",
                    "NEWREBOOTDEVICE",
                    "NEWRESETDEVICE",
                    "SHELLEXEC",
                    "SHELL",
                    "UNINSTALLENGINE",
                    "NEWUPDATEFW",
                    "NEWUPDATEFWVER",
                    "NEWMODIFYUBUSSETTINGS",
                    "NEWMODIFYNETWORKSETTINGS",
                    "NEWMODIFYDHCPSETTINGS",
                    "NEWMODIFYWIFISETTINGS",
                    "MODIFYROUTER",
                    "MODIFY",
                ):
                    self._send_json(
                        403,
                        {
                            "cmd": cmd_upper,
                            "code": -1,
                            "err": f"已拦截高危路由器固件指令 {cmd_upper}（该指令在路由器固件中会调用宿主机 reboot/shell）",
                            "data": {},
                        },
                    )
                    return
                # If user triggers STARTACC manually, also save it to profiles library
                if isinstance(req_obj, dict) and str(req_obj.get("cmd", "")).upper() in ("STARTACC", "START"):
                    d = req_obj.get("data") or {}
                    if isinstance(d, dict) and d.get("conf"):
                        profiles = load_saved_profiles()
                        prof_id = f"{d.get('catalog', 1)}-{d.get('subCatalog', 1)}-manual"
                        if not any(p.get("id") == prof_id for p in profiles):
                            profiles.insert(
                                0,
                                {
                                    "id": prof_id,
                                    "name": d.get("name", f"自定义配置 ({prof_id})"),
                                    "catalog": int(d.get("catalog", 1)),
                                    "subCatalog": int(d.get("subCatalog", 1)),
                                    "gameId": "-",
                                    "area": "-",
                                    "conf": d["conf"] if isinstance(d["conf"], str) else json.dumps(d["conf"]),
                                    "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                                },
                            )
                            save_profiles(profiles)
                resp = ws_rpc_call("127.0.0.1", 5588, "/ws", req_obj)
                self._send_json(200, resp)
            except Exception as exc:
                self._send_json(
                    502,
                    {
                        "cmd": "ERROR",
                        "code": -1,
                        "err": f"连接 127.0.0.1:5588/ws 失败: {exc}",
                        "data": {},
                    },
                )
            return

        if self.path == "/api/cleanup-net":
            script = LEIGOD_DIR / "leigod_cleanup_net.sh"
            try:
                if script.is_file():
                    subprocess.run(["/bin/sh", str(script)], check=False, timeout=5)
                self._send_json(200, {"ok": True, "msg": "已清理 GAMEACC 链、策略路由表 99、ipset 与残留 tun 接口"})
            except Exception as exc:
                self._send_json(500, {"ok": False, "msg": str(exc)})
            return

        if self.path == "/api/service-restart":
            try:
                subprocess.run(["systemctl", "restart", "leigod_plugin.service"], check=True, timeout=10)
                self._send_json(200, {"ok": True, "msg": "已安全重启 leigod_plugin.service 守护服务"})
            except Exception as exc:
                self._send_json(500, {"ok": False, "msg": str(exc)})
            return

        if self.path == "/api/profiles/delete":
            try:
                body = self._read_json()
                prof_id = body.get("id")
                profiles = [p for p in load_saved_profiles() if p.get("id") != prof_id]
                save_profiles(profiles)
                self._send_json(200, {"ok": True, "profiles": profiles})
            except Exception as exc:
                self._send_json(400, {"ok": False, "msg": str(exc)})
            return

        self._send_json(404, {"error": "Not found"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Leigod Plugin Linux WebUI Server")
    parser.add_argument(
        "--host",
        default=os.environ.get("LEIGOD_WEBUI_HOST", "127.0.0.1"),
        help="Listen address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("LEIGOD_WEBUI_PORT", os.environ.get("PORT", "8088"))),
        help="Listen port (default: 8088)",
    )
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), WebUIHandler)
    print(f"Leigod WebUI running at http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
