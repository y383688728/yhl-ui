import argparse
import fcntl
import getpass
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from app import DEFAULT_ADMIN_PATH, admin_path, hostname, origin_port
from panel import Panel, confirm_dns, get_http


ROOT = Path(__file__).resolve().parent
COMPOSE = ["docker", "compose", "--project-name", "yhl-ui"]
SERVICE_NAMES = {"yhl-ui-yhl-ui-1", "yhl-ui_yhl-ui_1"}


@dataclass(frozen=True)
class ServiceContainer:
    id: str
    name: str
    state: str
    slot: str


def command(*args, capture=False, check=True, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=check, text=True, capture_output=capture, **kwargs)


def service_containers():
    rows = command(
        "docker", "ps", "-a", "--no-trunc", "--filter", f"label=com.docker.compose.project={COMPOSE[3]}",
        "--filter", "label=com.docker.compose.service=yhl-ui",
        "--format", '[{{json .ID}},{{json .Names}},{{json .State}},'
        '{{json (.Label "com.docker.compose.container-number")}},{{json (.Label "com.docker.compose.oneoff")}}]',
        capture=True,
    ).stdout.splitlines()
    containers = []
    for row in rows:
        container_id, name, state, slot, oneoff = json.loads(row)
        if oneoff.lower() != "true":
            containers.append(ServiceContainer(container_id, name, state, slot))
    if len(containers) < 2:
        return (containers[0] if containers else None), None
    if len(containers) == 2:
        originals = [
            entry for entry in containers
            if entry.name in SERVICE_NAMES and entry.slot == "1" and entry.state in {"running", "exited"}
        ]
        if len(originals) == 1:
            original = originals[0]
            staged = next(entry for entry in containers if entry != original)
            # Compose creates this replacement before removing the original service container.
            if staged.state == "created" and staged.slot == original.slot and staged.name == f"{original.id[:12]}_{original.name}":
                return original, staged
    raise RuntimeError("有多个 yhl-ui 服务容器，无法确认安全的恢复方案。请先检查 Docker Compose 项目。")


def recover_interrupted_service(original, staged):
    if original.state != "running":
        command("docker", "start", original.id)
    command("docker", "rm", staged.id)
    print("已恢复上次中断的容器替换，继续使用原有数据。", flush=True)


def port_available(host, port):
    try:
        with socket.socket() as connection:
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            connection.bind((host, port))
    except OSError as error:
        raise RuntimeError(f"TCP {port} 已占用，安装已中断。") from error


def verify_handshake(host, server_name, port=443):
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    try:
        with socket.create_connection((host, port), timeout=8) as connection:
            with context.wrap_socket(connection, server_hostname=server_name) as tls:
                return tls.getpeercert(binary_form=True)
    except OSError as error:
        raise RuntimeError(f"{server_name}:{port} 无法通过 {host} 的 TLS 1.3 证书验证。") from error


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
    handshake_host = args.handshake_host or args.server_name
    deferred_handshake = handshake_host == args.server_name == public_domain
    if not deferred_handshake:
        verify_handshake(handshake_host, args.server_name)
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
    print("DNS、端口和 1Panel API 预检通过。", flush=True)
    if deferred_handshake:
        print("443 占位页作为 TLS 握手目标，将在 HTTPS 站点就绪后验证。", flush=True)
    if args.check:
        return
    cert_id = panel.certificate(args.domain, public_domain, acme_id, args.acme_email)
    options = [
        "--domain", args.domain, "--admin-port", str(admin_port), "--port", str(proxy_port),
        "--server-name", args.server_name, "--first-device", "默认设备",
    ]
    if args.handshake_host:
        options += ["--handshake-host", args.handshake_host]
    _, prefix, _ = deploy_service(args.image_archive, running, options, [
        f"https://{args.domain}:{admin_port}", str(proxy_port), args.server_name, handshake_host,
    ], public_port=19998)
    for domain, port, upstream in sites:
        panel.create_site(domain, port, upstream, args.website_group, cert_id)
    verify_site(f"https://{args.domain}:{admin_port}{prefix}/healthz", "ready\n")
    verify_site(f"https://{public_domain}/", "<title>小游戏</title>")
    if deferred_handshake:
        verify_handshake(handshake_host, args.server_name)
    print(f"yhl-ui 后台 https://{args.domain}:{admin_port}{prefix}/")
    print(f"代理 {args.domain}:{proxy_port}")
    print(f"443 网站 https://{public_domain}/")


