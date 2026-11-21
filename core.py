import json
import os
import re
import secrets
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

from routing_rules import DEFAULT_RULES
from storage import Store


FLOW = "xtls-rprx-vision"
DOMAIN = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}")


def write_json(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def device(name):
    name = name.strip()
    if not 1 <= len(name) <= 60 or any(ord(char) < 32 for char in name):
        raise ValueError("设备名称须为 1 至 60 个字符。")
    return {"id": str(uuid.uuid4()), "name": name, "token": secrets.token_urlsafe(32)}


def config(state, stats_address=None):
    result = {
        "log": {"level": "warn"},
        "inbounds": [{
            "type": "vless",
            "listen": state["bind"],
            "listen_port": state["port"],
            "users": [
                {"name": entry["id"], "uuid": entry["id"], "flow": FLOW}
                for entry in state["devices"]
            ],
            "tls": {
                "enabled": True,
                "server_name": state["server_name"],
                "reality": {
                    "enabled": True,
                    "handshake": {"server": state["handshake_host"], "server_port": state["handshake_port"]},
                    "private_key": state["private_key"],
                    "short_id": [state["short_id"]],
                },
            },
        }],
        "outbounds": [{"type": "direct", "tag": "internet"}],
    }
    if stats_address:
        result["experimental"] = {"v2ray_api": {
            "listen": stats_address,
            "stats": {"enabled": True, "users": [entry["id"] for entry in state["devices"]]},
        }}
    return result


def subscription(state, entry):
    # JSON is valid YAML and avoids interpolation of untrusted names into YAML.
    name = f'{state["label"]} · {entry["name"]}'
    return json.dumps({
        "mixed-port": 7890,
        "mode": "rule",
        "proxies": [{
            "name": name,
            "type": "vless",
            "server": state["domain"],
            "port": state["port"],
            "uuid": entry["id"],
            "flow": FLOW,
            "udp": True,
            "tls": True,
            "network": "tcp",
            "servername": state["server_name"],
            "client-fingerprint": "chrome",
            "reality-opts": {"public-key": state["public_key"], "short-id": state["short_id"]},
        }],
        "proxy-groups": [{"name": "节点", "type": "select", "proxies": [name]}],
        "rules": state.get("routing_rules", {}).get("rules", DEFAULT_RULES),
    }, ensure_ascii=False)


def vless_uri(state, entry):
    query = urlencode({
        "type": "tcp",
        "encryption": "none",
        "security": "reality",
        "flow": FLOW,
        "sni": state["server_name"],
        "fp": "chrome",
        "pbk": state["public_key"],
        "sid": state["short_id"],
    })
    label = quote(f'{state["label"]} · {entry["name"]}', safe="")
    return f'vless://{entry["id"]}@{state["domain"]}:{state["port"]}?{query}#{label}'


class Core:
    def __init__(self, binary, data, traffic_tool=None):
        self.binary = binary
        self.store = Store(data)
        self.path = data / "sing-box.json"
        self.pending = data / "sing-box.pending.json"
        self.process = None
        self.traffic_tool = traffic_tool
        self.stats_address = None
        self.stats_snapshot = {}
        if traffic_tool:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                self.stats_address = f"127.0.0.1:{listener.getsockname()[1]}"

    def command(self, *args):
        result = subprocess.run(
            [self.binary, *args], capture_output=True, encoding="utf-8", timeout=12,
        )
        if result.returncode:
            raise RuntimeError(f"sing-box {args[0]} 执行失败，退出码 {result.returncode}。")
        return result.stdout

    def keypair(self):
        output = self.command("generate", "reality-keypair")
        keys = dict(re.findall(r"^(PrivateKey|PublicKey)\s*:\s*([\w-]{43})\s*$", output, re.M))
        if keys.keys() != {"PrivateKey", "PublicKey"}:
            raise RuntimeError("无法读取 sing-box 生成的 REALITY 密钥。")
        return keys["PrivateKey"], keys["PublicKey"]

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def stop(self):
        if self.running:
            self.collect_traffic()
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process = None

    def collect_traffic(self):
        if not self.stats_address or not self.running:
            return False
        try:
            result = subprocess.run(
                [self.traffic_tool, self.stats_address], capture_output=True, encoding="utf-8",
                timeout=3, check=True,
            )
            counts = json.loads(result.stdout)
            deltas = {}
            for key, (upload, download) in counts.items():
                old_up, old_down = self.stats_snapshot.get(key, (0, 0))
                delta = (
                    upload - old_up if upload >= old_up else upload,
                    download - old_down if download >= old_down else download,
                )
                if delta != (0, 0):
                    deltas[key] = delta
            self.store.add_traffic(deltas)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            return False
        self.stats_snapshot = counts
        return True

    def start(self, path, state):
        if not state["devices"]:
            return
        self.stats_snapshot = {}
        self.process = subprocess.Popen([self.binary, "run", "-c", str(path)])
        address = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(state["bind"], state["bind"])
        deadline = time.monotonic() + 4
        while self.running and time.monotonic() < deadline:
            try:
                with socket.create_connection((address, state["port"]), timeout=0.2):
                    pass
            except OSError:
                time.sleep(0.05)
            else:
                time.sleep(0.15)
                if self.running:
                    return
                break
        self.stop()
        raise RuntimeError("代理端口未能启动，请检查端口是否被占用。")

    def apply(self, state, previous=None):
        try:
            write_json(self.pending, config(state, self.stats_address))
            if state["devices"]:
                self.command("check", "-c", str(self.pending))
            self.stop()
            try:
                self.start(self.pending, state)
                os.replace(self.pending, self.path)
                self.store.save(state)
            except (OSError, RuntimeError):
                self.stop()
                if previous is not None:
                    write_json(self.path, config(previous, self.stats_address))
                    self.start(self.path, previous)
                raise
        finally:
            self.pending.unlink(missing_ok=True)
