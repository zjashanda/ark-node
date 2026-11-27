# 知识候选流水线工具

lstest / ark-kb / service 三类改动的统一提交与推广链路。执行节点只上传，审核与推广只在受信节点（bszT14 / linux003）发生。

## 角色分工

| 角色 | 节点 | 能做什么 | 不能做什么 |
|---|---|---|---|
| 执行节点 | win003 等 | 把验证过的改动 + 证据上传到 8090 | 创建候选、审核、推送仓库 |
| 审核员 | bszT14 / linux003 | 取回上传、复核、创建候选、推广分支 | 代替执行节点跑设备 |

## 三步链路

```
① 执行节点上传
   python -m lstest.scripts.upload_lstest_patch \
       --task <issue> --run <run> --base-commit <lstest commit> \
       --file <相对路径> --checks '<适配校验 JSON>' --log-sample <样本文件>
   → PUT 8090 /storage/v1/objects/evidence/noahsark/<node>/<task>/<run>/lstest-patch/...

② bszT14 收集并复核
   python tools/knowledge-pipeline/collect_lstest_patch.py \
       --node <node> --task <issue> --run <run>
   → 下载 manifest 与文件，逐项核对 sha256 / 字节数 / base_commit / checks / log_sample
   → POST 8091 /v1/proposals（kind=profile_rule|marker_code|framework_fix，pending）

③ 审核通过后推广为分支
   python tools/knowledge-pipeline/promote_candidate.py --proposal-id <prop-id>
   → 基于 base_commit 建 candidate/<prop-id>，写入 payload，提交，推送分支
   → 人工合并 main → 发版
```

## 候选契约要点

- `publish_target` 决定终点：`ark-kb` / `lstest` / `service:*`
- lstest 类（`marker_code` / `profile_rule` / `framework_fix`）条件必填：
  `target_path`、`base_commit`、`payload`、`checks`、`log_sample`
- `log_sample` ≤ 50 行、每行 ≤ 400 字，作为最小复现样本，同时可作回归夹具
- `base_commit` 是出生证明：审核员据此判断是否需要 rebase，推广时用它作为分支起点

## 安全边界

- `target_path` 拒绝绝对路径与 `..`
- 推广前校验 `base_commit` 在仓库中可达，否则拒绝
- 正式推广要求候选状态 `accepted`；`--allow-pending` 仅供自测
- `--no-push` / `--dry-run` 用于自测，不产生远端分支
