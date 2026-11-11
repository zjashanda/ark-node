# 智能体模板维护手册

本文件给**维护者**：改模板、发新版、处理发布故障的人。
新建一个智能体的人应该读 [templates/agents/noahsark-agent-template.md](../templates/agents/noahsark-agent-template.md)，
不是本文件。

## 1. 文件构成

| 文件 | 角色 | 谁改 |
|---|---|---|
| `templates/agents/noahsark-agent-template.md` | 模板正文，人读的行为契约 | 维护者（走 PR） |
| `templates/agents/agent-profile.v1.json` | 机器可读 schema + 默认实例 + 角色预设 | 维护者（走 PR） |
| `templates/agents/publish-template.py` | 发布会话：扫描、算摘要、生成 manifest、上传 | 维护者 |
| `templates/agents/verify-template.py` | 目标工作区侧的校验器（零依赖） | 维护者 |
| `templates/agents/manifest.json` | 最近一次发布的清单（由脚本生成，**不手改**） | 仅 `publish-template.py` |

三者内容必须同步：模板正文里描述字段 → profile 里要有对应字段 → 校验器里要有对应断言。
改一处不改其余，`tests/test_agent_template.py` 会红。

## 2. 本地校验

提交前跑三条，任何一条不过都不要提 PR：

```bash
python3 -m unittest tests.test_agent_template -v
python3 tools/secret_scan.py templates docs tests
python3 tools/time_lint.py
```

外加一次发布预演（dry-run，不联网）：

```bash
python3 templates/agents/publish-template.py
```

dry-run 会打印将要上传的对象路径、字节数、摘要与可见性，并在 `build/agent-template/<版本>/`
生成 `manifest.json`。`build/` 已在 `.gitignore` 里，不会进仓库。

## 3. 发布流程

### 3.1 步骤

1. **改内容**：模板正文 / profile 一处或两处一起改，同步校验器断言。
2. **跑校验**：第 2 节三条 + dry-run。
3. **提 PR**：分支命名 `ark/<YYYYMMDD>-<slug>`，禁止直接推 main。
4. **评审**：至少一人看过内容差异；涉及凭据口径、审批边界、时间契约的改动必须由工作区负责人确认。
5. **合并**：合并后从 main 重新跑 dry-run，确认 `git.dirty` 为 `false` 且 `git.commit` 是合并提交。
6. **申请发布审批**：把 dry-run 输出（文件清单、sha256、对象路径、可见性、回滚方式）贴进 issue，
   写明「请批准对外发布」。**没有明确批准不许 `--apply`。**
7. **上传**：凭据从环境变量注入，然后 `--apply --yes`。
8. **验证**：下载 `latest/manifest.json` 到一个空目录，跑 `verify-template.py --dir .`，退出码必须是 0。
9. **回执**：在 issue 里贴上传结果与校验输出。

### 3.2 命令形态

```bash
# 预演（默认）
python3 templates/agents/publish-template.py

# 机器可读
python3 templates/agents/publish-template.py --json

# 发布（需已获批准 + 凭据就位）
python3 templates/agents/publish-template.py --apply --yes --endpoint <endpoint-host>
```

凭据变量名固定为 `ACCESS_KEY_ID` / `ACCESS_KEY_SECRET`，对象存储桶与端点用
`ARK_OSS_BUCKET` / `ARK_OSS_ENDPOINT` 覆盖。**值不进命令行、不进仓库、不进日志。**

### 3.3 对象布局

```
<prefix>/v<版本>/noahsark-agent-template.md     不可变
<prefix>/v<版本>/agent-profile.v1.json          不可变
<prefix>/v<版本>/verify-template.py             不可变
<prefix>/v<版本>/manifest.json                  不可变
<prefix>/latest/noahsark-agent-template.md      稳定别名
<prefix>/latest/agent-profile.v1.json           稳定别名
<prefix>/latest/verify-template.py              稳定别名
<prefix>/latest/manifest.json                   稳定别名（含 immutable_manifest 指回版本目录）
```

默认前缀 `noahsark/agent-template`，默认可见性 `public-read`（新工作区要能不认证就能读）。
`latest` 只是别名，内容与对应版本逐字节一致——不一致就是发布事故。

## 4. 回滚

对象不可变，所以回滚**不是删文件**，而是把 `latest` 指回上一个版本：

1. 取出上一版的 `manifest.json`（版本目录里那份，永远在）；
2. 用它的内容重写 `latest/*` 四个对象；
3. 对 `latest/manifest.json` 跑一次 `verify-template.py`。

不要删除任何版本目录：旧工作区可能正钉在那个版本上。
真要下架，先确认没有工作区在用（`verify` 记录、issue 回执里能查到版本号），再单独申请。

## 5. 版本规则

语义化三段：`主.次.修订`。

| 变化 | 版本位 | 例子 |
|---|---|---|
| 只改文字、不改字段与语义 | 修订 | 修错别字、补例子 |
| 新增字段（向后兼容） | 次 | 增加 `approval.requires` 的一项 |
| 改字段语义、删字段、改时间口径 | 主 | 时间口径变动、审批边界收紧 |

主版本变更必须同时更新 `compatible` 段，并在模板正文顶部调整兼容矩阵。
**已发布的对象不覆盖、不回改**：改了就发新版本。

## 6. 故障处置

| 现象 | 原因 | 处置 |
|---|---|---|
| 扫描报 `github-token` / `aliyun-ak` | 内容里真出现了凭据形状的串 | 立即删除该内容；若已提交，按泄漏流程轮换凭据 |
| 扫描报 `high-entropy-hex` 但不是摘要 | 有人贴了真实密钥散列 | 确认来源，删掉 |
| `time_lint` 报 `iso-t-separator` | 示例里写了字母 T 分隔 | 改成空格，或把示例改成文字描述 |
| `--apply` 退出码 2 + 缺凭据 | 环境变量没注入 | 注入 `ACCESS_KEY_ID` / `ACCESS_KEY_SECRET` 后重试，不要写进命令行 |
| `--apply` 退出码 3 + 工作区脏 | 有未提交改动 | 先提交再发布；从脏工作区发布等于发出无法追溯的内容 |
| `--apply` 退出码 4 | 401/403 | 检查桶权限与 AK 有效期；不重试，报人 |
| `--apply` 退出码 5 | 网络 | 退避重试；连续失败换网络通道 |
| 上传成功但 `verify` 失败 | 内容被改过 / 上传截断 | 视为发布事故：重传该版本，`latest` 指回上一版 |
| 新工作区读不到 `latest` | 可见性不是 `public-read` | 改桶或对象 ACL，重新验证 |

## 7. 硬规则

- `manifest.json` **只能由 `publish-template.py` 生成**。手改出来的摘要和内容对不上，等于给下游埋雷。
- 模板正文里**只允许出现凭据的变量名**，不允许任何值、任何真实 token、任何真实口令。
- 不写内网 IP 字面量：用 `<storage-node>` 之类的占位符。需要具体地址时写在节点本机配置里，不写进模板。
- 不写「某某系统不存在」这类结论式判定；连不上就记连不上。
- 模板与 profile 的字段名、枚举值、目录名保持一致，改一边必须改另一边。
- 发布是**对外动作**：没有明确批准就停在 dry-run。

## 8. 变更记录

| 版本 | 变更 |
|---|---|
| 1.0.0 | 首版：四角色、目录规范、canonical + SkillHub 来源、1006 与多来源取件、六步闭环、时间口径、凭据边界、四条监督规则、onboarding 回执 |
