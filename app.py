import argparse
import base64
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import struct
import subprocess
import threading
import time
from html import escape
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from core import DOMAIN, Core, device, subscription, vless_uri
from routing_rules import COMMA_PAYLOAD_TYPES, DEFAULT_RULES, MAX_RULE_BYTES, RULE_TYPES, parse_rules, read_rules
import web_update


COOKIE = "__Host-yhl-session"
ACCOUNT = "admin"
DEFAULT_ADMIN_PATH = "/admin"
SESSION_TTL = 8 * 3600
STYLE = Path(__file__).with_name("style.css")
SCRIPT = Path(__file__).with_name("admin.js")
PUBLIC = Path(__file__).with_name("public") / "index.html"


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024**2).hex()


def totp(secret, step):
    digest = hmac.digest(base64.b32decode(secret), struct.pack(">Q", step), "sha1")
    offset = digest[-1] & 15
    return f"{(int.from_bytes(digest[offset:offset + 4], 'big') & 0x7fffffff) % 1000000:06d}"


def totp_required(state):
    return state["auth"].get("totp_enabled", True)


def new_auth(password):
    salt = secrets.token_bytes(16)
    return {
        "salt": salt.hex(),
        "password_hash": password_hash(password, salt),
        "totp": base64.b32encode(secrets.token_bytes(20)).decode(),
        "totp_enabled": False,
    }


def entry_path(state):
    return state.get("admin_path", DEFAULT_ADMIN_PATH)


def subscription_url(state, device_entry):
    return f'{state["origin"]}{entry_path(state)}/sub/{device_entry["token"]}'


def qr_png(value):
    import qrcode
    from qrcode.image.pure import PyPNGImage

    image = qrcode.make(value, image_factory=PyPNGImage, box_size=9)
    output = BytesIO()
    image.save(output)
    return output.getvalue()


OTP_INPUT = (
    '<label>验证器代码 <input name="otp" inputmode="numeric" autocomplete="one-time-code" '
    'pattern="[0-9]{6}" required maxlength="6"></label>'
)
PASSWORD_INPUT = (
    '<label>密码 <input name="password" type="password" autocomplete="current-password" '
    'required maxlength="256"></label>'
)


def page(title, body, prefix, interactive=False):
    script = f'<script defer src="{prefix}/admin.js"></script>' if interactive else ""
    return f"""<!doctype html><html lang="zh-CN"><head>
    <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{escape(title)} · yhl-ui</title><link rel="stylesheet" href="{prefix}/style.css">
    {script}</head><body><main><header><span class="brand">yhl-ui</span><span>单节点代理管理</span></header>
    {body}</main></body></html>"""


def login_page(state, message=""):
    prefix = entry_path(state)
    notice = f'<p class="notice" role="alert">{escape(message)}</p>' if message else ""
    return page("管理员登录", f"""<section class="login">
    <h1>管理员登录</h1>{notice}
    <form method="post" action="{prefix}/login">
    <label>账号 <input name="account" value="{ACCOUNT}" autocomplete="username" required maxlength="32"></label>
    {PASSWORD_INPUT}{OTP_INPUT if totp_required(state) else ""}
    <button>登录</button></form></section>""", prefix)


def security_page(state, csrf, message=""):
    enabled = totp_required(state)
    prefix = entry_path(state)
    notice = f'<p class="notice" role="alert">{escape(message)}</p>' if message else ""
    guide = ""
    if not enabled:
        secret = state["auth"]["totp"]
        label = quote("yhl-ui:" + state["domain"], safe="")
        uri = escape(f"otpauth://totp/{label}?secret={secret}&issuer=yhl-ui", quote=True)
        guide = f"""<p>把下面的设置密钥添加到验证器，然后输入验证器显示的六位数字。</p>
        <label>设置密钥 <input readonly value="{secret}"></label>
        <label>导入链接 <input readonly value="{uri}"></label>"""
    action = "disable" if enabled else "enable"
    label = "关闭验证器" if enabled else "启用验证器"
    return page("登录安全", f"""<section class="login">
    <p><a href="{prefix}/">返回后台</a></p><h1>登录安全</h1>{notice}
    <h2>修改后台密码</h2>
    <form method="post" action="{prefix}/security/password">
    <input type="hidden" name="csrf" value="{csrf}">
    <label>当前密码 <input name="password" type="password" autocomplete="current-password" required maxlength="256"></label>
    <label>新密码 <input name="new_password" type="password" autocomplete="new-password" required minlength="12" maxlength="256"></label>
    <label>确认新密码 <input name="confirm_password" type="password" autocomplete="new-password" required minlength="12" maxlength="256"></label>
    {OTP_INPUT if enabled else ""}<button>修改密码</button></form></section>
    <section class="login"><h2>验证器</h2><p>验证器已{'启用' if enabled else '关闭'}</p>
    <form method="post" action="{prefix}/security/{action}">
    <input type="hidden" name="csrf" value="{csrf}">
    {guide}{PASSWORD_INPUT}{OTP_INPUT}<button>{label}</button></form></section>""", prefix)