def deploy_service(image_archive, running, init_options, expected, *, public_port=None, on_ready=None, requested_path=None):
    env = {**os.environ, "YHL_HTTP_PORT": "19999", "YHL_PUBLIC_PORT": str(public_port or "")}
    env["YHL_DOMAIN"] = urlsplit(expected[0]).hostname
    credentials = None
    if image_archive:
        command("docker", "load", "-i", str(image_archive.resolve()))
    else:
        command(*COMPOSE, "build", env=env)
    if not running:
        initialized = command(
            *COMPOSE, "run", "--rm", "--no-deps", "-T", "--entrypoint", "python",
            "yhl-ui", "-c", "from pathlib import Path; from storage import Store; "
            "raise SystemExit(not Store(Path('/data')).initialized)",
            check=False, env=env, stdin=subprocess.DEVNULL,
        )
        if initialized.returncode == 1:
            if "--generate-password" in init_options:
                credentials = bootstrap(env, "init", *init_options)
            else:
                command(*COMPOSE, "run", "--rm", "--no-deps", "yhl-ui", "init", *init_options, env=env)
        elif initialized.returncode:
            raise RuntimeError("无法检查 yhl-ui 持久化数据。")
    query = (
        "import sys; from pathlib import Path; from storage import Store; s=Store(Path('/data')).load(); "
        "actual=(s['origin'],str(s['port']),s['server_name'],s['handshake_host']); "
        "print(s.get('admin_path', '/admin')); "
        "sys.exit(0 if actual[:len(sys.argv)-1] == tuple(sys.argv[1:]) else 2)"
    )
    same = command(
        *COMPOSE, "run", "--rm", "--no-deps", "-T", "--entrypoint", "python",
        "yhl-ui", "-c", query, *expected, check=False, capture=True, env=env, stdin=subprocess.DEVNULL,
    )
    if same.returncode:
        raise RuntimeError("现有持久化配置与本次设置不符，请使用原设置重新运行安装器。")
    prefix = admin_path(same.stdout.strip() or DEFAULT_ADMIN_PATH)
    if requested_path is not None and prefix != requested_path:
        raise RuntimeError(f"已有的数据使用后台路径 {prefix}，升级会沿用该路径。")
    env["YHL_ADMIN_PATH"] = prefix
    previous = command(
        "docker", "inspect", "--format", "{{.Image}}", running, capture=True,
    ).stdout.strip() if running else None
    old_port = ""
    if running:
        old_command = json.loads(command(
            "docker", "inspect", "--format", "{{json .Config.Cmd}}", running, capture=True,
        ).stdout)
        if "--public-port" in old_command:
            old_port = old_command[old_command.index("--public-port") + 1]
        if public_port is None and old_port not in ("", "0"):
            public_port = int(old_port)
            env["YHL_PUBLIC_PORT"] = old_port
    try:
        command(*COMPOSE, "up", "-d", "--no-build", "--wait", "--wait-timeout", "45", env=env)
        verify_site("http://127.0.0.1:19999/healthz", "ready\n")
        if public_port is not None:
            verify_site(f"http://127.0.0.1:{public_port}/", "<title>小游戏</title>")
        if on_ready is not None:
            on_ready(prefix)
    except (KeyboardInterrupt, OSError, RuntimeError, subprocess.SubprocessError):
        if previous:
            original, staged = service_containers()
            if original and original.id == running:
                if staged:
                    recover_interrupted_service(original, staged)
                elif original.state != "running":
                    command("docker", "start", original.id)
                verify_site("http://127.0.0.1:19999/healthz", "ready\n")
                raise
            command("docker", "tag", previous, "yhl-ui:rollback")
            env["YHL_PUBLIC_PORT"] = old_port
            command(
                *COMPOSE, "up", "-d", "--no-build", "--wait", "--wait-timeout", "45",
                env={**env, "YHL_IMAGE": "yhl-ui:rollback"},
            )
        else:
            command(*COMPOSE, "stop", "yhl-ui", env=env)
        raise
    return env, prefix, credentials


def bootstrap(env, *args):
    result = command(
        *COMPOSE, "run", "--rm", "--no-deps", "-T", "yhl-ui", *args,
        env=env, capture=True, stdin=subprocess.DEVNULL,
    )
    return credentials_from(result.stdout)


def credentials_from(output):
    try:
        credentials = json.loads(output)
        if credentials.get("account") == "admin" and credentials.get("password"):
            return credentials
    except (ValueError, AttributeError):
        pass
    raise RuntimeError("未能读取新生成的管理员密码。")


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
