# NoahsArk 根索引（noahsark/index.md）

外部工作区接入 NoahsArk 时只需要记住**一个地址**：

```
https://listenai-test-internal.oss-cn-beijing.aliyuncs.com/noahsark/index.md
```

这个地址永久不变。升级模板、新增域目录、调整推荐顺序，都只改写这个地址背后的三个对象，不新增入口、不改路径、不删历史。

## 1. 这里放什么

| 文件 | 作用 | 谁读 |
|---|---|---|
| `catalog.json` | **唯一手工维护源**：两区域、条目、角色、服务、初始化步骤都在这里 | 维护者 |
| `services/*.json` | 服务机器可读契约：地址、鉴权、接口、错误、降级与验证状态 | 维护者与调用方 |
| `build-index.py` | catalog → `index.md` / `index.json` / `manifest.json` | CI 与本地 |
| `verify-index.py` | 结构校验 + 双声明一致 + 可选联网校验（匿名 GET、sha 比对） | 发布前后 |
| `check-services.py` | 服务契约漂移、ark-kb 基线、节点回执与在线探测 | 发布前后 |
| `publish-index.py` | 先测后发，上传三件套并匿名回读 | 发布者 |

生成的三个对象（不进版本库，在 `build/noahsark-index/`）：

- `index.md` —— 唯一人类入口，只有两个一级区域：**体系介绍**与**体系环境初始化**；
- `index.json` —— 机器导航，顶层 `system` / `onboarding` 两个导航域，每条含 id、标题、状态、版本、地址、`source_repo`、`source_commit`、`sha256`；
- `manifest.json` —— 根索引自身的版本、提交号、区域与服务摘要、两个文件的校验值与生成时间。

## 1.1 信息架构（固定）

| 区域 | 回答的问题 | JSON 域 |
|---|---|---|
| 体系介绍 | 平台边界、三层职责、执行闭环、角色体系、服务入口、知识时序、门禁与安全 | `system` |
| 体系环境初始化 | 工作区/节点/Agent 怎么接入：目录、runtime、Agent 绑定、Git、8090、8091、工具、TTS、验收、回执 | `onboarding` |

页面与 JSON 必须同时声明；`verify-index.py` 会检查一级标题数量、分组所属区域、条目与初始化步骤
是否在对应区域出现，任一不一致即判失败。`index.json` 仍保留 `items` / `groups` 顶层数组，
每个条目带 `region` 字段，供只认 v1 字段的旧读者继续使用。

## 2. 为什么不再手工维护 OSS 文档

OSS 上的 Markdown 没有历史、没有评审、没有测试。手工改 OSS 会立刻产生三个问题：谁改的、改了什么、还能不能回滚——都答不上来。因此：

- **Git 是唯一真源**，OSS 只是这份真源的副本；
- 任何对外可见的文字都必须先进入 `catalog.json` 或生成器，再由脚本发布；
- 版本目录（例如 `noahsark/agent-template/v1.1.0/`）**只增不改**，根索引只做「重指向」。

禁止的操作：直接在控制台上传/覆盖 `noahsark/index.*`；删除版本目录；把凭据写进任何对象。

## 3. 改索引的正确姿势

```bash
# 1. 只改 catalog.json（条目、角色、服务、初始化步骤）与服务契约
$EDITOR templates/noahsark-index/catalog.json
$EDITOR templates/noahsark-index/services/storage-service.json

# 2. 校验 + 生成（不联网）
python3 templates/noahsark-index/build-index.py

# 3. 离线校验：结构、区域、双声明、引用、模板校验值互校、密钥扫描、时间口径
python3 templates/noahsark-index/verify-index.py

# 4. 服务门禁：契约漂移 + 构建产物一致 + 可选 ark-kb 基线
python3 templates/noahsark-index/check-services.py --kb-repo <ark-kb 本地检出>

# 5. 节点侧在线探测（只在可达节点执行；token 从 0600 文件读）
python3 templates/noahsark-index/check-services.py --probe --token-file ~/.config/ark/kb-token --json

# 6. 回执门禁：把节点回执当发布前置条件
python3 templates/noahsark-index/check-services.py --receipt <receipt.json>

# 7. 看上传计划（默认 dry-run，不写任何东西）
python3 templates/noahsark-index/publish-index.py

# 8. 真正发布（凭据只从环境变量读，且必须先获批准）
ACCESS_KEY_ID=... ACCESS_KEY_SECRET=... \
  python3 templates/noahsark-index/publish-index.py --apply --yes

# 9. 发布后联网复核：匿名 GET 200、版本/标题/URL/Git pin/latest/bytes/SHA 一致
python3 templates/noahsark-index/verify-index.py --online
```

