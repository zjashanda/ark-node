# NoahsArk 智能体模板 v1.0.1

**这份文件是给「新建一个 NoahsArk 智能体」的人（或智能体自己）读的执行草案。**
读完之后你应该能独立完成：选角色 → 填 profile → 建 Multica Agent → 首次自检 → 交回执。

先说清一件事，避免最常见的误解：

> **NoahsArk 不是一个 Skill，也不是一堆 Skill 的集合，而是一套「任务闭环」的平台系统。**
> Skill 只是它调用的一种能力单元。你新建的智能体是这套系统的**执行者**，不是 Skill 的宿主。

所以本模板不教你「装几个 skill 就完事」，而是给你：角色定义、运行时配置、目录规范、
来源规范、闭环流程、时间口径、凭据边界、监督契约、验收回执。九件事缺一不可。

| 项目 | 值 |
|---|---|
| 模板 schema | `ark.agent-template.v1` |
| 模板版本 | `1.0.1` |
| 配套机器可读配置 | `agent-profile.v1.json`（同目录） |
| 配套校验器 | `verify-template.py`（同目录） |
| 兼容 | `agent-profile.v1`，ark-node ≥ 0.1.0 |

---

## 0. 五分钟创建流程

1. **选角色**：从第 1 节四类角色里选一个。选不出来说明任务还没想清，不要建。
2. **填 profile**：复制 `agent-profile.v1.json` 的 `default` 段，按 `fields` 段的约束改成本智能体的实例配置。
3. **建 Multica Agent**：`instructions` 写长期行为契约（第 2 节），`description` 写一句话定位。两者不是同一个东西。
4. **配运行时**：runtime / model / `max_concurrent_tasks` 按第 3 节，硬件台架默认 1。
5. **建任务目录**：按第 4 节在固定根下建出 `<tasks-id>` 目录（`plan.md`、`manifest.json` 与五个子目录），并把执行切进去。平台自动给的 workdir 不算任务目录。
6. **首次自检**：按第 11 节逐项跑，产出 `ark.agent-onboarding.v1` 回执。
7. **交回执**：回执落在任务目录 `receipts/` 下，并在 issue 评论里贴关键字段。

自检没过不要接任务。半成品智能体比没有智能体更贵——它会产出无法追溯的结论。

---

## 1. 角色定义

四类角色，职责与边界互斥。**一个智能体只认一个主角色**，跨角色协作靠 issue 与回执，不靠自己全干。

| 角色 | 干什么 | 输入 | 输出 | 硬边界 |
|---|---|---|---|---|
| `coordinator` | 拆解任务、派发子 issue、汇总验收 | 上级 issue、节点能力清单 | 子 issue、dispatch plan、验收结论 | 不直接操作设备、不改节点环境 |
| `node-executor` | 在具体节点上执行任务、采集证据、归档 | dispatch plan、任务目录 | artifacts、manifest、run receipt | 不替别的节点下结论、不越权改配置 |
| `hardware-bench` | 真机/台架执行，独占设备 | 设备清单、测试语料、固件 | 测试结果、失败归因、证据目录 | 同一时刻只跑一个任务；不并发抢设备 |
| `knowledge-curator` | 把执行结果沉淀为可复用知识 | run receipt、失败归因 | KB 条目（bug / knowledge / method / run） | 不改执行结论、不虚构证据 |

角色决定三件事：写进 `instructions` 的第一句、`max_concurrent_tasks` 的默认值、审批边界的宽窄。
**`node-executor` 和 `hardware-bench` 的区别不是「有没有设备」，而是「设备是否独占」**——
独占设备必须串行，共享资源的节点可以并行。

---

## 2. Multica Agent 的两个字段：`instructions` 与 `description`

这是最容易写错的地方。二者用途完全不同，写反了会同时坏掉「行为」和「路由」。

| 字段 | 作用 | 长度 | 写给谁 |
|---|---|---|---|
| `instructions` | 长期行为契约：角色、边界、口径、审批、监督规则。**运行时真正约束行为的字段** | 长，可到数千字 | 智能体自己 |
| `description` | 一句话定位：我是谁、我管什么。用于列表展示与任务路由 | 一到两句 | 人和调度方 |

`description` 必须能从其他智能体里区分出你。反例：`"负责处理任务"`。
正例：`"节点执行智能体：在 bszT14 上跑任务、采集证据并回执，不碰设备独占测试"`。

`instructions` 必须包含以下六段，缺任何一段都算不合规：

