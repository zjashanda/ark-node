#!/bin/sh
# NoahsArk 仓库同步与版本门禁（Linux 节点，A→C 双通道）
#
# A：git clone/fetch + checkout 锚定 commit（重试 3 次）
# C：A 失败时从 OSS 下载完整 bundle，校验 sha256 后 git clone 本地
#
# 用法：
#   sh ark-repo-sync.sh                  # 同步默认仓库
#   sh ark-repo-sync.sh lstest ark-kb    # 指定仓库
#   sh ark-repo-sync.sh --check          # 只校验不拉取
#   sh ark-repo-sync.sh --install-task   # 注册 cron 每 30 分钟自动同步
#
# 退出码：0 通过；2 A/C 均失败；4 索引不可读；5 缺少 git

set -u

WORKSPACE="${ARK_WORKSPACE:-/home/zhsh/workspace}"
INDEX_URL="${ARK_INDEX_URL:-https://listenai-test-internal.oss-cn-beijing.aliyuncs.com/noahsark/index.json}"
REPO_BASE="${ARK_REPO_BASE:-https://listenai-test-internal.oss-cn-beijing.aliyuncs.com/noahsark/repos}"
LOG_DIR="$WORKSPACE/logs"
LOG_FILE="$LOG_DIR/repo-sync.log"
REPOS_ROOT="$WORKSPACE/repos"
CACHE_ROOT="$REPOS_ROOT/_cache"

CHECK_ONLY=0
INSTALL_TASK=0
REPOS=""

for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=1 ;;
        --install-task) INSTALL_TASK=1 ;;
        -*) echo "unknown flag: $arg" >&2; exit 64 ;;
        *) REPOS="$REPOS $arg" ;;
    esac
done
[ -z "$REPOS" ] && REPOS="lstest ark-kb burner ark-node"

mkdir -p "$LOG_DIR" "$REPOS_ROOT" "$CACHE_ROOT"

log() {
    line="$(date '+%Y-%m-%d %H:%M:%S') $*"
    echo "$line"
    echo "$line" >> "$LOG_FILE"
}

command -v git >/dev/null 2>&1 || { log "FATAL git-not-installed"; exit 5; }
command -v python3 >/dev/null 2>&1 || { log "FATAL python3-not-installed"; exit 5; }

META_JSON="$(mktemp)"
if ! curl -fsS --max-time 30 "$INDEX_URL" -o "$META_JSON"; then
    log "FATAL index-unreadable: $INDEX_URL"
    rm -f "$META_JSON"
    exit 4
fi

pin_of() {
    python3 - "$META_JSON" "git.$1" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
for item in data.get("items", []):
    if item.get("id") == sys.argv[2]:
        print(item.get("source_commit") or "", item.get("source_repo") or "")
        break
PY
}

install_task() {
    if ! command -v crontab >/dev/null 2>&1; then
        log "INSTALL-TASK skipped (no crontab)"
        return 0
    fi
    entry="*/30 * * * * ARK_WORKSPACE=$WORKSPACE sh $WORKSPACE/tools/ark-repo-sync.sh >> $LOG_FILE 2>&1"
    if crontab -l 2>/dev/null | grep -q "ark-repo-sync.sh"; then
        log "INSTALL-TASK already-present"
    else
        (crontab -l 2>/dev/null; echo "$entry") | crontab -
        log "INSTALL-TASK added: $entry"
    fi
}

try_channel_a() {
    path="$1"; url="$2"; pinned="$3"
    i=1
    while [ "$i" -le 3 ]; do
        if [ -d "$path/.git" ]; then
            git -C "$path" fetch --depth 1 --quiet origin "$pinned" >/dev/null 2>&1 \
                && git -C "$path" -c advice.detachedHead=false checkout --quiet "$pinned" >/dev/null 2>&1
            rc=$?
        else
            rm -rf "$path"
            git -c http.version=HTTP/1.1 clone --depth 1 --quiet "$url" "$path" >/dev/null 2>&1
            rc=$?
            if [ "$rc" -eq 0 ]; then
                git -C "$path" -c advice.detachedHead=false checkout --quiet "$pinned" >/dev/null 2>&1
                rc=$?
            fi
        fi
        if [ "$rc" -eq 0 ] && [ "$(git -C "$path" rev-parse HEAD 2>/dev/null)" = "$pinned" ]; then
            return 0
        fi
        log "  A try$i failed rc=$rc"
        sleep $((3 * i))
        i=$((i + 1))
    done
    return 1
}

