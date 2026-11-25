import argparse
import fcntl
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from dataclasses import dataclass
from urllib.request import ProxyHandler, build_opener

from app import ACCOUNT, DEFAULT_ADMIN_PATH, admin_path, hostname
from install import COMPOSE, command, credentials_from, deploy_service, port_available, verify_handshake, verify_site
from openresty import ADMIN_PORT, OpenResty
from panel import NoRedirect


VERSION = "0.1.10"
RELEASES = Path("/opt/yhl-ui/releases")


@dataclass
class Installed:
    id: str
    image: str
    image_id: str
    running: bool
    healthy: bool
    domain: str
    path: str


def installed_service():
    result = command(
        "docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={COMPOSE[3]}",
        "--filter", "label=com.docker.compose.service=yhl-ui",
        "--format", '{{.ID}} {{.Label "com.docker.compose.oneoff"}}', capture=True,
    ).stdout.splitlines()
    ids = [line.split()[0] for line in result if not line.strip().lower().endswith(" true")]
    if not ids:
        return None
    if len(ids) != 1:
        raise RuntimeError("有多个 yhl-ui 服务容器，请先检查 Docker Compose 项目。")
    fields = command(
        "docker", "inspect", "--format",
        '{{.Config.Image}}\t{{.Image}}\t{{.State.Running}}\t'
        '{{if .State.Health}}{{.State.Health.Status}}{{end}}\t'
        '{{index .Config.Labels "io.yhl-ui.domain"}}\t{{index .Config.Labels "io.yhl-ui.admin-path"}}',
        ids[0], capture=True,
    ).stdout.rstrip("\r\n").split("\t")
    image, image_id, running, health, domain, path = fields
    if not domain:
        read_domain = (
            "import sqlite3,json; from pathlib import Path; p=Path('/data/yhl-ui.sqlite3'); "
            "print(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)"
            ".execute(\"SELECT json_extract(settings, '$.domain') FROM node WHERE id = 1\").fetchone()[0] "
            "if p.is_file() else json.loads(Path('/data/state.json').read_text())['domain'])"
        )
        domain = command(
            "docker", "run", "--rm", "--network", "none", "--read-only",
            "--volumes-from", ids[0] + ":ro", "--entrypoint", "python", image_id, "-c", read_domain,
            capture=True, stdin=subprocess.DEVNULL,
        ).stdout.strip()
    return Installed(ids[0], image, image_id, running == "true", health == "healthy",
                     hostname(domain), admin_path(path or DEFAULT_ADMIN_PATH))


def reset_password(domain, path):
    current = installed_service()
    command("docker", "stop", current.id)
    try:
        result = command(
            "docker", "run", "--rm", "--network", "none", "--read-only", "--volumes-from", current.id,
            current.image_id, "reset-login", capture=True, stdin=subprocess.DEVNULL,
        )
        credentials = credentials_from(result.stdout)
    finally:
        command("docker", "start", current.id)
    verify_site("http://127.0.0.1:19999/healthz", "ready\n")
    verify_frontend(domain, path)
    for _ in range(25):
        health = command(
            "docker", "inspect", "--format", "{{.State.Health.Status}}", current.id, capture=True,
        ).stdout.strip()
        if health == "healthy":
            return credentials
        time.sleep(1)
    raise RuntimeError("密码已重置，但容器尚未通过 Docker 健康检查。")


def show_result(domain, path, credentials, title):
    terminal = sys.stdout.isatty()
    emphasis = "\033[1;36m" if terminal else ""
    secret = "\033[1;33m" if terminal else ""
    reset = "\033[0m" if terminal else ""
    print(f"\n{emphasis}{title} · yhl-ui v{VERSION}{reset}")
    print(f"后台地址  https://{domain}:{ADMIN_PORT}{path}/")
    print(f"登录账号  {ACCOUNT}")
    if credentials:
        print(f"{secret}管理员密码  {credentials['password']}{reset}")
        print("请保存管理员密码。验证器可在登录安全页面开启。")
    print(f"代理地址  {domain}:8080")
    print(f"443 网站  https://{domain}/")


def health_instance(url):
    with build_opener(ProxyHandler({}), NoRedirect()).open(url, timeout=3) as response:
        instance = response.headers.get("X-YHL-Instance", "")
        if response.status == 200 and response.read(32) == b"ready\n" and re.fullmatch(r"[a-f0-9]{32}", instance):
            return instance
    raise RuntimeError("健康检查响应无效。")


def verify_frontend(domain, path):
    expected = health_instance("http://127.0.0.1:19999/healthz")
    for _ in range(15):
        try:
            if health_instance(f"https://{domain}:{ADMIN_PORT}{path}/healthz") == expected:
                return
        except (OSError, RuntimeError):
            pass
        time.sleep(1)
    raise RuntimeError(f"{domain}:{ADMIN_PORT} 未连到本机 yhl-ui，请检查公网端口、防火墙和反代。")


def remove_release_files():
    if not RELEASES.is_dir():
        return
    if RELEASES.resolve() != RELEASES:
        raise RuntimeError("安装目录使用了符号链接，请手工检查。")
    for child in RELEASES.iterdir():
        if (re.fullmatch(r"\d+\.\d+\.\d+", child.name) and child.is_dir()
                and not child.is_symlink() and child.resolve().parent == RELEASES
                and all((child / name).is_file() for name in ("setup.sh", "compose.yaml", "install_existing.py"))):
            shutil.rmtree(child)
    if not any(RELEASES.iterdir()):
        RELEASES.rmdir()
    if not any(RELEASES.parent.iterdir()):
        RELEASES.parent.rmdir()