def dashboard(state, running, csrf, message=""):
    prefix = entry_path(state)
    token = f'<input type="hidden" name="csrf" value="{csrf}">'
    rows = []
    for entry in state["devices"]:
        url = escape(subscription_url(state, entry), quote=True)
        rows.append(f"""<li><div><strong>{escape(entry["name"])}</strong>
        <div class="subscription-url"><input readonly aria-label="Mihomo 订阅链接" value="{url}">
        <button type="button" class="secondary" data-copy-url hidden>复制 URL</button></div>
        <p class="muted copy-status" role="status"></p></div>
        <form class="device-actions" method="post" action="{prefix}/devices/remove">{token}
        <a class="button secondary" href="{prefix}/qr/{entry['id']}" target="_blank" rel="noopener noreferrer">Mihomo 订阅码</a>
        <a class="button secondary" href="{prefix}/qr/node/{entry['id']}" target="_blank" rel="noopener noreferrer">小火箭节点码</a>
        <a class="button secondary" href="{prefix}/traffic/{entry['id']}">流量详情</a>
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
    <div class="hero-actions"><span class="status">{status}</span>
    <a class="button secondary" href="{prefix}/rules">Clash 分流规则</a>
    <a class="button secondary" href="{prefix}/update">版本更新</a>
    <a class="button secondary" href="{prefix}/security">登录安全</a></div></section>
    {notice}
    <section class="panel"><h2>设备与订阅</h2>{devices}
    <form class="actions" method="post" action="{prefix}/devices/add">{token}
    <label>设备名称 <input name="name" required maxlength="60" placeholder="例如 手机"></label>
    <button>添加设备</button></form></section>
    <section class="panel"><h2>节点名称</h2>
    <form class="actions" method="post" action="{prefix}/node">{token}
    <label>显示名称 <input name="label" required maxlength="60" value="{escape(state['label'], quote=True)}"></label>
    <button>保存</button></form></section>
    <form class="logout" method="post" action="{prefix}/logout">{token}
    <button class="secondary">退出登录</button></form>""", prefix, interactive=True)