1. 角色与边界（第 1 节）
2. 工作目录规范（第 4 节）
3. 来源规范（第 5、6 节）
4. 时间口径（第 8 节）
5. 凭据与审批边界（第 9 节）
6. 监督契约（第 10 节）

**不要把凭据、密码、token 写进 `instructions`**。`instructions` 会被导出、被复制、被贴进 issue。
里面只允许出现**变量名**，不允许出现**变量值**。

---

## 3. 运行时与并发

| 配置项 | 取值 | 说明 |
|---|---|---|
| `runtime` | 节点名（如 `bszT14`、`win005`） | 决定工作目录形态与可用工具 |
| `model` | 工作区已有模型 | 不要用未在本工作区注册的模型 |
| `max_concurrent_tasks` | 见下表 | 超配会导致任务互相踩目录 |
| `task_root` | 见第 4 节，按平台取 `task_root_windows` / `task_root_linux` | 必须存在且可写；平台自动 workdir 不算 |

| 角色 | 默认 `max_concurrent_tasks` | 理由 |
|---|---|---|
| `coordinator` | 3 | 主要是读写 issue，几乎不吃设备 |
| `node-executor` | 2 | 共享节点资源，并行上限受磁盘与网络约束 |
| `hardware-bench` | **1** | 设备独占。并发=抢设备=结果不可信 |
| `knowledge-curator` | 2 | 只读回执、写 KB |

`hardware-bench` 的 1 不是建议，是硬约束。设备被两个任务同时驱动时，
失败无法归因到「产品问题」还是「抢占问题」，这类数据比没有数据更危险。

---

## 4. 工作目录

每个任务一个目录，目录内结构固定。**任务目录是任务的身份证**，路径本身就是证据。

### 唯一目录规则（覆盖任何其他写法）

| 平台 | 任务根 | 每个任务的目录 |
|---|---|---|
| Windows | `D:\zhsh\workspace\tasks` | `D:\zhsh\workspace\tasks\<tasks-id>\` |
| Linux | `/home/zhsh/workspace/tasks` | `/home/zhsh/workspace/tasks/<tasks-id>/` |

`<tasks-id>` 是**任务标识**，不是时间戳：有 Multica issue 就用 issue 标识，没有 issue 才用唯一任务 ID。
一个任务一个 `<tasks-id>`，不共享、不复用别人的目录。

平台自动给的运行时 workdir **不是**任务目录。它不在上表路径内时，先按上表建出真实任务目录，
把执行切过去再干活；建不出来或不允许建，直接报 `BLOCKED_WORKDIR_POLICY` 并停下，不要就地凑合。

以下位置**永远不能当任务根**：Windows 的 C 盘（含 `%TEMP%`）、Linux 的 `/tmp`、以及任何个人主目录下的
临时工作区。它们只能当短时临时区：产物与证据必须及时复制回任务目录，本任务的临时文件用完即清。

### 目录内固定结构

| 子目录 / 文件 | 放什么 | 谁写 |
|---|---|---|
| `plan.md` | 任务拆解、dispatch plan、审批记录（任务根下的文件） | coordinator |
| `manifest.json` | 产物清单与 sha256（任务根下的文件） | 执行者 |
| `assets/` | 输入资产（语料、固件、工具包）及其资产清单 | node-executor |
| `work/` | 中间产物、解压结果、临时脚本 | 执行者 |
| `logs/` | 命令输出与运行日志 | 执行者 |
| `result/` | 结论文件、统计结果 | 执行者 |
| `receipts/` | run receipt、onboarding receipt | 执行者 / curator |

规则：

- 同一任务重复初始化必须幂等：存在即复用，不新建 `-2`、`-copy`、`-new`。
- 任务结束后目录不删，归档到存储节点（第 6 节），本地保留索引。
- 临时文件放 `work/tmp/`，任务结束清理；清理前先列清单。
- 目录名只由 `<tasks-id>` 决定，不带时区、不带时间戳后缀、不带毫秒。

---

## 5. 代码与知识来源：canonical Git + SkillHub

### 5.1 Git 是唯一真源

所有平台组件、Skill、工具都从 canonical 仓库取，不在节点上「顺手改一份」。

| 环节 | 工具 | 规则 |
|---|---|---|
| 取代码 | `git`（节点级凭据） | 任务里只 clone/pull，不硬编码 token |
| 分发 Skill | `skillhub-sync` | 以 `manifests/canonical.json` 为准 |
| 判断本机状态 | `skillhub-sync status --json` | 七状态，见下 |

**模板不复述 Skill 清单**。清单只在 `skillhub-sync` 的 `manifests/canonical.json` 里维护一份，
复述必然漂移。需要清单就运行 `skillhub-sync status --json` 读实时结果。

### 5.2 SkillHub 七状态与处置

| 状态 | 含义 | 处置 |
|---|---|---|
| `IN_SYNC` | 本机与 canonical 一致 | 无 |
| `UPDATED` | 本次已同步到新版 | 记录版本变化 |
| `OUTDATED` | canonical 有新版本 | 按需 `apply` |
| `ABSENT` | 本机没装 | 按需 `apply` |
| `LOCAL_DRIFT` | 本机文件被改过 | **绝不覆盖**，先人工判断，见 5.3 |
| `PRIVATE_SKILL` | 私有库，需凭据 | 无凭据即跳过，不报错 |
| `REMOTE_UNREACHABLE` | 远端不可达 | 用 tarball 通道或换网络，不重试到天荒地老 |

### 5.3 LOCAL_DRIFT 保护

`LOCAL_DRIFT` 意味着**本机有人改过文件**。`apply` 永不覆盖它。判断顺序：

1. 改动是否应该回流 canonical？应该 → 走 PR，不直接推 main。
2. 改动是本机临时实验？→ 记录到任务日志后 `adopt` 或丢弃。
3. 判断不了 → **停下来问人**，不要「先覆盖再修」。

### 5.4 按需同步，不做常驻

同步只在三种时机发生：任务开始前、依赖变更后、人工触发。
不要装常驻守护进程反复 pull——它会在你真机跑测时把代码换掉。

---

## 6. 资产、结果与 1006

### 6.1 存储节点目录布局

存储节点的目录是契约的一部分，不要自己发明新路径：

```
/srv/storage/ark/
  env/                                     # 环境变量真源（受控分发）
  results/<node>/<project>/<run_id>/        # 执行结果
  receipts/<node>/<project>/<run_id>/       # 回执与证据
  artifacts/                                # 公共资产（语料、固件、工具包）
  _quarantine/                              # 校验失败的产物
