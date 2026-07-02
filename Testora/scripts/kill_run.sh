#!/usr/bin/env bash
# 杀宿主机上的 Testora 批量任务
pkill -9 -f 'testora.RegressionFinder' 2>/dev/null || true
pkill -9 -f 'scripts/run_cases.py' 2>/dev/null || true
pkill -9 -f 'scripts/run_testora.sh' 2>/dev/null || true

# 清理各 dev 容器内残留的测试进程（docker exec 起的孤儿进程）。
# 很多镜像没有 pkill，用 /proc 扫描 cmdline 再 kill。
kill_in_container() {
  local c="$1" pattern="$2"
  docker exec "$c" sh -c "
    for f in /proc/[0-9]*/cmdline; do
      cmd=\$(tr '\0' ' ' < \"\$f\" 2>/dev/null) || continue
      echo \"\$cmd\" | grep -qE '$pattern' || continue
      kill -9 \"\$(basename \"\$(dirname \"\$f\")\")\" 2>/dev/null || true
    done
  " 2>/dev/null || true
}

for c in $(docker ps --format '{{.Names}}' | grep -- '-dev'); do
  kill_in_container "$c" 'coverage run|Testora_test_code'
done

ps aux | grep -E 'RegressionFinder|run_cases|run_testora' | grep -v grep || true