def rules_page(state, csrf, source=None, message="", visual_draft=False):
    prefix = entry_path(state)
    current = state.get("routing_rules", {})
    saved_source = current.get("source", "")
    if source is None:
        source = saved_source
    common = {
        "DOMAIN": "完整域名", "DOMAIN-SUFFIX": "域名后缀", "DOMAIN-KEYWORD": "域名关键词",
        "IP-CIDR": "IPv4 网段", "IP-CIDR6": "IPv6 网段", "GEOSITE": "域名分类",
        "GEOIP": "IP 地区", "MATCH": "其余流量",
    }
    options = "".join(f'<option value="{kind}">{label} · {kind}</option>' for kind, label in common.items())
    advanced_options = "".join(f"<option>{kind}</option>" for kind in sorted(RULE_TYPES - common.keys()))
    policy_names = {"节点": "代理 · 节点", "DIRECT": "直连 · DIRECT", "REJECT": "拒绝 · REJECT"}
    policies = "".join(
        f'<option value="{policy}">{policy_names.get(policy, policy)}</option>'
        for policy in ("节点", "DIRECT", "REJECT", "REJECT-DROP", "PASS", "PASS-RULE", "COMPATIBLE")
    )
    rules = current.get("rules", DEFAULT_RULES)
    yaml_pending = source != saved_source
    if visual_draft:
        try:
            rules = read_rules(source)
            yaml_pending = False
        except ValueError:
            pass
    editor_data = escape(json.dumps({
        "rules": rules,
        "commaPayload": sorted(COMMA_PAYLOAD_TYPES),
        "yamlPending": yaml_pending,
    }, ensure_ascii=False))
    notice = f'<p class="notice" role="status">{escape(message)}</p>' if message else ""
    example = "rules:\n  - DOMAIN-SUFFIX,example.com,DIRECT\n  - MATCH,节点"
    return page("Clash 分流规则", f"""<section class="panel">
    <p><a href="{prefix}/">返回后台</a></p><h1>Clash 分流规则</h1>{notice}
    <p class="muted">规则从上到下匹配。保存后刷新 Clash/Mihomo 订阅即可应用。</p>
    <form class="rules-editor" method="post" action="{prefix}/rules">
    <input type="hidden" name="csrf" value="{csrf}">
    <input type="hidden" id="rule-edit-mode" name="mode" value="{'visual' if visual_draft else 'yaml'}">
    <section id="visual-rules" hidden aria-label="可视化规则编辑器">
    <div id="rule-editor-data" hidden>{editor_data}</div>
    <div class="rule-fields">
    <label>规则类型 <select id="rule-type">{options}<optgroup label="高级类型">{advanced_options}</optgroup></select></label>
    <label id="rule-value-field">匹配值 <input id="rule-value" autocomplete="off" spellcheck="false"></label>
    <label>策略 <select id="rule-policy">{policies}</select></label>
    </div>
    <div class="rule-controls">
    <button type="button" id="apply-rule">添加规则</button>
    <button type="button" class="secondary" id="cancel-rule" hidden>取消编辑</button>
    <label id="rule-options-field">附加参数 <input id="rule-options" placeholder="可选，如 no-resolve" spellcheck="false"></label>
    </div>
    <p id="rule-feedback" class="muted" role="status"></p>
    <ol id="rule-list" class="rule-list" aria-label="规则顺序"></ol>
    </section>
    <p id="yaml-pending" class="notice" role="status" hidden>YAML 有改动，保存后可继续使用可视化编辑。</p>
    <details id="rule-yaml" open><summary>高级编辑 · rules YAML</summary>
    <p class="muted">可直接粘贴。清空后保存可恢复默认规则。表单编辑会重新生成 YAML。</p>
    <textarea name="rules" aria-label="rules YAML" spellcheck="false" maxlength="{MAX_RULE_BYTES}" rows="12"
        placeholder="{escape(example, quote=True)}">{escape(source)}</textarea></details>
    <button>保存规则</button></form></section>""", prefix, interactive=True)


def update_page(state, csrf, status):
    prefix = entry_path(state)
    phase = status.get("phase")
    version = status.get("version")
    messages = {
        "idle": "点击检查更新，查看 GitHub 是否有新版本。",
        "busy": "更新服务正在处理请求。刷新状态可查看结果。",
        "checking": "正在检查版本。",
        "updating": "更新已开始。后台和代理可能短暂中断，恢复后如果提示登录，请重新登录。",
        "current": "当前已是最新版本。",
        "check_error": "检查更新失败，请稍后重试。",
        "update_error": "更新未完成。可重新检查，或到服务器终端查看升级结果。",
        "unavailable": "网页更新暂不可用。可在服务器终端运行安装命令更新。",
    }
    available = phase == "available" and web_update.valid_version(version) and web_update.newer(version, web_update.VERSION)
    message = messages.get(phase, "可以检查是否有更新。")
    if available:
        message = f"发现新版本 v{version}。更新时后台和代理可能短暂中断。"
    elif phase == "installed" and web_update.valid_version(version):
        message = f"已完成 v{version} 的更新。"
    actions = ''
    pending = phase in {"busy", "checking", "updating"}
    if pending:
        actions = f'<a class="button secondary" href="{prefix}/update">刷新状态</a>'
    elif phase != "unavailable":
        install_button = f'<button name="action" value="install {version}">安装更新</button>' if available else ''
        actions = f'''<form class="actions" method="post" action="{prefix}/update">
        <input type="hidden" name="csrf" value="{csrf}">
        {install_button}<button class="secondary" name="action" value="check">检查更新</button></form>'''
    return page("版本更新", f'''<section class="panel" {'data-update-poll' if pending else ''}>
    <p><a href="{prefix}/">返回后台</a></p><h1>版本更新</h1>
    <p>当前版本 v{web_update.VERSION}</p><p class="muted" role="status">{escape(message)}</p>
    {actions}</section>''', prefix, interactive=pending)


