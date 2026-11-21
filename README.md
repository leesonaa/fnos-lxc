# fnos-lxc — 飞牛原生 LXC 管理

**原生 LXC**（`lxc-create` / `lxc-start` …），不是 Incus/LXD。  
`platform=all`，x86 / ARM 均可。

## 用户流程

1. 用 `fnpack build` 打出 `fnos-lxc.fpk`，在飞牛「应用中心 → 手动安装」
2. 打开应用 → **分步初始化向导**
   - 安装 `lxc` 等 apt 包
   - 选择数据卷路径（如 `/vol1/1000/lxc`，勿用系统盘）
   - 选择网桥（飞牛网络设置里先开启 OVS，选 `*-ovs`）
   - 网桥下拉旁有「刷新」：在飞牛网络设置里新建 OVS 后点一下即可，无需重开向导
3. 进入管理界面：列表（发行版图标、IPv4 / IPv6）、启停杀冻、删除、添加容器、**终端**、**配置表单**

列表每 4 秒自动刷新，显示 CPU 占用、内存（已用 / 上限）、地址、累计流量、运行时长和 PID（读取 cgroup v2 与 `/proc`，取不到时显示 `-`）；开机自启可直接点开关切换；强制停止、冻结、删除收在「更多」菜单里；右上角可在「列表」和「卡片」视图间切换。

## 容器配置表单

每个容器的「配置」按钮：主机名、开机自启、网桥/MAC、IPv4 / IPv6（DHCP 或静态）、CPU / 内存上限、
TUN/TAP、FUSE、GPU 直通（/dev/dri）、Docker 嵌套、目录挂载；另有「高级：原始配置」直接编辑 config。
表单项写入 config 末尾的受管区块（`# >>> fnos-lxc managed >>>`），每次保存前备份为 `config.fnos-bak`。
IPv4 设为静态时，会同时关闭容器内 eth0 的 DHCP（ifupdown / systemd-networkd / netplan，原文件备份为 `*.fnos-orig`，改回 DHCP 自动还原），避免出现额外的自动分配地址；保存后需重启容器生效。

**开机自启**：飞牛开机后本应用启动时，会等待数据卷目录和所选网桥就绪（最多 10 分钟），再对勾选了「开机自启」的容器执行一次 `lxc-autostart`（失败自动重试，每次开机只执行一次，日志见 `server.log`）。不依赖系统的 `lxc.service`。

## 终端

容器运行时点「终端」：真正的交互式终端（xterm.js + PTY + WebSocket，经 `lxc-attach`），
直接在黑框里敲命令（优先 `nsenter` 进入容器命名空间，不可用时回退 `lxc-attach`），支持 Tab 补全、Ctrl+C、vim/top 等。优先 bash，没有则 sh。

## 打包

在已安装 `fnpack` 的机器上：

```bash
cd fnos-lxc
fnpack build
# 得到 fnos-lxc.fpk
```

本应用只有 Python / Shell / 静态网页，没有编译产物，`manifest` 中 `platform=all` 时**同一个 fpk 即可同时用于 x86 和 ARM**，无需分别构建。
如需按架构分发，可把 `platform` 改为 `x86` 或 `arm`，各自执行一次 `fnpack build` 并给 fpk 改名区分。

### GitHub Actions 自动构建

仓库已带 `.github/workflows/build.yml`：推送到 `main` / PR / 手动触发都会构建 fpk 并作为 Artifact 上传；推送 `v*` 标签（如 `v1.0.1`）时自动把 manifest 版本改为标签版本，并发布 GitHub Release。

`fnpack` 1.3.3 起默认要求开发者证书签名。在仓库 Settings → Secrets and variables → Actions 添加：

- `FNOS_DEV_KEY`：开发者私钥（PEM 全文）
- `FNOS_DEV_CERT`：开发者证书（PEM 全文）

未配置时产出 `-unsigned.fpk`（同样会发布到 Release，文件名带 `-unsigned`）。本地调试可用 `fnpack build --nosign`。

安装到飞牛：

```bash
appcenter-cli install-fpk fnos-lxc.fpk
appcenter-cli start fnos-lxc
```

## 目录说明

| 路径 | 作用 |
|------|------|
| `manifest` | 应用元数据，`platform=all` |
| `cmd/main` | 启停 Python 后端（统一网关 Unix Socket，9100 仅监听本机） |
| `app/server/main.py` | API + 初始化 + LXC 封装 |
| `app/server/lxcconf.py` | 容器 config 解析 / 受管区块写入 |
| `app/ui/index.html` | 向导 + 列表 + 配置表单 + 终端 |
| `app/ui/vendor/` | xterm.js（MIT） |
| `app/ui/icons/` | 发行版图标（simple-icons，CC0） |
| `config/privilege` | root 运行（需要管理 LXC） |

## 注意

- 初始化与容器操作需要 **root**（privilege 已声明）
- 创建容器首次会从 `images.linuxcontainers.org` 拉模板，需能访问外网
- 快照页可在后续版本加；当前已覆盖：列表 / 启停杀冻 / 创建 / 配置表单 / 终端
- **访问方式**：通过飞牛[统一网关](https://developer.fnnas.com/docs/core-concepts/gateway-registration)（`/app/fnos-lxc/`）打开，由飞牛校验登录，应用只放行管理员（`X-Trim-Isadmin`）；因此经飞牛反代、穿透访问都可用。9100 端口只监听 `127.0.0.1`，局域网无法直连
- 所有写操作需带自定义请求头（防跨站请求），终端 WebSocket 使用一次性 30 秒令牌
- Unix Socket 权限为 0666（网关进程需要连接），本机其他有 shell 的用户理论上可伪造网关头，请只给可信用户 SSH 权限
- 若 apt 安装在向导里失败，可 SSH：`apt-get install -y lxc lxc-templates lxcfs bridge-utils debootstrap uidmap` 后继续向导

## 本地调试后端

```bash
mkdir -p /tmp/fnos-lxc-data
python3 app/server/main.py --port 9100 --data-dir /tmp/fnos-lxc-data --bind 127.0.0.1
# 浏览器打开 http://127.0.0.1:9100
```
