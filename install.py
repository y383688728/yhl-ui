import argparse
import fcntl
import getpass
import ipaddress
import os
import socket
import ssl
import subprocess
import time
from pathlib import Path

from app import hostname, origin_port
from panel import Panel, confirm_dns, get_http


ROOT = Path(__file__).resolve().parent
COMPOSE = ["docker", "compose", "--project-name", "yhl-ui"]


def command(*args, capture=False, check=True, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=check, text=True, capture_output=capture, **kwargs)


def port_available(host, port):
    try:
        with socket.socket() as connection:
            connection.bind((host, port))
    except OSError as error:
        raise RuntimeError(f"TCP {port} 已占用，安装已中断。") from error


def verify_handshake(host, server_name):
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    try:
        with socket.create_connection((host, 443), timeout=8) as connection:
            with context.wrap_socket(connection, server_hostname=server_name):
                pass
    except OSError as error:
        raise RuntimeError("代理握手目标无法通过 TLS 1.3 证书验证。") from error


def verify_site(url, marker):
    for _ in range(25):
        try:
            if marker in get_http(url, timeout=3):
                return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError(f"{url} 未通过内容与 HTTPS 验证。")


def install(args):
    public_domain = args.public_domain or args.domain
    admin_port, proxy_port = args.admin_port, args.proxy_port
    if (
        len({80, 443, admin_port, proxy_port, 19999, 19998}) != 6
        or not 1024 <= proxy_port
    ):
        raise RuntimeError("后台与代理端口须不同，并且不能占用 80、443、19998、19999。代理端口须大于 1023。")
    confirm_dns(args.domain, args.public_ip)
    confirm_dns(public_domain, args.public_ip)
    verify_handshake(args.handshake_host or args.server_name, args.server_name)
    command(*COMPOSE, "version")
    panel = Panel(args.panel_url, getpass.getpass("1Panel API Key: "))
    sites = [(args.domain, admin_port, "http://127.0.0.1:19999"),
             (public_domain, 443, "http://127.0.0.1:19998")]
    acme_id = panel.preflight(sites, args.acme_account, args.acme_email)
    running = command(*COMPOSE, "ps", "-q", "yhl-ui", capture=True).stdout.strip()
    existing = panel.find_site(panel.sites(), *sites[0])
    if existing is None:
        port_available("0.0.0.0", admin_port)
    if not running:
        port_available("0.0.0.0", proxy_port)
        port_available("127.0.0.1", 19998)
        port_available("127.0.0.1", 19999)
    print("DNS、握手目标、后台端口和 1Panel API 预检通过。", flush=True)
    if args.check:
        return
    cert_id = panel.certificate(args.domain, public_domain, acme_id, args.acme_email)
    if args.image_archive:
        command("docker", "load", "-i", str(args.image_archive.resolve()))
    else:
        command(*COMPOSE, "build")
    options = [
        "--domain", args.domain, "--admin-port", str(admin_port), "--port", str(proxy_port),
        "--server-name", args.server_name, "--first-device", "默认设备",
    ]
    if args.handshake_host:
        options += ["--handshake-host", args.handshake_host]
    state_file = "/data/state.json"
    if not running:
        initialized = command(
            *COMPOSE, "run", "--rm", "--no-deps", "-T", "--entrypoint", "test",
            "yhl-ui", "-f", state_file, check=False,
        )
        if initialized.returncode == 1:
            command(*COMPOSE, "run", "--rm", "--no-deps", "yhl-ui", "init", *options)
        elif initialized.returncode:
            raise RuntimeError("无法检查 yhl-ui 持久化数据。")
    query = (
        "import sys,json; s=json.load(open('/data/state.json')); "
        "sys.exit(0 if (s['origin'],str(s['port']),s['server_name'],s['handshake_host']) "
        "== tuple(sys.argv[1:]) else 2)"
    )
    supplied = [
        f"https://{args.domain}:{admin_port}", str(proxy_port), args.server_name,
        args.handshake_host or args.server_name,
    ]
    same = command(
        *COMPOSE, "run", "--rm", "--no-deps", "-T", "--entrypoint", "python",
        "yhl-ui", "-c", query, *supplied, check=False,
    )
    if same.returncode:
        raise RuntimeError("现有持久化配置与本次设置不符，请使用原设置重新运行安装器。")
    previous = command(
        "docker", "inspect", "--format", "{{.Image}}", running, capture=True,
    ).stdout.strip() if running else None
    try:
        command(*COMPOSE, "up", "-d", "--no-build", "--wait", "--wait-timeout", "45")
        verify_site("http://127.0.0.1:19999/healthz", "ready\n")
        verify_site("http://127.0.0.1:19998/", "<title>小游戏</title>")
    except (RuntimeError, subprocess.CalledProcessError):
        if previous:
            command("docker", "tag", previous, "yhl-ui:rollback")
            command(
                *COMPOSE, "up", "-d", "--no-build", "--wait", "--wait-timeout", "45",
                env={**os.environ, "YHL_IMAGE": "yhl-ui:rollback"},
            )
        else:
            command(*COMPOSE, "stop", "yhl-ui")
        raise
    for domain, port, upstream in sites:
        panel.create_site(domain, port, upstream, args.website_group, cert_id)
    verify_site(f"https://{args.domain}:{admin_port}/healthz", "ready\n")
    verify_site(f"https://{public_domain}/", "<title>小游戏</title>")
    print(f"yhl-ui 后台 https://{args.domain}:{admin_port}")
    print(f"代理 {args.domain}:{proxy_port}")
    print(f"443 网站 https://{public_domain}/")


def main():
    parser = argparse.ArgumentParser(description="通过 1Panel v2 部署 yhl-ui")
    parser.add_argument("--domain", required=True, type=hostname)
    parser.add_argument("--public-ip", required=True, type=ipaddress.IPv4Address)
    parser.add_argument("--public-domain", type=hostname)
    parser.add_argument("--server-name", required=True, type=hostname)
    parser.add_argument("--handshake-host", type=hostname)
    parser.add_argument("--admin-port", type=origin_port, default=9999)
    parser.add_argument("--proxy-port", type=origin_port, default=8080)
    parser.add_argument("--panel-url", default="http://127.0.0.1:8888")
    parser.add_argument("--website-group", type=int, default=1)
    acme = parser.add_mutually_exclusive_group(required=True)
    acme.add_argument("--acme-email")
    acme.add_argument("--acme-account", type=int)
    parser.add_argument("--image-archive", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    if args.acme_email and ("@" not in args.acme_email or any(char.isspace() for char in args.acme_email)):
        parser.error("请输入有效的 ACME 邮箱。")
    if args.website_group < 1 or (args.acme_account is not None and args.acme_account < 1):
        parser.error("网站组和 ACME 账户编号须为正整数。")
    if args.image_archive is not None and not args.image_archive.is_file():
        parser.error("找不到 Docker 镜像归档。")
    try:
        with open("/run/lock/yhl-ui.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            install(args)
    except (RuntimeError, OSError, subprocess.CalledProcessError, ValueError, KeyError) as error:
        parser.exit(1, f"yhl-ui 安装中断：{error}\n")


if __name__ == "__main__":
    main()