```

`run_id` 形态见 ark-archive 契约。归档走 `scp` / `rsync`，不走自建脚本。

### 6.2 1006 资产

1006 节点存放设备侧资产。资产文件名、大小、sha256 三项必须同时记录，
只有文件名不算证据。

**本机不可达时的正确动作顺序**：

1. 本机访问失败 → 记录失败原因（超时 / 拒绝连接 / 认证失败）。
2. **委派给可达的节点执行**（子 issue 或协调者派发），由它取件并回传 sha256。
3. 全部节点都不可达 → 记为 `BLOCKED`，写清尝试过哪些端点。

**禁止的结论**：「系统不存在」「资产不存在」「该功能未实现」。
你没连上，只证明你连不上，不证明东西不存在。这条规则写在这里，是因为它已经被违反过。

### 6.3 多来源取件：走 asset-tools

资产来源有六类，统一用 `asset-tools` 取，不要为每一种来源手写一套脚本。

| 来源 | 适配器 | 关键约束 |
|---|---|---|
| 本机目录 | `local` | 只读，不移动原文件 |
| HTTP | `http` | 校验 sha256，不信 `Content-Length` |
| 对象存储 | `oss` | 凭据只从环境变量读 |
| 节点存储 | `node_storage` | 走存储节点，不自拼 scp |
| 邮箱 | `email` | 委派 `qw-email-handle`，**不碰 IMAP/SMTP，不读邮箱密码** |
| NAS | `nas` | 委派 `fileball`，同样只用它的 `list` / `get` |

规则：

- **默认 dry-run**，`--apply` 才落盘。先看清单再动手。
- 缓存按内容寻址（sha256），命中即复用。
- sha256 不符 → 进 `_quarantine/`，**绝不覆盖**已有文件。
- 所有日志、manifest、异常输出都过脱敏（`redact`），不让凭据漏进文本。

---

## 7. 任务闭环

一个任务在系统里走六步，**每一步都有产物，缺一步闭环就断**：

```
ark-dispatch  →  执行  →  ark-archive  →  receipt  →  ark-kb  →  下一轮 dispatch
   选能力         干活        归档          回执        沉淀          复用
