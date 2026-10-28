# 多用户 Linux 预览部署

[English](DEPLOYMENT_MULTIUSER.md) · [默认本地部署](DEPLOYMENT.md)

此版本面向具备系统管理经验的部署者，尚非开箱即用的公网安装流程。设计目标是在一台 Linux 主机上支持 2–5 位受邀用户。默认 Docker Compose 仍为单用户部署。安装前请按自身环境检查服务单元、磁盘配额、出站限制和反向代理配置。

## 前提

- Linux、systemd、Python 3.12、Caddy 或等效 HTTPS 反向代理、可容纳每用户独立 5 GiB 镜像的磁盘空间，以及管理员权限。
- 有效 HTTPS 域名。公网只开放 80/443；网关仅监听 `127.0.0.1:18780`，租户 API 通过 Unix socket 连接。
- 如迁移现有单用户安装，先分别安全备份数据库、下载文件、凭据 vault 和密钥；不得提交到 Git。

## 准备公网地址

把 `ops/azure/Caddyfile.watcher` 中的 `watcher.example.com` 改成自己的域名，将站点块加入 Caddy 配置。若使用 Cloudflare，检查 `ops/azure/Caddyfile.cloudflare-global` 与其中的 IP 段是否仍适用。确保 HTTP-01 证书验证路径可达，或改用 DNS-01；处理真实账号前先检查 TLS 与反向代理。

在 Linux 主机上，运行安装器前创建公网环境文件：

```sh
sudo install -d -m 0755 /etc/academic-watcher
sudo install -m 0644 ops/azure/public.env.example /etc/academic-watcher/public.env
sudoedit /etc/academic-watcher/public.env
```

将 `AW_GATEWAY_PUBLIC_ORIGIN` 设为精确的 HTTPS 来源地址，不含 `/watcher` 或其他路径。安装器会拒绝示例域名。网关与供应器服务都读取此文件。

## 构建与安装源码

前端构建需要 `VITE_PUBLIC_BASE=/watcher` 和 `VITE_ACCOUNT_URL=/watcher/account`。安装脚本要求一个包含 `backend/`、`identity-gateway/`、`frontend-dist/` 与 `ops/azure/` 的部署包。安装前准备前端依赖，再从仓库构建部署包：

```sh
cd frontend && npm ci && VITE_PUBLIC_BASE=/watcher VITE_ACCOUNT_URL=/watcher/account npm run build && cd ..
mkdir -p bundle/ops
cp -a backend identity-gateway bundle/
cp -a frontend/dist bundle/frontend-dist
cp -a ops/azure bundle/ops/
sudo sh bundle/ops/azure/install.sh "$PWD/bundle"
```

安装器负责网关服务、供应器 timer 与后端依赖，不会修改 Caddy，也不会初始化首位账号。从已有单用户安装迁移时，应先审查 `ops/azure/import_owner.py`，并以 `--username`、`--email` 显式指定原账号；该脚本要求准备好的旧实例快照，且会执行特权写入。**全新**多用户安装的首位管理员初始化尚未自动化。

配置完成后，分别检查 `aw-gateway`、`aw-provision.timer`、租户服务、HTTPS 登录、匿名 API 拒绝、租户隔离、文件下载及备份恢复。登录页健康不代表外部 Canvas 或课程网站凭据可用；各来源还需重新登录并验证真实同步。