### catalog 条目的最小字段

```json
{
  "id": "agent-template.template",
  "kind": "entry",
  "title": "智能体模板正文 v1.7.0",
  "status": "available",
  "version": "1.7.0",
  "roles": ["external-reader", "coordinator"],
  "requires": ["agent-template.manifest"],
  "oss": "noahsark/agent-template/v1.7.0/noahsark-agent-template.md",
  "local": "templates/agents/noahsark-agent-template.md",
  "summary": "一句话说清这条是干什么的。"
}
```

服务条目用契约而不是地址：

```json
{
  "id": "svc.storage-service",
  "kind": "service",
  "title": "Ark Storage Service（8090）",
  "status": "available",
  "version": "1.0.0",
  "roles": ["node-executor"],
  "requires": [],
  "descriptor": "templates/noahsark-index/services/storage-service.json",
  "summary": "8090 统一存储入口。"
}
```

- `status` 只有 `available` / `planned`。**没发布的东西必须是 `planned`**，且不得带 `oss`、`git`、`descriptor`、`version` —— 校验器会直接拒绝，避免死链。
- `local` 指向仓库内同名文件时，生成器会现算 sha256，并与 `templates/agents/manifest.json` 交叉核对；两边不一致即失败。
- `git` 条目写 `zjashanda/<repo>`，可选 `file` 生成 blob 链接。
- 服务条目的 `version`、`endpoint`、`host.name`、`host.port` 必须与 `services/*.json` 逐字一致；endpoint 只允许可解析节点名，出现 RFC1918 字面量直接失败。
- `requires` 必须指向存在的 id，且不许成环。
- 分组必须声明 `region: system|onboarding`；`onboarding.steps` 引用的验收项与阻塞码必须已登记。

## 4. 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功（含 dry-run） |
| 1 | 校验或上传失败 |
| 2 | 用法错误（`--apply` 未配 `--yes`） |
| 3 | NO_SOURCE：源文件缺失、未纳入版本库、工作树脏 |
| 4 | FORBIDDEN：缺少 `ACCESS_KEY_ID` / `ACCESS_KEY_SECRET`，或 visibility 非法 |
| 5 | NETWORK：准备阶段或回读时网络失败 |

## 5. 发布门禁（先测后发）

`publish-index.py` 在写任何对象之前依次执行，任一失败即中止：

1. 源文件必须在版本库中；工作树有未提交改动则拒绝（`--allow-dirty` 仅供调试）。
2. `tools/secret_scan.py` —— 密钥、私钥、内网地址扫描。
3. `tools/time_lint.py` —— 时间只允许 `YYYY-MM-DD HH:MM:SS`。
4. 生成三件套，并把 `checks.secret_scan` / `checks.time_lint` 写回 manifest。
5. `verify-index.py` 离线校验（含两区域与双声明一致）。
6. 线上版本比对：同版本不同内容需 `--allow-republish`；版本号回退需 `--allow-downgrade`。

发布前还应单独跑 `check-services.py`：服务契约漂移、ark-kb 基线、节点回执必须一致。
节点侧在线探测回执（`--probe`）是发布验收证据，不是发布器的自动依赖——本机不可达时由节点执行。

上传顺序固定 `index.json` → `index.md` → `manifest.json`。manifest 最后落地，保证读者看到清单时它描述的内容已经就位。每个对象上传后立即匿名回读并比对 sha256，不符即失败。

## 6. 回滚

根索引的回滚是**只改指针**，代价极小：

```bash
# 方案 A：回退 catalog 到上一个提交，重新生成并发布
git checkout <上一个提交> -- templates/noahsark-index/catalog.json
python3 templates/noahsark-index/publish-index.py --apply --yes
```

```bash
# 方案 B：直接从上一个版本的 manifest 取回 index.json / index.md 重新上传
python3 templates/noahsark-index/publish-index.py --allow-downgrade --apply --yes
```

两条路径都只覆盖 `noahsark/index.md` / `noahsark/index.json` / `noahsark/manifest.json`，**不删除任何版本目录**。版本物料不可变，回滚永远不需要动它们。

## 7. 与模板的关系

根索引不复制模板正文，只指向它：

- 创建入口 `noahsark/agent-template/latest/manifest.json`（当前 v1.1.0）；
- 正文 `noahsark/agent-template/v1.1.0/noahsark-agent-template.md`；
- 校验器 `noahsark/agent-template/v1.1.0/verify-template.py`。

模板升级时：先按 `templates/agents/publish-template.py` 发布新版本目录，再改本目录 `catalog.json` 里的版本号与校验值，最后发布根索引。顺序颠倒会出现「索引指向尚不存在的对象」，`verify-index.py --online` 会拦住。
