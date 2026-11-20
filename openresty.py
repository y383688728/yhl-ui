import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from install import command, port_available, verify_handshake


CONF_DIR = "/usr/local/openresty/nginx/conf/conf.d"
BINARY = "/usr/local/openresty/bin/openresty"
ADMIN_PORT = 9999
DIRECTIVE = re.compile(r"^[ \t]*(listen|server_name|ssl_certificate(?:_key)?)\s+([^;#\r\n]+);", re.M)
CERT = re.compile(r"/www/sites/[.a-zA-Z0-9_-]+/ssl/fullchain\.pem")


def site_certificate(directory, domain):
    matches = []
    for path in directory.glob("*.conf"):
        if not path.is_file() or path.is_symlink():
            continue
        source = path.read_text(encoding="utf-8")
        names, ports, certificates, keys = [], [], [], []
        values = {"server_name": names, "listen": ports, "ssl_certificate": certificates, "ssl_certificate_key": keys}
        for kind, value in DIRECTIVE.findall(source):
            values[kind].append(value.split())
        if not any(domain in map(str.lower, record) for record in names):
            continue
        if not any("ssl" in listen[1:] and (
            listen[0] == "443" or listen[0].endswith(":443")
        ) for listen in ports):
            continue
        if len(re.findall(r"^[ \t]*server\s*\{", source, re.M)) != 1:
            raise RuntimeError(f"443 站点 {path.name} 含多个 server 区块，无法安全确定证书。")
        if len(certificates) != 1 or len(certificates[0]) != 1 or len(keys) != 1 or len(keys[0]) != 1:
            raise RuntimeError(f"443 站点 {path.name} 的证书配置不明确。")
        cert, key = (words[0][0].strip("\"'") for words in (certificates, keys))
        if not CERT.fullmatch(cert) or ".." in Path(cert).parts or key != cert.removesuffix("fullchain.pem") + "privkey.pem":
            raise RuntimeError(f"443 站点 {path.name} 未使用 1Panel 标准的站点证书文件。")
        matches.append((cert, key))
    if len(matches) != 1:
        raise RuntimeError(f"请在 1Panel 中为 {domain} 建立唯一的 443 HTTPS 站点，并绑定已签发的证书。")
    return matches[0]


def render(domain, cert, key):
    return f"""# Managed by yhl-ui
server {{
    listen {ADMIN_PORT} ssl;
    server_name {domain};
    ssl_certificate {cert};
    ssl_certificate_key {key};
    ssl_protocols TLSv1.2 TLSv1.3;
    access_log off;
    error_log /dev/null crit;
    location / {{
        proxy_pass http://127.0.0.1:19999;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_redirect off;
        proxy_cache off;
    }}
}}
"""


class OpenResty:
    def __init__(self, domain, *, removing=False):
        listing = ["--all"] if removing else []
        result = command(
            "docker", "ps", *listing, "--filter", "label=com.docker.compose.service=openresty",
            "--format", "{{.ID}} {{.Image}}", capture=True,
        )
        matches = [
            identifier for identifier, image in (line.split() for line in result.stdout.splitlines())
            if re.fullmatch(r"(?:[^ ]+/)?1panel/openresty(?::[^ ]+|@sha256:[a-f0-9]+)?", image)
        ]
        if len(matches) != 1:
            raise RuntimeError("请先在 1Panel 安装并启动唯一一个 OpenResty 实例。")
        self.container = matches[0]
        status = command("docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", self.container, capture=True)
        if status.stdout.strip() != "host":
            raise RuntimeError("1Panel OpenResty 须使用应用商店默认的 host 网络，才能自动接出 9999。")
        mounts = json.loads(command(
            "docker", "inspect", "--format", "{{json .Mounts}}", self.container, capture=True,
        ).stdout)
        locations = [
            Path(mount["Source"]) for mount in mounts
            if mount["Type"] == "bind" and mount["Destination"].rstrip("/") == CONF_DIR and mount["RW"]
        ]
        if len(locations) != 1 or not locations[0].is_dir():
            raise RuntimeError("找不到 1Panel OpenResty 的标准网站配置目录。")
        directory = locations[0].resolve()
        name = "yhl-ui-admin-" + hashlib.sha256(domain.encode()).hexdigest()[:12] + ".conf"
        self.path = directory / name
        self.domain = domain
        if removing:
            return
        cert, key = site_certificate(directory, domain)
        self.config = render(domain, cert, key).encode("utf-8")
        if self.path.is_symlink() or (self.path.exists() and self.path.read_bytes() != self.config):
            raise RuntimeError(f"{name} 已存在且配置不同，安装器无法接管。")
        self.run("-t", "-q")

    def run(self, *args):
        result = command(
            "docker", "exec", self.container, BINARY, *args, capture=True, check=False, timeout=15,
        )
        if result.returncode:
            raise RuntimeError(f"OpenResty {args[0]} 失败，请在 1Panel 检查 OpenResty 配置。")

    def check_port(self):
        try:
            port_available("0.0.0.0", ADMIN_PORT)
        except RuntimeError:
            # A live 9999 service with a valid certificate may be an existing admin proxy.
            verify_handshake("127.0.0.1", self.domain, ADMIN_PORT)
            return True
        return False

    def remove(self):
        if self.path.is_symlink():
            raise RuntimeError("9999 配置是符号链接，请手工检查。")
        if not self.path.exists():
            return
        source = self.path.read_text(encoding="utf-8")
        certs = [value.strip() for kind, value in DIRECTIVE.findall(source) if kind == "ssl_certificate"]
        if len(certs) != 1 or not CERT.fullmatch(certs[0]):
            raise RuntimeError("9999 配置与安装器生成的内容不同，请手工检查。")
        cert = certs[0]
        key = cert.removesuffix("fullchain.pem") + "privkey.pem"
        if source != render(self.domain, cert, key):
            raise RuntimeError("9999 配置与安装器生成的内容不同，请手工检查。")
        running = command("docker", "inspect", "--format", "{{.State.Running}}", self.container, capture=True)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".yhl-ui-removal-")
        os.close(fd)
        backup = Path(name)
        os.replace(self.path, backup)
        try:
            if running.stdout.strip() == "true":
                self.run("-t", "-q")
                self.run("-s", "reload")
        except BaseException:
            os.link(backup, self.path)
            backup.unlink()
            raise
        backup.unlink()

    @contextmanager
    def activate(self):
        if self.check_port():
            yield
            return
        created = False
        reload_requested = False
        try:
            if not self.path.exists():
                fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".yhl-ui-")
                temp_path = Path(name)
                try:
                    with os.fdopen(fd, "wb") as file:
                        file.write(self.config)
                        file.flush()
                        os.fsync(file.fileno())
                    os.link(temp_path, self.path)
                    created = True
                finally:
                    temp_path.unlink(missing_ok=True)
            self.run("-t", "-q")
            reload_requested = True
            self.run("-s", "reload")
            yield
        except BaseException:
            if created and self.path.is_file() and not self.path.is_symlink() and self.path.read_bytes() == self.config:
                self.path.unlink()
                if reload_requested:
                    self.run("-t", "-q")
                    self.run("-s", "reload")
            raise
