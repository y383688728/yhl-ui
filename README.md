# yhl-ui

yhl-ui 是自用的单节点代理管理程序。它使用官方 sing-box 1.14.4，支持 VLESS、TCP、REALITY 和 Vision，并为每台设备生成独立的 Mihomo 订阅。管理后台采用密码和 TOTP 双因素登录。

## 端口

| 端口 | 用途 |
| --- | --- |
| 8080/TCP | sing-box 代理 |
| 9999/TCP | 1Panel OpenResty 提供的 HTTPS 管理后台和订阅 |
| 443/TCP | 1Panel 中已有的 HTTPS 网站，也作为新安装的 REALITY 握手目标 |
| 19999/TCP | 后台的本机 HTTP 服务，仅监听回环地址 |

订阅是持有链接即可读取的设备凭据，请通过私人渠道导入 Mihomo。撤销设备会关闭旧订阅并重新加载代理配置，现有代理连接会短暂中断。

## 安装

在目标 Linux 节点上准备 Docker Engine、Compose 插件、Python 3.10 或以上，并在 1Panel 安装和启动 OpenResty，使用应用商店默认的 host 网络。域名需要直接解析到本节点的公网 IPv4。请在 1Panel 建好该域名的 443 网站，绑定已签发的可信证书并开启自动续期。443 需要支持 TLS 1.3。节点防火墙和云安全组需允许 TCP 8080、9999 和 443。

在服务器的交互式 CLI 粘贴一行，然后输入域名。使用 root 登录时可将 `sudo bash` 改成 `bash`。

```sh
curl -fsSL https://github.com/y383688728/yhl-ui/releases/download/v0.1.2/setup.sh | sudo bash
```

引导程序验证发布包的 SHA256，并将安装文件放在 `/opt/yhl-ui/releases/0.1.2`。x86-64 使用预构建镜像，ARM64 使用同一版本源码在节点本地构建。

安装器从 1Panel OpenResty 的网站配置识别证书路径，为 9999 创建独立的反向代理配置，引用 443 站点使用的同一套证书文件。1Panel 续期更新网站证书并重载 OpenResty 后，9999 随之使用新证书。默认安装无需 1Panel API Key、DNS 服务商凭据或 Redis。管理员密码和 TOTP 设置密钥在首次安装时自动生成并显示，请录入验证器并妥善保存。

可运行 `python3 install_existing.py node.example.com --check` 只检查服务器条件。AMD64 离线安装可以用 `--image-archive /path/to/yhl-ui-image.tar.gz` 指定发布包里的镜像。源码中仍保留 `python3 install.py --help` 的 1Panel API 安装入口。

安装成功后，用默认设备的订阅验证代理。安装器会检查 9999 是否转发到本次启动的实例。已有可用反代时会直接复用。新建配置后验证失败时，安装器会撤回本次新增的配置。

## 维护

在项目目录用 `docker compose -p yhl-ui ps` 查看运行状态。账户、设备和订阅存储在 Docker 卷 `yhl-ui_yhl-data` 内的 `yhl-ui.sqlite3`。升级会自动导入旧版本的 JSON 数据。升级前请停止服务或使用支持一致性快照的卷备份方案保存该卷，并保留当前镜像 ID。备份包含敏感数据，限制读取权限。

安装器会在新版容器启动或验收失败时尝试恢复原镜像。手动降级到 v0.1.0 或 v0.1.1 时，请使用升级前的卷备份，因为旧镜像只读取 JSON。旧版的 443 网站如果转发到本机占位页，重建旧服务时设置 `YHL_PUBLIC_PORT=19998`。

Docker 构建会校验官方 sing-box 二进制归档的 SHA256。上游发行包、源代码和许可证可在 [sing-box v1.14.4](https://github.com/SagerNet/sing-box/releases/tag/v1.14.4) 查看。安装器的站点和证书接口以 [1Panel v2.5.0](https://github.com/1Panel-dev/1Panel/tree/v2.5.0) 为适配目标。
