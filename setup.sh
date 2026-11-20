#!/usr/bin/env bash
set -euo pipefail
umask 022

version=0.1.2
release="https://github.com/y383688728/yhl-ui/releases/download/v$version"
install_dir="/opt/yhl-ui/releases/$version"

if (( EUID != 0 )); then
    printf '请通过 sudo bash 或以 root 用户运行。\n' >&2
    exit 1
fi
if ! { true </dev/tty; } 2>/dev/null; then
    printf '请在有交互终端的服务器会话中运行安装。\n' >&2
    exit 1
fi
for dependency in curl sha256sum tar python3 docker; do
    command -v "$dependency" >/dev/null || { printf '缺少命令 %s。\n' "$dependency" >&2; exit 1; }
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || {
    printf '需要 Python 3.10 或以上。\n' >&2
    exit 1
}
docker compose version >/dev/null

case "$(uname -m)" in
    x86_64) archive="yhl-ui-$version-linux-amd64.tar.gz"; image_archive="yhl-ui-image.tar.gz" ;;
    aarch64) archive="yhl-ui-$version-source.tar.gz"; image_archive="" ;;
    *) printf '当前架构不受支持。\n' >&2; exit 1 ;;
esac

download() {
    curl --fail --silent --show-error --location --retry 2 --max-time 180 \
        --proto '=https' --proto-redir '=https' "$1" -o "$2"
}

tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
download "$release/$archive" "$tmp/$archive"
download "$release/$archive.sha256" "$tmp/$archive.sha256"
read -r digest _ < "$tmp/$archive.sha256"
[[ "$digest" =~ ^[[:xdigit:]]{64}$ ]] || { printf '发布包校验文件格式无效。\n' >&2; exit 1; }
printf '%s  %s\n' "$digest" "$tmp/$archive" | sha256sum --check --status || {
    printf '发布包 SHA256 校验失败。\n' >&2
    exit 1
}
mkdir -p -- "$install_dir"
tar -xzf "$tmp/$archive" -C "$install_dir" --no-same-owner --no-same-permissions
cd -- "$install_dir"

if (( $# == 0 )); then
    read -r -p '已在 1Panel 配好 HTTPS 的域名: ' domain </dev/tty
    set -- "$domain"
fi
if [[ -n "$image_archive" ]]; then
    set -- "$@" --image-archive "$install_dir/$image_archive"
fi
PYTHONUTF8=1 python3 install_existing.py "$@" </dev/tty
