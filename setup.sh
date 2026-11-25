#!/usr/bin/env bash
set -euo pipefail
umask 022

version=0.1.11
release="https://github.com/yhlapple15/yhl-ui/releases/download/v$version"
install_dir="/opt/yhl-ui/releases/$version"

if (( EUID != 0 )); then
    printf '请通过 sudo bash 或以 root 用户运行。\n' >&2
    exit 1
fi
if [[ $# -eq 1 && $1 == --update ]]; then
    input=/dev/null
elif { true </dev/tty; } 2>/dev/null; then
    input=/dev/tty
else
    printf '请在有交互终端的服务器会话中运行。\n' >&2
    exit 1
fi

if (( $# == 0 )); then
    printf '\nyhl-ui 管理菜单 · GitHub v%s\n' "$version"
    printf '1) 安装或更新\n2) 更新并重置管理员密码，关闭验证器\n3) 完整卸载\n0) 退出\n'
    while true; do
        read -r -p '请选择 [0–3]：' choice </dev/tty || exit 0
        case "$choice" in
            1) break ;;
            2) set -- --update --reset-password-only; break ;;
            3) set -- --uninstall; break ;;
            0) exit 0 ;;
            *) printf '请输入 0、1、2 或 3。\n' ;;
        esac
    done
fi

for dependency in curl sha256sum tar python3 docker; do
    command -v "$dependency" >/dev/null || { printf '缺少命令 %s。\n' "$dependency" >&2; exit 1; }
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || {
    printf '需要 Python 3.10 或以上。\n' >&2
    exit 1
}
docker compose version >/dev/null
installed_image=$(docker ps --filter label=com.docker.compose.project=yhl-ui \
    --filter label=com.docker.compose.service=yhl-ui --filter health=healthy --format '{{.Image}}')
printf 'GitHub 发布版本 v%s\n' "$version"

case "$(uname -m)" in
    x86_64) archive="yhl-ui-$version-linux-amd64.tar.gz"; image_archive="yhl-ui-image.tar.gz" ;;
    aarch64) archive="yhl-ui-$version-source.tar.gz"; image_archive="" ;;
    *) printf '当前架构不受支持。\n' >&2; exit 1 ;;
esac
uninstall=false
for argument in "$@"; do
    if [[ "$argument" == "--uninstall" ]]; then
        uninstall=true
    fi
done
if [[ "$installed_image" == "yhl-ui:$version" || "$uninstall" == true ]]; then
    archive="yhl-ui-$version-source.tar.gz"
    image_archive=""
fi

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
if [[ "$uninstall" == true ]]; then
    install_dir="$tmp/source"
fi
mkdir -p -- "$install_dir"
tar -xzf "$tmp/$archive" -C "$install_dir" --no-same-owner --no-same-permissions
cd -- "$install_dir"

if [[ -n "$image_archive" ]]; then
    set -- "$@" --image-archive "$install_dir/$image_archive"
fi
PYTHONUTF8=1 python3 install_existing.py "$@" <"$input"
