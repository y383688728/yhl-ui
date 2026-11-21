# yhl-ui

yhl-ui 是自用的单节点代理管理程序。它从 sing-box 1.14.4 的官方源码构建代理和流量统计服务，支持 VLESS、TCP、REALITY 和 Vision，并为每台设备生成独立的 Mihomo 订阅。新装时使用账号和密码登录，可在后台按需启用 TOTP 验证器。

## 端口

| 端口 | 用途 |
| --- | --- |
| 8080/TCP | sing-box 代理 |
| 9999/TCP | 1Panel OpenResty 提供的 HTTPS 管理后台和订阅 |
| 443/TCP | 1Panel 中已有的 HTTPS 网站，也作为新安装的 REALITY 握手目标 |
| 19999/TCP | 后台的本机 HTTP 服务，仅监听回环地址 |

订阅是持有链接即可读取的设备凭据，请通过私人渠道导入 Mihomo。可以复制链接，或打开该设备的「Mihomo 订阅码」扫码获取链接。使用 Shadowrocket 扫码导入单个节点时，打开「小火箭节点码」。撤销设备会使该设备的订阅和节点凭据失效，重新加载代理配置会短暂中断现有代理连接。

## 分流规则与流量

在后台打开「Clash 分流规则」，粘贴 `rules:` YAML 段，保存后让各设备刷新 Mihomo 订阅。规则按输入顺序下发，`PROXY` 映射到「节点」。还可以使用 `DIRECT`、`REJECT` 等 Mihomo 内置策略。没有 `MATCH` 时会追加 `MATCH,节点`，清空后保存会恢复默认规则。粘贴区域接受规则列表，引用 `RULE-SET` 或 `SUB-RULE` 的条目需要另备定义。小火箭节点码只携带节点配置，Shadowrocket 的分流规则在客户端管理。

每条订阅的「流量详情」显示自开启统计后的代理上传、下载和合计。共用一个订阅的设备按同一 UUID 累计。采样每 5 秒写入 SQLite，正常关停前会补采样。历史流量没有回填，节点异常退出时可能丢失最近一次采样间隔内的统计。

## 安装

在目标 Linux 节点上准备 Docker Engine、Compose 插件、Python 3.10 或以上，并在 1Panel 安装和启动 OpenResty，使用应用商店默认的 host 网络。域名需要直接解析到本节点的公网 IPv4。请在 1Panel 建好该域名的 443 网站，绑定已签发的可信证书并开启自动续期。443 需要支持 TLS 1.3。节点防火墙和云安全组需允许 TCP 8080、9999 和 443。

在服务器的交互式 CLI 粘贴一行打开管理菜单，再选择「安装或更新」。首次安装输入域名和后台路径，路径回车默认 `/admin`，输入 `yhlui` 会使用 `/yhlui`。以 root 登录时可将 `sudo bash` 改成 `bash`。

```sh
curl -fsSL https://github.com/y383688728/yhl-ui/releases/latest/download/setup.sh | sudo bash
```

同一条命令也用于更新。引导程序从 GitHub 获取最新稳定版，检测到已安装时沿用域名和路径。版本已最新且运行健康时跳过重装。发布包经过 SHA256 校验，文件放在 `/opt/yhl-ui/releases/<版本>`。x86-64 使用预构建镜像，ARM64 使用同一版本源码在节点本地构建。

安装器从 1Panel OpenResty 的网站配置识别证书路径，为 9999 创建独立的反向代理配置，引用 443 站点使用的同一套证书文件。1Panel 续期更新网站证书并重载 OpenResty 后，9999 随之使用新证书。默认安装无需 1Panel API Key、DNS 服务商凭据或 Redis。安装通过健康检查后，CLI 显示后台完整地址、账号 `admin` 和随机管理员密码，请妥善保存。在「登录安全」中可以配置验证器。

后台地址包含配置的路径，例如 `https://node.example.com:9999/admin/`。后台顶部的「登录安全」可修改管理员密码并管理验证器。修改密码后需重新登录。升级后旧订阅链接仍可使用，已有验证器状态会随升级保留。需要切换到密码登录并生成新密码时，可以选择菜单第 2 项，或直接运行下面命令。

```sh
curl -fsSL https://github.com/y383688728/yhl-ui/releases/latest/download/setup.sh | sudo bash -s -- --update --reset-password-only
```

可运行 `python3 install_existing.py node.example.com --check` 只检查服务器条件。AMD64 离线安装可以用 `--image-archive /path/to/yhl-ui-image.tar.gz` 指定发布包里的镜像。源码中仍保留 `python3 install.py --help` 的 1Panel API 安装入口。

安装成功后，用默认设备的订阅验证代理。安装器会检查 9999 是否转发到本次启动的实例。已有可用反代时会直接复用。新建配置后验证失败时，安装器会撤回本次新增的配置。

## 维护

卸载服务时选择菜单第 3 项，或运行下面的命令，然后确认。完整卸载会永久删除 SQLite 卷中的管理员、设备和订阅数据，同时移除 yhl-ui 容器、安装镜像、已下载版本文件和自己写入的 9999 配置。1Panel 的 443 网站和证书继续由 1Panel 管理。重装时可以重新填写后台路径，并会生成新的登录密码和订阅。

```sh
curl -fsSL https://github.com/y383688728/yhl-ui/releases/latest/download/setup.sh | sudo bash -s -- --uninstall
```

在项目目录用 `docker compose -p yhl-ui ps` 查看运行状态。账户、设备和订阅存储在 Docker 卷 `yhl-ui_yhl-data` 内的 `yhl-ui.sqlite3`。升级会自动导入旧版本的 JSON 数据。升级前请停止服务或使用支持一致性快照的卷备份方案保存该卷，并保留当前镜像 ID。备份包含敏感数据，限制读取权限。

安装器会在新版容器启动或验收失败时尝试恢复原镜像。手动降级到 v0.1.0 或 v0.1.1 时，请使用升级前的卷备份，因为旧镜像只读取 JSON。旧版的 443 网站如果转发到本机占位页，重建旧服务时设置 `YHL_PUBLIC_PORT=19998`。

Docker 构建会校验 sing-box 源码归档的 SHA256，并启用 V2Ray 用户流量统计。上游发行包、源代码和许可证可在 [sing-box v1.14.4](https://github.com/SagerNet/sing-box/releases/tag/v1.14.4) 查看。安装器的站点和证书接口以 [1Panel v2.5.0](https://github.com/1Panel-dev/1Panel/tree/v2.5.0) 为适配目标。