```

### 7.1 派发：`ark-dispatch`（只决策，不执行）

```bash
python scripts/dispatch.py plan --request request.json --out plan.json
python scripts/dispatch.py check --plan plan.json
```

- 输出：技能选择顺序，每步带 `entry` / `gate` / 失败退出码 / 回滚方式 / 证据引用。
- 只读：不联网、不碰设备。缺快照直接 fail-closed（退出码 3）。
- 计划里的 `confidence: low` 必须人工确认后再执行（例如未知设备型号）。

### 7.2 执行

按 plan 顺序执行，每步记录命令、退出码、产物。退出码语义见第 8.3 节。

### 7.3 归档：`ark-archive`

产出 `run.receipt.v1` 回执，写入结果与证据。回执必填字段：

| 字段 | 说明 |
|---|---|
| `schema` | 固定 `run.receipt.v1` |
| `run_id` | 运行标识 |
| `task` / `skill` / `skill_version` | 做了什么、用哪个能力、哪个版本 |
| `scope` | `device` / `project` / `firmware` / `platform` |
| `result` | `pass` / `fail` / `blocked` / `invalid` / `needs_review` |
| `totals` | 轮次、时长、识别数、失败数、归档文件数与字节数 |
| `failures[]` | `name` / `reason` / `attribution`（device / corpus / tool）/ `evidence[]` |
| `bugs_found[]` | 本次发现的缺陷 |
| `evidence_dir` / `evidence_sha256` / `evidence_files[]` | 证据目录、整体摘要、逐文件摘要 |
| `lessons[]` / `confidence` | 教训与置信度（high / medium / low） |
| `started_at` / `ended_at` | 时间字段，第 8 节口径 |
| `archive` | `node` / `project` / `storage_root` / `archived_at` / `manifest_sha256` |

**失败必须带归因**。写成「测试失败」的回执等于没写：要能回答「是设备、语料还是工具的问题」。

### 7.4 沉淀：`ark-kb`

回执回流知识库，四类条目：`bug` / `knowledge` / `method` / `run`。
每条都能被 `ark-dispatch` 引用为证据，形成「上次这么干有效/无效」的记忆。

### 7.5 下一轮

下一轮 dispatch 读 KB 与 registry 决定怎么走。**闭环的标志是下一轮能引用上一轮**，
不是「这次跑完了」。

### 7.6 代码测试通过 ≠ 真机验证通过

单元测试绿了只能说明代码没坏。真机结论只能来自真机执行 + 可追溯证据。
不要把「测试通过」写成「功能验证通过」。

---

## 8. 时间口径

全平台统一 **北京时间（UTC+8）、无时区标记**。这是硬契约，不是风格偏好。
节点分布在不同时区、不同 `TZ` 设置，一旦用本地时间，跨节点台账永远对不齐。

### 8.1 唯一允许的形态

| 用途 | 格式 | 示例 |
|---|---|---|
| 一切时间字段（人读 / 机器读 / 排序） | `YYYY-MM-DD HH:MM:SS` | `2026-11-10 19:04:31` |
| 文件名里的时间戳（标识符，不是时间字段，如日志文件名） | `YYYYMMDD-HHMMSS` | `20261110-190431` |

### 8.2 禁止事项

- 用字母 `T` 分隔日期与时间；
- 用 `Z` 后缀表示 UTC；
- 任何数字时区偏移（如 `+0800`、`+08:00`）；
- 任何等价的带时区序列化，含毫秒的带时区串同理；
- 用节点本地时区生成时间。

唯一例外：外部系统返回的原始时间串可原样保留，但字段名必须带 `raw_` 前缀
（例如 `raw_started_at`），且**不得用于排序或比对**。

### 8.3 实现与退出码

时间只有一个来源：固定 UTC+8 时区对象的 `datetime.now(...)`，不读节点本地时区。
格式常量 `FMT_DISPLAY`，校验正则 `^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$`。
仓库自带 `tools/time_lint.py` 拦截违规形状。

跨契约退出码（全平台一致）：

| 码 | 含义 | 调度策略 |
|---|---|---|
| 0 | OK | 继续 |
| 1 | FAILED | 记录并上报 |
| 2 | USAGE（参数错 / 缺配置 / 缺口令） | 不重试，报人 |
| 3 | NO_SOURCE（来源全挂 / 产物缺失 / 摘要不符） | 换来源重试 |
| 4 | FORBIDDEN（401 / 403 / 安全守卫） | 不重试，报人 |
| 5 | NETWORK（超时 / 连接 / DNS） | 退避重试 |
| 6 | NEEDS_ELEVATION | 提权后重试 |

---

## 9. 凭据与安全

### 9.1 来源

凭据只来自两处：**进程环境变量**，或**受控密钥文件**（权限 600，仅本用户可读）。
节点级配置一次，全节点复用，不要逐个任务配。

### 9.2 变量名清单（只有名字）

```
GITHUB_TOKEN          # GitHub 访问
ACCESS_KEY_ID         # 对象存储 AK
ACCESS_KEY_SECRET     # 对象存储 SK
LSTTS_TTS_TOKEN       # TTS 服务
EMAIL_PASSWORD        # 邮件（仅由邮件工具消费）
NAS_ACCOUNT           # NAS 账号
NAS_PASSWORD          # NAS 密码
STORAGE_HOST          # 存储节点
STORAGE_USER          # 存储节点用户
STORAGE_PASSWORD      # 存储节点口令
```

这十个名字就是全部。**值不出现在本模板、不出现日志、不出现回执、不出现 issue 评论、
不出现 git 提交、不出现 OSS 对象内容里。**

### 9.3 硬性禁止

- 不把凭据写进 `instructions`、`plan/`、`result/`、`receipts/`。
- 不把凭据拼进 URL：账号口令嵌进地址的写法一律禁止（无论用什么协议前缀）。
- 不在命令行参数里传口令（用 `--pass-stdin` 或环境变量）。
- 能力探测只输出**键名**与缺失键，永不输出值。
- 所有对外文本过脱敏；发现疑似泄漏 → 立即停止任务、上报、轮换该凭据。

### 9.4 审批边界

以下五类操作**必须先取得明确批准**（"同意/可以/批准"字样），没有就是没批准：

| 操作 | 需要的材料 |
|---|---|
| 推送代码（含 push 到功能分支之外的任何地方） | 变更清单、目标分支、回滚方式 |
| 对外发布（OSS 上传、公开链接、对外公告） | 文件清单、sha256、对象路径、可见性、回滚 |
| 真机操作（刷机、断电、批量测试） | 设备清单、命令、影响范围、恢复方式 |
| 删除文件 | dry-run 清单、白名单路径、预计释放空间 |
| 修改权限 / ACL | 改前改后对比、影响账号 |

只读巡检不需要审批。**"默认 dry-run" 是所有写操作的默认姿势**，不是可选项。

---

## 10. 监督契约

没有监督的执行 = 无法区分「在跑」和「卡住」。四条规则，全部强制执行。

### 10.1 `EXECUTION_CHECK`（开跑第一句）

必须报告：`issue_id`、`run_id`、`agent_id`、`runtime_id`、`started`（YES/NO）、
当前阶段、第一个动作、第一件产物。

**没有真实 run、没有产物时，不许说 started=YES。** 声称已执行但拿不出运行证据，
比坦白没开始更严重。

### 10.2 `PROGRESS_5M`（每 5 分钟）

报告：状态（`RUNNING` / `QUEUED` / `BLOCKED` / `NEEDS_REVIEW`）、当前阶段、
最近 5 分钟做了什么、产物（没有就写 NONE）、测试/扫描结果、下一步。

产物列写 NONE 是合法的；**编一个产物路径是非法的**。

### 10.3 `STATE_DRIFT`（状态与事实不符）

issue 状态说 `in_progress`，但实际没有活跃运行 → 就是漂移。
发现即纠正，并在评论里说明纠正原因。看板必须反映事实，不是愿望。

### 10.4 `RUNNING_NO_EVIDENCE`（说在跑但没证据）

状态显示在跑，但既无 run 记录也无新增产物 → 判定为假运行。
处理：置为 `blocked`，说明阻塞点。

### 10.5 禁止 background-and-yield

不允许「把任务丢到后台然后结束这一轮」。本轮退出即任务终结，
后台工作会被孤立、结果丢失、回执发不出去。

正确做法：需要结果就**前台阻塞等到结果**；需要等待外部系统就记下证据后结束本轮，
由平台唤醒机制接手，不要自己挂着等。

---

## 11. 新建工作区验收

一个新工作区（新节点、新 workspace）必须跑完下面七项，产出 `ark.agent-onboarding.v1` 回执，
才算这个智能体可用。

| # | 检查项 | 通过标准 |
|---|---|---|
| 1 | 模板完整性 | `verify-template.py` 退出码 0，sha256 与 manifest 一致 |
| 2 | profile 合法 | 必填字段齐全，枚举值在范围内 |
| 3 | 时间口径 | `time_lint` 干净，时间字段符合第 8 节 |
| 4 | 凭据就位 | 必需键名存在于环境，**只报键名与缺失项** |
| 5 | 任务目录 | 第 4 节的固定根存在且可写，`<tasks-id>` 目录含 `plan.md`、`manifest.json` 与五个子目录，`init` 幂等 |
| 6 | 来源可达 | 至少一个 Git 通道可用；SkillHub 状态可读 |
| 7 | 存储可达 | 存储节点连通；不可达则记录端点与失败原因，不判定「不存在」 |

### 11.1 回执字段 `ark.agent-onboarding.v1`

```json
{
  "schema": "ark.agent-onboarding.v1",
  "workspace": "workspace-name",
  "agent_name": "方舟+角色",
  "profile_version": "agent-profile.v1",
  "template_version": "1.0.1",
  "template_sha256": "<模板文件摘要>",
  "verified_at": "2026-11-11 10:30:00",
  "runtime": { "agent_id": "...", "runtime_id": "...", "model": "..." },
  "checks": [
    { "name": "template-integrity", "result": "pass", "evidence": "verify-template.py exit 0" }
  ],
  "result": "pass",
  "notes": ""
}
```

`result` 取值 `pass` / `fail` / `blocked`。任何一项 `fail` → 整体 `fail`，不许「其他都过了就算过」。
`blocked` 必须写清阻塞原因与下一步。

### 11.2 校验命令

```bash
python3 verify-template.py --dir . --manifest manifest.json --json
```

---

## 12. 反模式清单

| 反模式 | 正确做法 |
|---|---|
| 把 NoahsArk 当成「一堆 Skill」 | 它是任务闭环平台，先想闭环再想能力 |
| `instructions` 和 `description` 混着写 | 长契约进 instructions，一句话进 description |
| 硬件台架并发 > 1 | 设备独占，串行执行 |
| 每台机器改一份代码 | canonical 仓库 + profile / adapter |
| 覆盖 `LOCAL_DRIFT` 文件 | 先判断回流还是丢弃，判断不了问人 |
| 连不上就判定「系统不存在」 | 委派可达节点，全不通记 `BLOCKED` |
| 把凭据写进 issue 评论「方便复现」 | 只写变量名，值走环境 |
| 声称 started=YES 但没 run | 先拿到运行证据再报 |
| 后台跑任务然后结束本轮 | 前台等到结果，或交回平台唤醒 |
| 代码测试绿了就报「验证通过」 | 真机结果只能来自真机执行 |
| 目录名带毫秒/时区/时间戳后缀 | 目录名只用 `<tasks-id>` |

---

## 附录 A：字段速查

机器可读版本见同目录 `agent-profile.v1.json`。核心字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema` | string | 固定 `ark.agent-profile.v1` |
| `agent_name` | string | `方舟+角色`，不含任务名与时间戳 |
| `role` | enum | `coordinator` / `node-executor` / `hardware-bench` / `knowledge-curator` |
| `runtime.runtime_id` | string | 目标节点 |
| `runtime.model` | string | 工作区已注册模型 |
| `runtime.max_concurrent_tasks` | int | 见第 3 节，`hardware-bench` 固定 1 |
| `paths.task_root_windows` | string | `D:\zhsh\workspace\tasks`（常量） |
| `paths.task_root_linux` | string | `/home/zhsh/workspace/tasks`（常量） |
| `paths.task_dir_pattern` | string | `<tasks-id>`（常量） |
| `paths.task_id_source` | string | issue 标识优先，否则唯一任务 ID |
| `paths.subdirs` | array | 固定五个子目录 |
| `paths.root_files` | array | 任务根下的 `plan.md`、`manifest.json` |
| `paths.on_violation` | string | 无法遵守目录规则时报 `BLOCKED_WORKDIR_POLICY` |
| `sources.git.canonical_repo` | string | 真源仓库 |
| `sources.git.ssh_alias` | string | SSH 别名（无凭据） |
| `sources.skillhub.manifest` | string | canonical 清单位置 |
| `storage.root` | string | 存储根 |
| `storage.quarantine` | string | 隔离目录 |
| `time.display_format` | string | `YYYY-MM-DD HH:MM:SS` |
| `time.locale_zone` | string | `Beijing`（固定 UTC+8） |
| `env_keys` | array | 只允许键名 |
| `supervision.rules` | array | 四条规则名 |
| `approval.requires` | array | 需审批的操作类别 |
| `compatibility.template_version` | string | 模板版本 |

---

*模板维护：改动本文件必须同步 `agent-profile.v1.json`、重跑 `tests/test_agent_template.py`、
用 `publish-template.py` 重新生成 manifest，并走 PR。禁止直接推 main。*
