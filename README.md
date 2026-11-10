# ark-node

Ark 节点的**统一入口**：一个 CLI，覆盖节点的环境变量同步、节点身份、任务目录初始化、能力探测与安全取件。

它把过去分散在 `ark-env`（环境变量分发）、`ark-mesh`（节点间取件）、`node-agent`（节点身份与目录约定）三处的公共能力收拢到一个仓库，**只保留一套契约**，Windows 与 Linux 共用同一份 Python 实现。

```
ark-node env       同步环境变量（真源仍是 ark-env 分发服务）
ark-node identity  解析节点身份（node_id / 平台 / 角色）
ark-node init      幂等初始化任务目录与运行时目录
ark-node probe     输出节点能力清单（ark-node-capability.v1）
ark-node fetch     status / index / fetch / archive（artifact 通道）
```

## 设计边界

| 项 | 归属 |
|---|---|
| 环境变量**真源** | `ark-env` 分发服务（HTTP），本仓库只做**客户端** |
| 环境变量**值** | 只存在于节点本机文件或进程环境，**不进仓库、不进日志** |
| 项目配置值 | 不进本仓库，走项目自己的 profile |
| 业务逻辑 | **不在**本仓库，fetch 只做搬运与校验 |
| 节点运行时（agent 本体） | 仍由部署仓库负责，本仓库只提供身份/目录/探测契约 |

原则：**公共能力进 canonical 仓库，项目差异进 profile / adapter / tool。**

## 安装

零第三方依赖，Python ≥ 3.8。

```bash
git clone https://github.com/zjashanda/ark-node.git
cd ark-node
python3 -m pip install --user .        # 或直接 python3 -m ark_node ...
```

不想装包也可以直接跑：

```bash
./bin/ark-node probe          # Linux / macOS
```

```powershell
.\bin\ark-node.ps1 probe      # Windows
```

## 五分钟上手

```bash
# 1. 我是谁
ark-node identity

# 2. 这台机器能干什么（JSON，可用于能力路由）
ark-node probe --out capability.json

# 3. 建今天的任务目录（幂等，重复执行不会多建）
ark-node init --task node-patrol

# 4. 环境变量先看差异，再落盘
ark-node env --base https://ark-env.example.com:8443 --diff
ark-node env --base https://ark-env.example.com:8443 --apply

# 5. 取件
ark-node fetch status --json
ark-node fetch fetch --name my-artifact --dest ./inbox
```

口令永远从文件或环境变量来，不走命令行参数：

```bash
printf '%s' "$ARK_HTTP_PASS" | ark-node env --pass-stdin --apply
```

## 契约

三份 JSON 契约 + 一条时间契约，见 [docs/contracts.md](docs/contracts.md)：

- `ark-node-capability.v1` — 能力清单（`probe`）
- `ark-node-manifest.v1` — 取件台账（`fetch`）
- `ark-node-env-state.v1` — 环境同步状态（`env`）
- **时间**：对外一律北京时间、无时区后缀 `YYYY-MM-DD HH:MM:SS`；机器可读字段用 RFC3339；文件名用 `YYYYMMDD-HHMMSS`

## 退出码

| 码 | 含义 |
|---|---|
| 0 | OK |
| 1 | FAILED（未归类失败） |
| 2 | USAGE（参数/配置错误） |
| 3 | NO_SOURCE（取件无可用来源） |
| 4 | FORBIDDEN（鉴权失败或被安全守卫拒绝） |
| 5 | NETWORK（网络/传输失败） |
| 6 | NEEDS_ELEVATION（需要管理员/root） |

## 安全

- 仓库内**没有**任何密码、token、私钥、真实主机名或内网 IP；示例配置只用 `example.com` 占位。
- 凭据只以**环境变量名**或本机 600 权限文件的形式被引用（`~/.config/ark/auth`、`%USERPROFILE%\.config\ark\auth.json`）。
- `fetch` 内置文件名守卫：`.env`、`*.key`、`*.pem`、`id_rsa`、`auth.json`、`git-credentials` 等一律拒绝，且拒绝 `..` 路径穿越。
- 所有写盘走原子写 + 600；所有 JSON 输出经 `redact` 过一遍才落地。
- `tools/secret_scan.py` 可在提交前自查：`python3 tools/secret_scan.py ark_node config docs tests tools`。

## 与旧工具的兼容

`compat/ark-env/` 保留 `ark-env.sh` / `ark-env.ps1` 作为**语义参照**（不参与运行）。新旧对应关系、行为差异与迁移步骤见 [docs/legacy-compat.md](docs/legacy-compat.md)。

`ark-node fetch` 保留 `ark-mesh` 的子命令与 flag 形态，旧脚本把 `ark_fetch.py` 换成 `ark-node fetch` 即可，参数不变。

## 文档

- [docs/architecture.md](docs/architecture.md) — 模块划分与数据流
- [docs/contracts.md](docs/contracts.md) — 三份 JSON 契约与时间契约
- [docs/runbook.md](docs/runbook.md) — 日常操作手册
- [docs/rollback.md](docs/rollback.md) — 回滚步骤
- [docs/legacy-compat.md](docs/legacy-compat.md) — 旧工具映射与差异

## 许可

MIT，见 [LICENSE](LICENSE)。