def format_bytes(amount):
    if amount < 1024:
        return f"{amount:,} B"
    quantity = amount
    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB", "EiB"):
        quantity /= 1024
        if quantity < 1024 or unit == "EiB":
            return f"{quantity:,.2f} {unit}"


def traffic_page(state, device_entry, usage, sampled):
    prefix = entry_path(state)
    upload, download = usage
    name = escape(device_entry["name"])
    notice = "" if sampled else '<p class="notice">当前未取得实时数据，显示最近已保存的统计。</p>'
    return page("订阅流量", f"""<section class="panel">
    <p><a href="{prefix}/">返回后台</a></p><h1>{name}的订阅流量</h1>{notice}
    <dl class="traffic-totals">
    <div><dt>上传</dt><dd>{format_bytes(upload)}</dd></div>
    <div><dt>下载</dt><dd>{format_bytes(download)}</dd></div>
    <div><dt>合计</dt><dd>{format_bytes(upload + download)}</dd></div>
    </dl>
    <p class="muted">从流量统计启用后累计。共用此订阅的设备合并计量。</p>
    </section>""", prefix)


class Admin:
    def __init__(self, core, state):
        self.core = core
        self.state = state
        self.origin = state["origin"]
        self.lock = threading.Lock()
        self.sessions = {}
        self.failures = []
        self.last_step = self.core.store.last_step()

    def verify(self, password, otp, require_otp):
        now = time.time()
        self.failures = [stamp for stamp in self.failures if now - stamp < 60]
        if len(self.failures) >= 8:
            return 429, None
        auth = self.state["auth"]
        step = int(now // 30)
        if 1 <= len(password) <= 256 and hmac.compare_digest(
            password_hash(password, bytes.fromhex(auth["salt"])), auth["password_hash"],
        ):
            if not require_otp:
                self.failures.clear()
                return 200, None
            if re.fullmatch(r"[0-9]{6}", otp):
                for candidate in (step, step - 1, step + 1):
                    if candidate > self.last_step and hmac.compare_digest(totp(auth["totp"], candidate), otp):
                        self.failures.clear()
                        return 200, candidate
        self.failures.append(now)
        return 401, None

    def login(self, account, password, otp):
        status, step = self.verify(password if account == ACCOUNT else "", otp, totp_required(self.state))
        if status != 200:
            return None, status
        if step is not None:
            self.core.store.save_step(step)
            self.last_step = step
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.sessions = {key: value for key, value in self.sessions.items() if value[0] > now}
        self.sessions[token] = (now + SESSION_TTL, secrets.token_urlsafe(32))
        return token, 303

    def configure_totp(self, enabled, password, otp, token):
        if enabled == totp_required(self.state):
            return 200
        status, step = self.verify(password, otp, True)
        if status != 200:
            return status
        auth = {**self.state["auth"], "totp_enabled": enabled}
        if not enabled:
            auth["totp"] = base64.b32encode(secrets.token_bytes(20)).decode()
            step = 0
        state = {**self.state, "auth": auth}
        self.core.store.save(state, last_step=step)
        self.state = state
        self.last_step = step
        self.sessions = {token: self.sessions[token]}
        return 200

    def change_password(self, form):
        new_password = form.get("new_password", "")
        if not 12 <= len(new_password) <= 256:
            return 400, "新密码须为 12 至 256 个字符。"
        if new_password != form.get("confirm_password", ""):
            return 400, "两次输入的新密码不一致。"
        status, step = self.verify(form.get("password", ""), form.get("otp", ""), totp_required(self.state))
        if status == 401:
            return status, "当前密码或验证器代码有误。"
        if status == 429:
            return status, "尝试次数过多，请稍后再试。"
        salt = secrets.token_bytes(16)
        auth = {**self.state["auth"], "salt": salt.hex(), "password_hash": password_hash(new_password, salt)}
        state = {**self.state, "auth": auth}
        self.core.store.save(state, last_step=step)
        self.state = state
        if step is not None:
            self.last_step = step
        self.sessions.clear()
        return 200, "密码已修改，请重新登录。"

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

    def change_rules(self, source):
        parsed = parse_rules(source)
        state = dict(self.state)
        if parsed is None:
            state.pop("routing_rules", None)
        else:
            state["routing_rules"] = parsed
        self.core.store.save(state)
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
        binary = isinstance(body, bytes)
        payload = body if binary else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type if binary else f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        # Form navigation needs a same-origin Origin header for POST validation.
        self.send_header("Referrer-Policy", "same-origin" if content_type == "text/html" else "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
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
        request = urlsplit(self.path)
        path = request.path
        prefix = entry_path(app.state)
        if path == prefix:
            return self.redirect(prefix + "/")
        if path.startswith(prefix + "/"):
            path = path[len(prefix):]
        elif path == "/healthz" and not self.headers.get("X-Forwarded-Proto") and (
            self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"
        ):
            pass
        elif path.startswith("/sub/") and "admin_path" not in app.state:
            pass
        else:
            return self.respond(404)
        if path == "/style.css":
            return self.respond(200, STYLE.read_text(encoding="utf-8"), "text/css")
        if path == "/admin.js":
            return self.respond(200, SCRIPT.read_text(encoding="utf-8"), "text/javascript")
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
            if path.startswith("/qr/"):
                _, csrf = self.session(app)
                key = path.removeprefix("/qr/")
                node = key.startswith("node/")
                if node:
                    key = key.removeprefix("node/")
                if csrf:
                    for entry in app.state["devices"]:
                        if entry["id"] == key:
                            payload = vless_uri(app.state, entry) if node else subscription_url(app.state, entry)
                            return self.respond(200, qr_png(payload), "image/png")
                return self.respond(404)
            if path.startswith("/traffic/"):
                _, csrf = self.session(app)
                key = path.removeprefix("/traffic/")
                if csrf:
                    for entry in app.state["devices"]:
                        if entry["id"] == key:
                            sampled = app.core.collect_traffic()
                            try:
                                usage = app.core.store.traffic(key)
                            except RuntimeError:
                                return self.respond(503)
                            return self.respond(200, traffic_page(app.state, entry, usage, sampled))
                return self.respond(404)
            if path not in ("/", "/login", "/security", "/rules", "/update"):
                return self.respond(404)
            _, csrf = self.session(app)
            if csrf:
                if path == "/security":
                    return self.respond(200, security_page(app.state, csrf))
                if path == "/rules":
                    message = "规则已保存，刷新订阅后即可应用。" if request.query == "saved=1" else ""
                    return self.respond(200, rules_page(app.state, csrf, message=message))
                if path == "/update":
                    return self.respond(200, update_page(app.state, csrf, web_update.request("status")))
                return self.respond(200, dashboard(app.state, app.core.running, csrf))
            if path in ("/security", "/rules", "/update"):
                return self.redirect(prefix + "/")
            message = "密码已修改，请重新登录。" if path == "/login" and request.query == "password=changed" else ""
            self.respond(200, login_page(app.state, message))

    def do_POST(self):
        app = self.server.app
        path = urlsplit(self.path).path
        prefix = entry_path(app.state)
        if not path.startswith(prefix + "/"):
            return self.respond(404)
        path = path[len(prefix):]
        if self.headers.get("Origin") != app.origin:
            return self.respond(403)
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            return self.respond(415)
        try:
            size = int(self.headers["Content-Length"])
            if not 0 < size <= (MAX_RULE_BYTES * 3 + 1024 if path == "/rules" else 12288):
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
                try:
                    token, status = app.login(form.get("account", ""), password, otp)
                except (OSError, RuntimeError):
                    return self.respond(503, login_page(app.state, "暂时无法保存登录状态，请稍后重试。"))
                if token:
                    return self.redirect(prefix + "/", token)
                message = "尝试次数过多，请稍后再试。" if status == 429 else "登录凭据无效。"
                return self.respond(status, login_page(app.state, message))
            token, csrf = self.session(app)
            form_csrf = form.get("csrf", "")
            if not csrf or not form_csrf.isascii() or not hmac.compare_digest(form_csrf, csrf):
                return self.respond(403)
            if path == "/logout":
                app.sessions.pop(token, None)
                return self.redirect(prefix + "/", "")
            if path == "/update":
                action = form.get("action", "")
                if action != "check" and not (action.startswith("install ") and web_update.valid_version(action[8:])):
                    return self.respond(400)
                return self.respond(200, update_page(app.state, csrf, web_update.request(action)))
            if path == "/rules":
                source = form.get("rules", "")
                visual_draft = form.get("mode") == "visual"
                try:
                    app.change_rules(source)
                except ValueError as error:
                    return self.respond(400, rules_page(app.state, csrf, source, str(error), visual_draft))
                except (OSError, RuntimeError):
                    return self.respond(503, rules_page(app.state, csrf, source, "暂时无法保存规则，请稍后重试。", visual_draft))
                return self.redirect(prefix + "/rules?saved=1")
            if path == "/security/password":
                try:
                    status, message = app.change_password(form)
                except (OSError, RuntimeError):
                    return self.respond(503, security_page(app.state, csrf, "暂时无法保存，请稍后重试。"))
                if status == 200:
                    return self.redirect(prefix + "/login?password=changed", "")
                return self.respond(status, security_page(app.state, csrf, message))
            if path in ("/security/enable", "/security/disable"):
                try:
                    status = app.configure_totp(
                        path.endswith("/enable"), form.get("password", ""), form.get("otp", ""), token,
                    )
                except (OSError, RuntimeError):
                    return self.respond(503, security_page(app.state, csrf, "暂时无法保存，请稍后重试。"))
                if status == 200:
                    return self.redirect(prefix + "/security")
                message = "尝试次数过多，请稍后再试。" if status == 429 else "密码或验证器代码有误。"
                return self.respond(status, security_page(app.state, csrf, message))
            if path not in ("/devices/add", "/devices/remove", "/node"):
                return self.respond(404)
            try:
                app.change(path, form)
            except ValueError as error:
                return self.respond(400, dashboard(app.state, app.core.running, csrf, str(error)))
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                return self.respond(503, dashboard(app.state, app.core.running, csrf, "保存失败，已尝试恢复原有代理配置。"))
            return self.redirect(prefix + "/")


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


def admin_path(value):
    if not value.startswith("/"):
        value = "/" + value
    if value in ("/healthz", "/sub") or not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise argparse.ArgumentTypeError("后台路径须为单段路径，例如 /admin 或 /my-panel。")
    return value


def show_bootstrap(password):
    print(json.dumps({"account": ACCOUNT, "password": password}))


def reset_login(core):
    state = core.store.load()
    password = secrets.token_urlsafe(24)
    core.store.save({**state, "auth": new_auth(password)}, last_step=0)
    show_bootstrap(password)


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
    private_key, public_key = core.keypair()
    origin = f"https://{args.domain}:{args.admin_port}"
    state = {
        "origin": origin,
        "admin_path": args.admin_path,
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
        "auth": new_auth(password),
    }
    core.store.save(state)
    if args.generate_password:
        show_bootstrap(password)
    else:
        print(f"后台地址 {origin}{args.admin_path}/")
        print(f"登录账号 {ACCOUNT}")
        if generated:
            print(f"生成的管理员密码 {password}")


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
                else:
                    core.collect_traffic()

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
    init.add_argument("--admin-path", type=admin_path, default=DEFAULT_ADMIN_PATH)
    init.add_argument("--proxy-bind", default="0.0.0.0")
    init.add_argument("--first-device")
    init.add_argument("--generate-password", action="store_true")
    run = commands.add_parser("serve")
    commands.add_parser("reset-login")
    run.add_argument("--http-bind", default="127.0.0.1")
    run.add_argument("--public-port", type=lambda value: None if value == "0" else origin_port(value))
    for command in (init, run):
        command.add_argument("--http-port", type=origin_port, default=19999)
    args = parser.parse_args()
    args.data.mkdir(mode=0o700, parents=True, exist_ok=True)
    traffic_tool = shutil.which("yhl-traffic") if args.command == "serve" else None
    core = Core(args.core, args.data, traffic_tool)
    if args.command == "init":
        initialize(args, core)
    elif args.command == "reset-login":
        reset_login(core)
    else:
        serve(args, core)


if __name__ == "__main__":
    main()
