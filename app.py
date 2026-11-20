import argparse
import base64
import getpass
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import signal
import struct
import subprocess
import threading
import time
from html import escape
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from core import DOMAIN, Core, device, subscription


COOKIE = "__Host-yhl-session"
SESSION_TTL = 8 * 3600
STYLE = Path(__file__).with_name("style.css")
PUBLIC = Path(__file__).with_name("public") / "index.html"


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024**2).hex()


def totp(secret, step):
    digest = hmac.digest(base64.b32decode(secret), struct.pack(">Q", step), "sha1")
    offset = digest[-1] & 15
    return f"{(int.from_bytes(digest[offset:offset + 4], 'big') & 0x7fffffff) % 1000000:06d}"


def page(title, body):
    return f"""<!doctype html><html lang="zh-CN"><head>
    <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{escape(title)} · yhl-ui</title><link rel="stylesheet" href="/style.css">
    </head><body><main><header><span class="brand">yhl-ui</span><span>单节点代理管理</span></header>
    {body}</main></body></html>"""


def login_page(message=""):
    notice = f'<p class="notice" role="alert">{escape(message)}</p>' if message else ""
    return page("管理员登录", f"""<section class="login">
    <h1>管理员登录</h1>{notice}
    <form method="post" action="/login">
    <label>密码 <input name="password" type="password" autocomplete="current-password" required maxlength="256"></label>
    <label>验证器代码 <input name="otp" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{{6}}" required maxlength="6"></label>
    <button>登录</button></form></section>""")


def dashboard(state, origin, running, csrf, message=""):
    token = f'<input type="hidden" name="csrf" value="{csrf}">'
    rows = []
    for entry in state["devices"]:
        url = escape(f'{origin}/sub/{entry["token"]}', quote=True)
        rows.append(f"""<li><div><strong>{escape(entry["name"])}</strong>
        <input readonly aria-label="Mihomo 订阅链接" value="{url}"></div>
        <form method="post" action="/devices/remove">{token}
        <input type="hidden" name="id" value="{entry['id']}"><button class="secondary">撤销</button>
        </form></li>""")
    devices = f'<ul>{"".join(rows)}</ul>' if rows else '<p class="muted">添加设备后，代理会开始监听。</p>'
    if not state["devices"]:
        status = "等待添加设备"
    else:
        status = "运行中" if running else "已停止"
    notice = f'<p class="notice" role="alert">{escape(message)}</p>' if message else ""
    return page("节点管理", f"""<section class="hero"><div><h1>{escape(state["label"])}</h1>
    <p>{escape(state["domain"])}:{state["port"]} · VLESS / REALITY / Vision</p></div>
    <span class="status">{status}</span></section>
    {notice}
    <section class="panel"><h2>设备与订阅</h2>{devices}
    <form class="actions" method="post" action="/devices/add">{token}
    <label>设备名称 <input name="name" required maxlength="60" placeholder="例如 手机"></label>
    <button>添加设备</button></form></section>
    <section class="panel"><h2>节点名称</h2>
    <form class="actions" method="post" action="/node">{token}
    <label>显示名称 <input name="label" required maxlength="60" value="{escape(state['label'], quote=True)}"></label>
    <button>保存</button></form></section>
    <form class="logout" method="post" action="/logout">{token}<button class="secondary">退出登录</button></form>""")


