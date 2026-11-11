# OSS 根总览与单链接导航

面向外部工作区的接入说明：把 NoahsArk 从「一堆仓库和 Skill」变成**一个地址加一份总览**。

## 1. 问题

在此之前，外部智能体要接入 NoahsArk，需要知道：模板在哪个 OSS 前缀、Skill 清单在哪个仓库、结果归档走哪台机器、工作目录怎么建、时间怎么写、凭据从哪来。这些信息分散在多个仓库的多个文件里，任何一个变化都会让外部副本失效，而且没有地方能确认「我现在读的是不是最新版」。

## 2. 解法：一个永久地址，三份生成物

```
https://listenai-test-internal.oss-cn-beijing.aliyuncs.com/noahsark/index.md
```

```text
noahsark/
├── index.md            # 唯一人类入口（永久地址，只重指向）
├── index.json          # 机器导航
├── manifest.json       # 根索引自身的版本与校验值
├── agent-template/     # Agent 模板（版本目录不可变）
│   ├── latest/         # 只做指向，内容与当前版本逐字节一致
│   ├── v1.0.0/
│   └── v1.0.1/
├── platform/           # 规划中
├── git/                # 规划中
├── storage/            # 规划中
├── knowledge/          # 规划中
├── execution/          # 规划中
└── releases/           # 规划中
```

三条不变式：

1. **入口地址永久不变**。升级、扩目录、调顺序都只改 `index.md` / `index.json` / `manifest.json` 三个对象。
2. **版本目录不可变**。`v1.0.1/` 一旦发布就不再改写；`latest/` 才是可变的指针。
3. **未发布的内容不出现链接**。`catalog.json` 里状态为 `planned` 的条目在 `index.md` 与 `index.json` 中都没有 URL，不会产生死链。

## 3. 内容职责

`index.md` 回答「我该读什么、按什么顺序读」：

- 平台定位：NoahsArk 是闭环平台（派发 → 执行 → 归档 → 沉淀 → 下一轮），不是单个 Skill；
- 三步接入：读总览 → 建任务目录 → 按角色读必读项；
- 按角色阅读顺序（`external-reader` / `coordinator` / `node-executor` / `hardware-bench` / `knowledge-curator`），必读项有序，可选参考单列；
- 条目总览：每个分组一张表，标出版本、状态、校验值前缀；
- 硬规则：工作目录、时间口径、凭据、审批边界、监督契约；
- 未发布条目与更新回滚说明。

`index.json` 是同一份内容的机器视图，每个条目固定字段：`id`、`title`、`kind`、`status`、`version`、`path`、`url`、`roles`、`requires`、`summary`，可用条目额外带 `sha256`。字段集合由 `verify-index.py` 强制，缺一个就失败。

`manifest.json` 记录根索引自己的身份：版本、内容提交号、生成时间、两个文件的字节数与校验值。它让「线上这份是不是最新」变成一个可判定问题，而不需要人去比对正文。

## 4. 真源与副本

| 层 | 位置 | 可变性 |
|---|---|---|
| 真源 | `ark-node` 仓库 `templates/noahsark-index/catalog.json` | 每次改动走提交与评审 |
| 生成物 | `build/noahsark-index/`（不进版本库） | 可随时重建 |
| 副本 | OSS `noahsark/index.*` | 只由 `publish-index.py` 写入 |

OSS 控制台不再作为编辑入口。这样做的直接收益：变更可追溯（提交号写在 manifest 里）、可回归（29 项本地测试）、可回滚（只重写三个对象）。

## 5. 为什么角色是一等字段

外部接入方最常见的失败不是「读不到」，而是「读错顺序」：先看设备仓库再看工作目录规则，就会把产物写到 `C:` 盘或 `/tmp`；先动手再看审批边界，就会在没批准的机器上发布。

因此 `catalog.json` 里每条目都声明 `roles`，每个角色都有有序的 `must_read` 与无序的 `optional`。生成器把这两个列表渲染成阅读路径，校验器保证：

- 每个角色至少一条必读；
- 每个引用都指向真实存在的条目；
- `requires` 关系无环。

角色定义（与 Agent 模板第 1 节一致，另加外部读者）：

| 角色 | 关心什么 |
|---|---|
| `external-reader` | 首次接入：唯一地址、创建入口、模板正文、目录规则 |
| `coordinator` | 拆解与派发：监督契约、审批边界、凭据、时间口径、路由 |
| `node-executor` | 在节点上落地：目录规则、凭据、时间、节点 CLI |
| `hardware-bench` | 真机基准：目录规则、监督契约、烧录与验证能力仓库 |
| `knowledge-curator` | 沉淀：时间口径、存储布局、知识库账本、归档 |

## 6. 发布与验收

发布前的门禁（任一失败即不上传）：源文件在版本库且工作树干净 → 密钥扫描 → 时间口径检查 → 生成 → 离线校验 → 线上版本比对。

发布后的验收（`verify-index.py --online`）：

1. 三个对象匿名 GET 均 200；
2. `index.json` 与 `manifest.json` 为合法 JSON 且 schema 相符；
3. 每个 `noahsark/` 对象的匿名回读 sha256 与 manifest 记录一致；
4. `manifest.json` 中列出的每个文件都能匿名读到，且 sha256 一致；
5. 指向 Git 的条目（可选 `--check-git`）在 `api.github.com` 可解析。

匿名读这一步是刻意的：外部工作区没有我们的凭据，只有匿名可读才叫「接得进来」。因此根索引的对象 ACL 必须是 `public-read`。

## 7. 边界

- 本任务只新增根索引三件套与工具链，**不合并到 `ark-node` 主分支**；合入需要单独批准。
- 不删除 OSS 任何历史对象；回滚只覆盖根索引三个对象。
- 不写入任何密码、令牌、私钥；凭据只在进程环境变量里存在。
- 不手工编辑 OSS 上的 Markdown。

## 8. 目录内文件

| 文件 | 说明 |
|---|---|
| `templates/noahsark-index/catalog.json` | 唯一手工维护源 |
| `templates/noahsark-index/build-index.py` | 生成器（零依赖） |
| `templates/noahsark-index/verify-index.py` | 校验器（离线 / `--online` / `--check-git`） |
| `templates/noahsark-index/publish-index.py` | 发布器（先测后发、匿名回读） |
| `templates/noahsark-index/README.md` | 维护者操作手册 |
| `tools/oss_publish.py` | 共用 OSS 客户端（签名 V1，只 PUT 与匿名 GET） |
| `tests/test_noahsark_index.py` | 29 项回归测试 |