def uninstall(domain):
    if input("确认完整卸载 yhl-ui？管理员、设备和订阅数据将永久删除。[y/N] ").strip().lower() != "y":
        print("已取消卸载。")
        return
    if domain:
        installed = installed_service()
        if installed and domain != installed.domain:
            raise RuntimeError("指定域名与当前安装不符。")
    volume = f"{COMPOSE[3]}_yhl-data"
    names = command("docker", "volume", "ls", "--format", "{{.Name}}", capture=True).stdout.splitlines()
    if volume in names:
        owned = command(
            "docker", "volume", "ls", "--filter", f"label=com.docker.compose.project={COMPOSE[3]}",
            "--filter", "label=com.docker.compose.volume=yhl-data", "--format", "{{.Name}}", capture=True,
        ).stdout.splitlines()
        if volume not in owned:
            raise RuntimeError(f"无法确认 {volume} 属于 yhl-ui，已中断卸载。")
    OpenResty(domain, removing=True).remove_managed()
    command(*COMPOSE, "down", "--remove-orphans")
    if volume in names:
        command("docker", "volume", "rm", volume)
    images = command(
        "docker", "image", "ls", "yhl-ui", "--format", "{{.Repository}}:{{.Tag}}", capture=True,
    ).stdout.splitlines()
    for image in images:
        if re.fullmatch(r"yhl-ui:(?:\d+\.\d+\.\d+|rollback)", image):
            command("docker", "image", "rm", image)
    remove_release_files()
    print("yhl-ui 已完整卸载，SQLite 数据已删除。1Panel 的 443 网站和证书已保留。")


def install(args):
    command(*COMPOSE, "version")
    if args.uninstall:
        uninstall(args.domain)
        return
    installed = installed_service()
    if installed and args.domain and args.domain != installed.domain:
        raise RuntimeError("指定域名与当前安装不符。")
    if args.update and not installed:
        raise RuntimeError("未检测到已安装的 yhl-ui，请先执行安装。")
    if installed and args.admin_path and args.admin_path != installed.path:
        raise RuntimeError(f"更新会沿用已配置的后台路径 {installed.path}。")
    domain = installed.domain if installed else args.domain or hostname(input("已在 1Panel 配好 HTTPS 的域名: ").strip())
    configured_path = args.admin_path
    if not installed and not args.check and configured_path is None:
        entered = input(f"后台路径 [{DEFAULT_ADMIN_PATH}]: ").strip()
        configured_path = admin_path(entered) if entered else None
    prefix = configured_path or DEFAULT_ADMIN_PATH
    if installed:
        print(f"已安装镜像 {installed.image}，发布版本 v{VERSION}。", flush=True)
    if installed and installed.running and installed.healthy and installed.image == f"yhl-ui:{VERSION}" and not args.check:
        credentials = reset_password(domain, installed.path) if args.reset_password_only else None
        show_result(domain, installed.path, credentials, "已重置登录" if credentials else "已是最新版本")
        return
    frontend = OpenResty(domain)
    addresses = {row[4][0] for row in socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)}
    if len(addresses) != 1 or ":" in next(iter(addresses)):
        raise RuntimeError(f"{domain} 须直连此节点的单个 IPv4 地址。")
    local = verify_handshake("127.0.0.1", domain)
    if verify_handshake(next(iter(addresses)), domain) != local:
        raise RuntimeError(f"{domain}:443 和本机 OpenResty 提供的证书不同，请检查 DNS 是否直连此节点。")
    frontend.check_port()
    running = installed.id if installed and installed.running else ""
    if not running:
        port_available("0.0.0.0", 8080)
        port_available("127.0.0.1", 19999)
    print("1Panel OpenResty、443 站点证书和本机端口预检通过。", flush=True)
    if args.check:
        return

    def on_ready(admin_prefix):
        with frontend.activate():
            verify_frontend(domain, admin_prefix)

    origin = f"https://{domain}:{ADMIN_PORT}"
    options = [
        "--domain", domain, "--server-name", domain, "--admin-port", str(ADMIN_PORT),
        "--admin-path", prefix, "--first-device", "默认设备", "--generate-password",
    ]
    _, prefix, credentials = deploy_service(
        args.image_archive, running, options, [origin, "8080"],
        on_ready=on_ready, requested_path=configured_path,
    )
    if args.reset_password_only and credentials is None:
        credentials = reset_password(domain, prefix)
    show_result(domain, prefix, credentials, "更新完成" if installed else "安装完成")


def main():
    parser = argparse.ArgumentParser(description="复用 1Panel 站点证书安装 yhl-ui")
    parser.add_argument("domain", nargs="?", type=hostname)
    parser.add_argument("--admin-path", type=admin_path)
    parser.add_argument("--image-archive", type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="仅验证环境和证书")
    action.add_argument("--update", action="store_true", help="更新已有安装")
    action.add_argument("--uninstall", action="store_true", help="完整卸载并删除本机 yhl-ui 数据")
    parser.add_argument("--reset-password-only", action="store_true", help="重置管理员密码并关闭验证器")
    args = parser.parse_args()
    if args.reset_password_only and (args.check or args.uninstall):
        parser.error("重置登录须在安装或更新时执行。")
    if args.image_archive and not args.image_archive.is_file():
        parser.error("找不到 Docker 镜像归档。")
    try:
        with open("/run/lock/yhl-ui.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            install(args)
    except (argparse.ArgumentTypeError, EOFError, RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
        parser.exit(1, f"yhl-ui 安装中断：{error}\n")


if __name__ == "__main__":
    os.umask(0o077)
    main()
