import hashlib
import ipaddress
import json
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def confirm_dns(domain, expected):
    try:
        addresses = {ipaddress.ip_address(record[4][0]) for record in socket.getaddrinfo(
            domain, 443, type=socket.SOCK_STREAM,
        )}
    except socket.gaierror as error:
        raise RuntimeError(f"{domain} 无法解析，已中断安装。") from error
    if addresses != {expected}:
        raise RuntimeError(f"{domain} 未唯一解析到 {expected}，已中断安装。")


def website_alias(domain, port):
    return "yhl-ui-" + hashlib.sha256(f"{domain}:{port}".encode()).hexdigest()[:12]


def get_http(url, timeout=8):
    with build_opener(ProxyHandler({}), NoRedirect()).open(url, timeout=timeout) as response:
        return response.read(65536).decode()


class Panel:
    def __init__(self, url, key):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment
        ):
            raise ValueError("1Panel 地址须包含协议、主机及端口。")
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("请在服务器上用 1Panel 本机 HTTP 地址，远程 API 需要可信 HTTPS。")
        if not key:
            raise ValueError("请输入 1Panel API Key。")
        self.url = url.rstrip("/") + "/api/v2"
        self.key = key
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path, body=None):
        timestamp = str(int(time.time()))
        token = hashlib.md5(f"1panel{self.key}{timestamp}".encode(), usedforsecurity=False).hexdigest()
        req = Request(
            self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "1Panel-Token": token,
                "1Panel-Timestamp": timestamp,
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener.open(req, timeout=30) as response:
                result = json.loads(response.read(4 * 1024**2))
        except (HTTPError, URLError, OSError, ValueError) as error:
            raise RuntimeError(f"1Panel 请求失败，接口 {path}，请检查版本、权限和运行状态。") from error
        if not isinstance(result, dict) or result.get("code") != 200:
            raise RuntimeError(f"1Panel 拒绝接口 {path}，请查看 1Panel 操作日志。")
        return result.get("data")

    def sites(self):
        return self.request("/websites/list") or []

    def preflight(self, sites, acme_id, email):
        if self.request("/websites/check", {"installIds": []}):
            raise RuntimeError("1Panel 的 OpenResty 尚未安装或未运行。")
        accounts = self.request("/websites/acme/search", {"page": 1, "pageSize": 100})
        accounts = (accounts or {}).get("items") or []
        account = next((item for item in accounts if
                        item.get("id") == acme_id or (email and item.get("email") == email
                                                     and item.get("type") == "letsencrypt")), None)
        if acme_id and account is None:
            raise RuntimeError("找不到指定的 1Panel ACME 账户。")
        existing = self.sites()
        for domain, port, upstream in sites:
            self.find_site(existing, domain, port, upstream, inspect_domains=True)
        return account["id"] if account is not None else None

    def find_site(self, sites, domain, port, upstream, inspect_domains=False):
        alias = website_alias(domain, port)
        for site in sites:
            if site["alias"] == alias:
                if (
                    site.get("type") != "proxy" or site.get("protocol") != "HTTPS"
                    or site.get("proxy") != upstream or site.get("remark") != "yhl-ui"
                ):
                    raise RuntimeError(f"网站别名 {alias} 已被其他配置使用。")
                domains = self.request(f'/websites/domains/{site["id"]}') or []
                if not any(
                    row.get("domain") == domain and row.get("port") == port and row.get("ssl")
                    for row in domains
                ):
                    raise RuntimeError(f"网站 {alias} 的 HTTPS 域名或端口不匹配。")
                return site
            if inspect_domains and site.get("protocol") != "TCP/UDP":
                domains = self.request(f'/websites/domains/{site["id"]}') or []
                if any(row.get("domain") == domain and row.get("port") == port for row in domains):
                    raise RuntimeError(f"{domain}:{port} 已由其他网站管理。")
        return None

    def certificate(self, admin, public, acme_id, email):
        if acme_id is None:
            account = self.request("/websites/acme", {
                "email": email, "type": "letsencrypt", "keyType": "EC256",
            })
            acme_id = account["id"]
        other = public if public != admin else ""
        marker = f"yhl-ui:{admin}:{other}"
        records = self.request("/websites/ssl/search", {
            "page": 1, "pageSize": 100, "domain": admin,
        })
        records = (records or {}).get("items") or []
        existing = next((cert for cert in records if cert.get("description") == marker), None)
        if existing is None:
            existing = self.request("/websites/ssl", {
                "primaryDomain": admin, "otherDomains": other, "provider": "http",
                "acmeAccountId": acme_id, "autoRenew": True, "keyType": "EC256",
                "apply": True, "description": marker,
            })
        elif existing.get("status") in ("error", "applyError"):
            self.request("/websites/ssl/obtain", {"ID": existing["id"]})
        cert_id = existing["id"]
        for _ in range(60):
            current = self.request(f"/websites/ssl/{cert_id}")
            status = current.get("status")
            if status == "ready":
                return cert_id
            if status in ("error", "applyError"):
                raise RuntimeError(f"1Panel 签发证书失败（编号 {cert_id}），请检查公网 80 及面板证书日志。")
            time.sleep(2)
        raise RuntimeError(f"证书仍在签发（编号 {cert_id}），可重新运行安装器继续。")

    def create_site(self, domain, port, upstream, group_id, cert_id):
        site = self.find_site(self.sites(), domain, port, upstream, inspect_domains=True)
        if site is None:
            self.request("/websites", {
                "alias": website_alias(domain, port), "remark": "yhl-ui", "type": "proxy",
                "appType": "new", "proxy": upstream, "webSiteGroupID": group_id,
                "domains": [{"domain": domain, "port": port, "ssl": True}],
                "enableSSL": True, "websiteSSLID": cert_id,
            })
            for _ in range(20):
                site = self.find_site(self.sites(), domain, port, upstream)
                if site is not None:
                    break
                time.sleep(1)
            else:
                raise RuntimeError(f"1Panel 尚未完成 {domain}:{port} 的网站创建。")
        if port != 443:
            for log_type in ("access.log", "error.log"):
                self.request("/websites/log/operate", {
                    "id": site["id"], "operate": "disable", "logType": log_type,
                })