class Admin:
    def __init__(self, core, state):
        self.core = core
        self.state = state
        self.origin = state["origin"]
        self.lock = threading.Lock()
        self.sessions = {}
        self.failures = []
        self.last_step = self.core.store.last_step()

    def login(self, password, otp):
        now = time.time()
        self.failures = [stamp for stamp in self.failures if now - stamp < 60]
        if len(self.failures) >= 8:
            return None, 429
        auth = self.state["auth"]
        step = int(now // 30)
        if hmac.compare_digest(password_hash(password, bytes.fromhex(auth["salt"])), auth["password_hash"]):
            for candidate in (step, step - 1, step + 1):
                if candidate > self.last_step and hmac.compare_digest(totp(auth["totp"], candidate), otp):
                    self.core.store.save_step(candidate)
                    self.last_step = candidate
                    self.failures.clear()
                    token = secrets.token_urlsafe(32)
                    self.sessions = {key: value for key, value in self.sessions.items() if value[0] > now}
                    self.sessions[token] = (now + SESSION_TTL, secrets.token_urlsafe(32))
                    return token, 303
        self.failures.append(now)
        return None, 401

    def change(self, path, form):
        state = dict(self.state)
        entries = state["devices"]
        if path == "/devices/add":
            if len(entries) >= 100:
                raise ValueError("最多支持 100 台设备。")
            entry = device(form.get("name", ""))
            state["devices"] = [*entries, entry]
        elif path == "/devices/remove":
            remaining = [entry for entry in entries if entry["id"] != form.get("id")]
            if len(remaining) == len(entries):
                raise ValueError("该设备已撤销。")
            state["devices"] = remaining
        else:
            label = form.get("label", "").strip()
            if not 1 <= len(label) <= 60 or any(ord(char) < 32 for char in label):
                raise ValueError("节点名称须为 1 至 60 个字符。")
            state["label"] = label
            self.core.store.save(state)
            self.state = state
            return
        self.core.apply(state, self.state)
        self.state = state


class Handler(BaseHTTPRequestHandler):
    server_version = "yhl-ui"
    sys_version = ""

    def setup(self):
        self.request.settimeout(8)
        super().setup()

    def log_message(self, *args):
        # Request URLs can contain device subscription tokens.
        pass

    def respond(self, status, body="", content_type="text/html", headers=None):
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def redirect(self, path, cookie=None):
        headers = {"Location": path}
        if cookie is not None:
            seconds = SESSION_TTL if cookie else 0
            headers["Set-Cookie"] = f"{COOKIE}={cookie}; Path=/; Max-Age={seconds}; Secure; HttpOnly; SameSite=Strict"
        self.respond(303, headers=headers)

    def session(self, app):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
        except CookieError:
            return "", ""
        if cookie is None:
            return "", ""
        token = cookie.value
        expiry, csrf = app.sessions.get(token, (0, ""))
        if expiry <= time.time():
            app.sessions.pop(token, None)
            return "", ""
        return token, csrf

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        app = self.server.app
        path = urlsplit(self.path).path
        if path == "/style.css":
            return self.respond(200, STYLE.read_text(encoding="utf-8"), "text/css")
        with app.lock:
            if path == "/healthz":
                ready = app.core.running or not app.state["devices"]
                return self.respond(
                    200 if ready else 503, "ready\n" if ready else "unavailable\n",
                    "text/plain", {"X-YHL-Instance": self.server.instance},
                )
            if path.startswith("/sub/"):
                key = path.removeprefix("/sub/")
                if re.fullmatch(r"[a-zA-Z0-9_-]{43}", key):
                    for entry in app.state["devices"]:
                        if hmac.compare_digest(key, entry["token"]):
                            return self.respond(200, subscription(app.state, entry), "application/yaml")
                return self.respond(404)
            if path not in ("/", "/login"):
                return self.respond(404)
            _, csrf = self.session(app)
            if csrf:
                return self.respond(200, dashboard(app.state, app.origin, app.core.running, csrf))
        self.respond(200, login_page())

    def do_POST(self):
        app = self.server.app
        path = urlsplit(self.path).path
        if self.headers.get("Origin") != app.origin:
            return self.respond(403)
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            return self.respond(415)
        try:
            size = int(self.headers["Content-Length"])
            if not 0 < size <= 4096:
                return self.respond(413)
            form = {
                key: values[0] for key, values in parse_qs(
                    self.rfile.read(size).decode(), max_num_fields=5, keep_blank_values=True,
                ).items()
            }
        except (ValueError, TypeError, OSError):
            return self.respond(400)
        with app.lock:
            if path == "/login":
                password, otp = form.get("password", ""), form.get("otp", "")
                if not 1 <= len(password) <= 256 or not re.fullmatch(r"[0-9]{6}", otp):
                    return self.respond(400, login_page("请输入密码和六位验证器代码。"))
                try:
                    token, status = app.login(password, otp)
                except (OSError, RuntimeError):
                    return self.respond(503, login_page("暂时无法保存登录状态，请稍后重试。"))
                if token:
                    return self.redirect("/", token)
                message = "尝试次数过多，请稍后再试。" if status == 429 else "登录凭据无效。"
                return self.respond(status, login_page(message))
            token, csrf = self.session(app)
            form_csrf = form.get("csrf", "")
            if not csrf or not form_csrf.isascii() or not hmac.compare_digest(form_csrf, csrf):
                return self.respond(403)
            if path == "/logout":
                app.sessions.pop(token, None)
                return self.redirect("/", "")
            if path not in ("/devices/add", "/devices/remove", "/node"):
                return self.respond(404)
            try:
                app.change(path, form)
            except ValueError as error:
                return self.respond(400, dashboard(app.state, app.origin, app.core.running, csrf, str(error)))
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                return self.respond(503, dashboard(app.state, app.origin, app.core.running, csrf, "保存失败，已尝试恢复原有代理配置。"))
            return self.redirect("/")


class Placeholder(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        body = PUBLIC.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def origin_port(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口须在 1 至 65535 之间。")
    return port


def hostname(value):
    value = value.lower()
    if not DOMAIN.fullmatch(value):
        raise argparse.ArgumentTypeError("请输入有效的域名。")
    return value


def initialize(args, core):
    if core.store.initialized:
        raise SystemExit("数据目录已初始化。")
    if len({args.port, args.admin_port, args.http_port}) != 3 or args.port in (80, 443) or args.admin_port in (80, 443):
        raise SystemExit("代理、后台和本地 HTTP 必须使用不同的端口，保留 80/443 给网站。")
    password = "" if args.generate_password else getpass.getpass("管理员密码（至少 12 个字符，留空自动生成）: ")
    generated = not password
    password = password or secrets.token_urlsafe(24)
    if not 12 <= len(password) <= 256:
        raise SystemExit("管理员密码长度须为 12 至 256 个字符。")
    if not generated and not hmac.compare_digest(password.encode(), getpass.getpass("再次输入密码: ").encode()):
        raise SystemExit("两次输入的密码不同。")
    salt = secrets.token_bytes(16)
    secret = base64.b32encode(secrets.token_bytes(20)).decode()
    private_key, public_key = core.keypair()
    origin = f"https://{args.domain}:{args.admin_port}"
    state = {
        "origin": origin,
        "domain": args.domain,
        "port": args.port,
        "bind": str(ipaddress.ip_address(args.proxy_bind)),
        "label": "我的代理节点",
        "server_name": args.server_name,
        "handshake_host": args.handshake_host or args.server_name,
        "handshake_port": args.handshake_port,
        "private_key": private_key,
        "public_key": public_key,
        "short_id": secrets.token_hex(8),
        "devices": [device(args.first_device)] if args.first_device else [],
        "auth": {"salt": salt.hex(), "password_hash": password_hash(password, salt), "totp": secret},
    }
    enrollment = f'otpauth://totp/{quote("yhl-ui:" + args.domain, safe="")}?secret={secret}&issuer=yhl-ui'
    core.store.save(state)
    print(f"后台地址: {origin}")
    if generated:
        print(f"生成的管理员密码: {password}")
    print(f"验证器设置密钥: {secret}")
    print(enrollment)


def serve(args, core):
    state = core.store.load()
    app = Admin(core, state)
    server = ThreadingHTTPServer((args.http_bind, args.http_port), Handler)
    server.app = app
    server.instance = secrets.token_hex(16)
    stopped = threading.Event()
    placeholder = None

    def supervise():
        while not stopped.wait(5):
            with app.lock:
                if app.state["devices"] and not core.running:
                    try:
                        core.start(core.path, app.state)
                    except (OSError, RuntimeError):
                        print("代理进程重启失败。", flush=True)

    def terminate(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        core.apply(state)
        if args.public_port is not None:
            placeholder = ThreadingHTTPServer(("127.0.0.1", args.public_port), Placeholder)
            threading.Thread(target=placeholder.serve_forever, daemon=True).start()
        threading.Thread(target=supervise, daemon=True).start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        server.server_close()
        if placeholder is not None:
            placeholder.shutdown()
            placeholder.server_close()
        with app.lock:
            core.stop()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="yhl-ui 单节点代理管理")
    parser.add_argument("--data", type=Path, default=Path(os.getenv("YHL_DATA", "data")))
    parser.add_argument("--core", default=os.getenv("YHL_CORE", "sing-box"))
    commands = parser.add_subparsers(required=True, dest="command")
    init = commands.add_parser("init")
    init.add_argument("--domain", type=hostname, required=True)
    init.add_argument("--server-name", type=hostname, required=True)
    init.add_argument("--handshake-host", type=hostname)
    init.add_argument("--handshake-port", type=origin_port, default=443)
    init.add_argument("--port", type=origin_port, default=8080)
    init.add_argument("--admin-port", type=origin_port, default=9999)
    init.add_argument("--proxy-bind", default="0.0.0.0")
    init.add_argument("--first-device")
    init.add_argument("--generate-password", action="store_true")
    run = commands.add_parser("serve")
    run.add_argument("--http-bind", default="127.0.0.1")
    run.add_argument("--public-port", type=lambda value: None if value == "0" else origin_port(value))
    for command in (init, run):
        command.add_argument("--http-port", type=origin_port, default=19999)
    args = parser.parse_args()
    args.data.mkdir(mode=0o700, parents=True, exist_ok=True)
    core = Core(args.core, args.data)
    if args.command == "init":
        initialize(args, core)
    else:
        serve(args, core)


if __name__ == "__main__":
    main()