try_channel_c() {
    name="$1"; path="$2"; pinned="$3"
    meta_url="$REPO_BASE/$name/latest.json"
    json="$(mktemp)"
    if ! curl -fsS --max-time 30 "$meta_url" -o "$json"; then
        log "  C meta-unavailable: $meta_url"
        rm -f "$json"
        return 1
    fi
    commit="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1],encoding="utf-8")).get("commit",""))' "$json")"
    bundle="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1],encoding="utf-8")).get("bundle",""))' "$json")"
    want_sha="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1],encoding="utf-8")).get("sha256",""))' "$json")"
    rm -f "$json"

    if [ "$commit" != "$pinned" ]; then
        log "  C meta-commit-mismatch meta=$commit pinned=$pinned"
        return 1
    fi
    cache="$CACHE_ROOT/$bundle"
    if [ ! -f "$cache" ]; then
        if ! curl -fsS --max-time 180 "$REPO_BASE/$name/$bundle" -o "$cache"; then
            log "  C download-failed"
            return 1
        fi
    fi
    got_sha="$(sha256sum "$cache" | awk '{print $1}')"
    if [ "$got_sha" != "$want_sha" ]; then
        log "  C sha-mismatch"
        rm -f "$cache"
        return 1
    fi
    rm -rf "$path"
    git clone --quiet "$cache" "$path" >/dev/null 2>&1 || { log "  C clone-failed"; return 1; }
    git -C "$path" -c advice.detachedHead=false checkout --quiet "$pinned" >/dev/null 2>&1 \
        || { log "  C checkout-failed"; return 1; }
    [ "$(git -C "$path" rev-parse HEAD 2>/dev/null)" = "$pinned" ]
}

failed=0
for name in $REPOS; do
    read -r pinned slug <<EOF
$(pin_of "$name")
EOF
    if [ -z "${pinned:-}" ] || [ -z "${slug:-}" ]; then
        log "SKIP $name (no git.$name in index)"
        continue
    fi

    path="$REPOS_ROOT/$name"
    if [ -d "$path/.git" ]; then
        head="$(git -C "$path" rev-parse HEAD 2>/dev/null)"
        if [ "$head" = "$pinned" ]; then
            log "OK $name ${pinned%????????} (already)"
            continue
        fi
        if [ "$CHECK_ONLY" -eq 1 ]; then
            log "FAIL $name stale head=${head%????????} pinned=${pinned%????????}"
            failed=$((failed + 1))
            continue
        fi
    elif [ "$CHECK_ONLY" -eq 1 ]; then
        log "FAIL $name missing $path"
        failed=$((failed + 1))
        continue
    fi

    channel="A"
    if ! try_channel_a "$path" "https://github.com/$slug.git" "$pinned"; then
        log "  -> fallback to C for $name"
        channel="C"
        if ! try_channel_c "$name" "$path" "$pinned"; then
            log "FAIL $name both-channels-failed"
            failed=$((failed + 1))
            continue
        fi
    fi
    echo "$pinned" > "$path/.ark_version"
    log "OK $name ${pinned%????????} via $channel -> $path"
done

rm -f "$META_JSON"

if [ "$INSTALL_TASK" -eq 1 ]; then
    install_task
fi

if [ "$failed" -gt 0 ]; then
    log "GATE BLOCKED_STALE_REPO failed=$failed"
    exit 2
fi
log "GATE PASS"
exit 0
