"""命令行入口：子命令解析与分发。

约定
----
* 每个子命令把 ``argparse.Namespace`` 交给对应模块的 ``run()``，返回退出码。
* 模块按需导入：``ark-node identity`` 不需要网络与 fetch 依赖。
* ``fetch`` 子命令保留 ark-mesh 的历史参数形态（status/index/fetch/archive），
  其余 argv 原样透传，方便旧脚本平滑迁移。
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import __version__, util


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ark-node",
        description="Ark 节点统一入口：环境同步 / 身份 / 任务目录 / 能力探测 / 安全取件",
    )
    parser.add_argument("--version", action="version", version=f"ark-node {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # ---------------------------------------------------------------- env
    env = sub.add_parser("env", help="同步节点环境变量（真源：ark-env HTTP 分发）")
    env.add_argument("--base", default=None, help="分发服务基址，默认自动探测")
    env.add_argument("--user", default="ark", help="HTTP Basic 用户名（默认 ark）")
    env.add_argument("--pass-stdin", action="store_true", help="从 stdin 读取口令，避免命令行留痕")
    env.add_argument("--auth-file", default=None, help="口令文件，默认 ~/.config/ark/auth")
    env.add_argument(
        "--scope",
        default="user",
        choices=["user", "system", "machine"],
        help="写入作用域：user(~/.config/ark/env) / system(/etc/ark/env) / machine(Windows 机器级)",
    )
    env.add_argument("--diff", action="store_true", help="只打印将要变化的键名")
    env.add_argument("--apply", action="store_true", help="真正落盘（缺省为 dry-run）")
    env.add_argument("--offline", default=None, help="离线快照 JSON 路径，跳过网络")
    env.add_argument("--timeout", type=float, default=10.0, help="单次 HTTP 超时秒数")
    env.add_argument("--json", action="store_true", help="机器可读输出")

    # ----------------------------------------------------------- identity
    ident = sub.add_parser("identity", help="解析节点身份")
    ident.add_argument("--node-id", default=None, help="显式指定 node_id（最高优先级）")
    ident.add_argument("--root", default=None, help="工作根目录，默认 $HOME")
    ident.add_argument("--json", action="store_true", help="机器可读输出")

    # --------------------------------------------------------------- init
    init = sub.add_parser("init", help="幂等初始化任务目录与运行时目录")
    init.add_argument("--task", default=None, help="任务 slug；缺省只初始化运行时目录")
    init.add_argument("--base-dir", default=None, help="任务根目录，默认 ~/.ark/tasks")
    init.add_argument("--dry-run", action="store_true", help="只输出将创建的目录")
    init.add_argument("--json", action="store_true", help="机器可读输出")

    # -------------------------------------------------------------- probe
    probe = sub.add_parser("probe", help="探测节点能力，输出 ark-node-capability.v1")
    probe.add_argument("--json", action="store_true", help="机器可读输出（默认即 JSON）")
    probe.add_argument("--out", default=None, help="同时写入指定文件")
    probe.add_argument("--deep", action="store_true", help="额外探测 git remote / 磁盘明细")

    # -------------------------------------------------------------- fetch
    fetch = sub.add_parser("fetch", help="安全取件（status/index/fetch/archive）")
    fetch.add_argument("argv", nargs=argparse.REMAINDER, help="透传给 fetch 子命令")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.command:
        build_parser().print_help(sys.stderr)
        return util.EXIT_USAGE

    try:
        if args.command == "env":
            from . import envsync

            return envsync.run(args)
        if args.command == "identity":
            from . import identity

            return identity.run(args)
        if args.command == "init":
            from . import initspace

            return initspace.run(args)
        if args.command == "probe":
            from . import probe

            return probe.run(args)
        if args.command == "fetch":
            from . import fetch as fetch_mod

            return fetch_mod.main(args.argv)
    except SystemExit as exc:  # 子模块用 SystemExit 表达退出码
        code = exc.code
        return code if isinstance(code, int) else util.EXIT_FAIL
    except KeyboardInterrupt:
        util.info("interrupted")
        return 130

    return util.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
