# yhl-ui

yhl-ui 是自用的单节点代理管理程序。它使用官方 sing-box 1.14.4，支持 VLESS、TCP、REALITY 和 Vision，并为每台设备生成独立的 Mihomo 订阅。管理后台采用密码和 TOTP 双因素登录。

## 端口

| 端口 | 用途 |
| --- | --- |
| 8080/TCP | sing-box 代理 |
| 9999/TCP | 1Panel OpenResty 提供的 HTTPS 管理后台和订阅 |
| 443/TCP | OpenResty 提供的空白小游戏占位页 |
| 80/TCP | 1Panel 自动签发及续期证书的 HTTP 验证 |
| 19999、19998/TCP | 后台与占位页的本机 HTTP 服务，仅监听回环地址 |

代理和管理 HTTPS 端口可在首次安装时指定。订阅是持有链接即可读取的设备凭据，请通过私人渠道导入 Mihomo。撤销设备会关闭旧订阅并重新加载代理配置，现有代理连接会短暂中断。

## 安装

在目标 Linux 节点上准备 Docker Engine、Compose 插件、Python 3.10 或以上及带 OpenResty 的 1Panel v2.5。后台域名和占位页域名都需要直接解析到该节点的公网 IPv4。1Panel API Key 需要允许服务器本机地址调用网站、OpenResty 检查和证书接口。公网防火墙需允许证书验证和所需端口。

在服务器的交互式 CLI 粘贴一行，跟随提示完成安装。使用 root 登录时可将 `sudo bash` 改成 `bash`。

```sh
curl -fsSL https://github.com/y383688728/yhl-ui/releases/download/v0.1.1/setup.sh | sudo bash
```

引导程序验证发布包的 SHA256，并将安装文件放在 `/opt/yhl-ui/releases/0.1.1`。x86-64 使用预构建镜像，ARM64 使用同一版本源码在节点本地构建。

如需直接指定参数，在源码或发布包目录运行下方命令。替换示例域名、地址和邮箱。REALITY 的目标须支持 TLS 1.3，安装器会按指定的 SNI 验证证书。

```sh
python3 install.py --domain node.example.com --public-ip 192.0.2.10 \
  --server-name tls.example.com --acme-email admin@example.com \
  --panel-url http://127.0.0.1:8888
```

安装器从终端读取 1Panel API Key。首次安装时按提示设置管理员密码并录入验证器，密码留空会自动生成。验证器设置密钥只在初始化时显示，请保存到安全位置。安装成功后，用默认设备的订阅验证代理，再按需添加或撤销设备。

- 增加 `--check` 可只检查 DNS、1Panel API、握手目标和端口。
- 用 `--public-domain play.example.com` 可给 443 占位页使用独立域名。省略时复用后台域名。
- 选择 443 占位页作为 REALITY 握手目标时，安装器会在该站点启用 HTTPS 后再验证 TLS 1.3。
- 已有 ACME 账户时，可用 `--acme-account ID` 替代 `--acme-email`。这里的 ID 为数字，证书通过 HTTP-01 验证。
- 使用预构建镜像时，从 AMD64 发布包中解出 `yhl-ui-image.tar.gz`，再用 `--image-archive /path/to/yhl-ui-image.tar.gz` 指定该镜像归档。
- 安装器遇到已被其他站点占用的域名和端口会中断。同一设置重试时，会复用本项目已创建的网站和证书。

## 维护

在项目目录用 `docker compose -p yhl-ui ps` 查看运行状态。账户、设备、订阅及核心配置存储在 Docker 卷 `yhl-ui_yhl-data`。发布和升级前，停止服务或使用支持一致性快照的卷备份方案保存该卷，并保留当前镜像 ID。备份包含敏感数据，限制读取权限。

安装器会在新版容器启动或本机健康校验失败时尝试恢复原镜像。手动回退时，将已保留的镜像标记为 `yhl-ui:rollback`，再用 `YHL_IMAGE=yhl-ui:rollback docker compose -p yhl-ui up -d --no-build` 启动。当前版本的数据文件与镜像分开保存。

Docker 构建会校验官方 sing-box 二进制归档的 SHA256。上游发行包、源代码和许可证可在 [sing-box v1.14.4](https://github.com/SagerNet/sing-box/releases/tag/v1.14.4) 查看。安装器的站点和证书接口以 [1Panel v2.5.0](https://github.com/1Panel-dev/1Panel/tree/v2.5.0) 为适配目标。
