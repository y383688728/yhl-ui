import argparse
import fcntl
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from app import hostname
from install import COMPOSE, command, deploy_service, port_available, verify_handshake
from openresty import ADMIN_PORT, OpenResty
from panel import NoRedirect


def health_instance(url):
    with build_opener(ProxyHandler({}), NoRedirect()).open(url, timeout=3) as response:
        instance = response.headers.get("X-YHL-Instance", "")
        if response.status == 200 and response.read(32) == b"ready\n" and re.fullmatch(r"[a-f0-9]{32}", instance):
            return instance
    raise RuntimeError("健康检查响应无效。")


def verify_frontend(domain):
    expected = health_instance("http://127.0.0.1:19999/healthz")
    for _ in range(15):
        try:
            if health_instance(f"https://{domain}:{ADMIN_PORT}/healthz") == expected:
                return
        except (OSError, RuntimeError):
            pass
        time.sleep(1)
    raise RuntimeError(f"{domain}:{ADMIN_PORT} 未连到本机 yhl-ui，请检查公网端口、防火墙和反代。")


def install(args):
    domain = args.domain
    command(*COMPOSE, "version")
    frontend = OpenResty(domain)
    addresses = {row[4][0] for row in socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)}
    if len(addresses) != 1 or ":" in next(iter(addresses)):
        raise RuntimeError(f"{domain} 须直连此节点的单个 IPv4 地址。")
    local = verify_handshake("127.0.0.1", domain)
    if verify_handshake(next(iter(addresses)), domain) != local:
        raise RuntimeError(f"{domain}:443 和本机 OpenResty 提供的证书不同，请检查 DNS 是否直连此节点。")
    frontend.check_port()
    running = command(*COMPOSE, "ps", "-q", "yhl-ui", capture=True).stdout.strip()
    if not running:
        port_available("0.0.0.0", 8080)
        port_available("127.0.0.1", 19999)
    print("1Panel OpenResty、443 站点证书和本机端口预检通过。", flush=True)
    if args.check:
        return

    def on_ready():
        with frontend.activate():
            verify_frontend(domain)

    origin = f"https://{domain}:{ADMIN_PORT}"
    options = [
        "--domain", domain, "--server-name", domain, "--admin-port", str(ADMIN_PORT),
        "--first-device", "默认设备", "--generate-password",
    ]
    deploy_service(args.image_archive, running, options, [origin, "8080"], on_ready=on_ready)
    print(f"yhl-ui 后台 {origin}")
    print(f"代理 {domain}:8080")
    print(f"443 网站 https://{domain}/")


def main():
    parser = argparse.ArgumentParser(description="复用 1Panel 站点证书安装 yhl-ui")
    parser.add_argument("domain", type=hostname)
    parser.add_argument("--image-archive", type=Path)
    parser.add_argument("--check", action="store_true", help="仅验证环境和证书")
    args = parser.parse_args()
    if args.image_archive and not args.image_archive.is_file():
        parser.error("找不到 Docker 镜像归档。")
    try:
        with open("/run/lock/yhl-ui.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            install(args)
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
        parser.exit(1, f"yhl-ui 安装中断：{error}\n")


if __name__ == "__main__":
    os.umask(0o077)
    main()
