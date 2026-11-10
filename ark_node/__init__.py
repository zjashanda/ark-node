"""ark-node：Ark 节点统一入口。

四个子域，共用同一套契约（时间、退出码、JSON 输出、凭据边界）：

- ``env``      环境变量同步（真源由 ark-env 服务分发，本仓库只做客户端）
- ``identity`` 节点身份解析（node_id / hostname / 平台 / 角色）
- ``init``     任务目录与运行时目录初始化（幂等）
- ``probe``    节点能力探测（输出 ark-node-capability.v1）
- ``fetch``    安全取件（artifact manifest / SHA-256 / quarantine）

设计原则：只用标准库；绝不打印、写入、提交任何密码、token、私钥；
只引用环境变量名，不内联凭据值。
"""

__version__ = "0.1.0"
SCHEMA_CAPABILITY = "ark-node-capability.v1"
SCHEMA_MANIFEST = "ark-node-manifest.v1"
SCHEMA_ENV_STATE = "ark-node-env-state.v1"
