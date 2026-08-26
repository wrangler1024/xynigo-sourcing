#!/bin/bash
# Transactional updater for the self-contained macOS green package.
set -u

install_dir=""
stage_dir=""
backup_dir=""
parent_pid=0
work_dir=""
state_dir=""
skip_wait=0
no_restart=0
test_fail_after_install=0
test_restart_direct=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --install-dir) install_dir="$2"; shift 2 ;;
    --stage-dir) stage_dir="$2"; shift 2 ;;
    --backup-dir) backup_dir="$2"; shift 2 ;;
    --parent-pid) parent_pid="$2"; shift 2 ;;
    --work-dir) work_dir="$2"; shift 2 ;;
    --state-dir) state_dir="$2"; shift 2 ;;
    --skip-wait) skip_wait=1; shift ;;
    --no-restart) no_restart=1; shift ;;
    --test-fail-after-install) test_fail_after_install="$2"; shift 2 ;;
    --test-restart-direct) test_restart_direct=1; shift ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

if [ -z "$install_dir" ] || [ -z "$stage_dir" ] || [ -z "$backup_dir" ]; then
  echo "缺少更新目录参数" >&2
  exit 2
fi

install_dir="$(cd "$install_dir" 2>/dev/null && pwd -P)" || exit 2
stage_dir="$(cd "$stage_dir" 2>/dev/null && pwd -P)" || exit 2
if [ -z "$install_dir" ] || [ "$install_dir" = "/" ]; then
  echo "拒绝使用不安全的安装目录" >&2
  exit 2
fi
if [ -z "$state_dir" ]; then
  state_dir="$HOME/Library/Application Support/XynigoSourcing"
fi

managed_paths=(
  "runtime" "启动-Mac.command" "启动-本地执行器-Mac.command" "update-helper.sh"
  "VERSION.json" "使用说明.txt"
)
mkdir -p "$state_dir/logs"
log_path="$state_dir/logs/update-$(date +%Y%m%d-%H%M%S).log"

write_log() {
  local line
  line="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
  echo "$line"
  printf '%s\n' "$line" >> "$log_path"
}

retry() {
  local description="$1"
  shift
  local attempt=1
  while [ "$attempt" -le 10 ]; do
    if "$@"; then return 0; fi
    write_log "$description 失败，第 $attempt/10 次重试"
    attempt=$((attempt + 1))
    sleep 0.6
  done
  return 1
}

start_xynigo() {
  [ "$no_restart" -eq 1 ] && return 0
  local launcher="$install_dir/启动-Mac.command"
  if [ ! -f "$launcher" ]; then
    write_log "找不到启动-Mac.command，无法自动重启。"
    return 1
  fi
  mkdir -p "$state_dir"
  printf '1\n' > "$state_dir/skip-update-once"
  chmod +x "$launcher"
  if [ "$test_restart_direct" -eq 1 ]; then
    XYNIGO_SKIP_UPDATE_ONCE=1 /bin/bash "$launcher" >/dev/null 2>&1 &
  else
    XYNIGO_SKIP_UPDATE_ONCE=1 /usr/bin/open -a Terminal "$launcher"
  fi
}

write_log "准备更新 Xynigo Sourcing。"
write_log "安装目录：$install_dir"

if [ "$skip_wait" -eq 0 ] && [ "$parent_pid" -gt 0 ] 2>/dev/null; then
  write_log "等待旧程序退出，PID=$parent_pid"
  deadline=$((SECONDS + 90))
  while kill -0 "$parent_pid" 2>/dev/null && [ "$SECONDS" -lt "$deadline" ]; do
    sleep 0.5
  done
  if kill -0 "$parent_pid" 2>/dev/null; then
    write_log "旧程序在 90 秒内未退出"
    exit 1
  fi
fi

for name in "${managed_paths[@]}"; do
  if [ ! -e "$stage_dir/$name" ]; then
    write_log "更新包缺少受管文件：$name"
    exit 1
  fi
done

if [ -e "$backup_dir" ]; then
  backup_dir="${backup_dir}-$(date +%Y%m%d-%H%M%S)"
fi
mkdir -p "$backup_dir"
write_log "当前程序备份目录：$backup_dir"

moved_paths=()
installed_paths=()
failed=0
installed_count=0

for name in "${managed_paths[@]}"; do
  current="$install_dir/$name"
  if [ -e "$current" ]; then
    mkdir -p "$(dirname "$backup_dir/$name")"
    if retry "备份 $name" /bin/mv "$current" "$backup_dir/$name"; then
      moved_paths+=("$name")
    else
      failed=1
      break
    fi
  fi
done

if [ "$failed" -eq 0 ]; then
  for name in "${managed_paths[@]}"; do
    installed_paths+=("$name")
    if ! retry "安装 $name" /bin/cp -R "$stage_dir/$name" "$install_dir/$name"; then
      failed=1
      break
    fi
    installed_count=$((installed_count + 1))
    if [ "$test_fail_after_install" -gt 0 ] 2>/dev/null \
        && [ "$installed_count" -ge "$test_fail_after_install" ]; then
      write_log "测试注入：模拟替换失败"
      failed=1
      break
    fi
  done
fi

if [ "$failed" -eq 0 ]; then
  chmod +x "$install_dir/启动-Mac.command" \
    "$install_dir/启动-本地执行器-Mac.command" \
    "$install_dir/update-helper.sh"
  chmod +x "$install_dir/runtime/xynigo-sourcing"
  write_log "更新安装成功，用户配置和本地数据未被修改。"
  start_xynigo
  exit $?
fi

write_log "更新失败，开始回滚。"
rollback_failed=0
for name in "${installed_paths[@]}"; do
  target="$install_dir/$name"
  if [ -e "$target" ]; then
    if ! retry "清理新版本 $name" /bin/rm -rf "$target"; then
      rollback_failed=1
    fi
  fi
done
for name in "${moved_paths[@]}"; do
  source_path="$backup_dir/$name"
  if [ -e "$source_path" ]; then
    if ! retry "恢复 $name" /bin/mv "$source_path" "$install_dir/$name"; then
      rollback_failed=1
    fi
  fi
done

if [ "$rollback_failed" -eq 0 ]; then
  write_log "回滚完成，正在重新启动原版本。"
  start_xynigo || true
else
  write_log "回滚失败，需要人工从备份目录恢复。"
fi
exit 1
