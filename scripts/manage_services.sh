#!/usr/bin/env bash
# GMAWGraph 服务管理脚本（维护 conda 环境 / Neo4j / vLLM 服务的启停）
# 用法：bash scripts/manage_services.sh {start|stop|status|restart}
set -u

ENV_PY=/ENV/Anaconda/envs/jm/GMAWGraph/bin
NEO4J_HOME=/DATA/jm/neo4j/GMAWGraph
LLM_LOG=/DATA/jm/GMAWGraph_llm_8000.log
EMBED_LOG=/DATA/jm/GMAWGraph_embed_8001.log

case "${1:-}" in
  start)
    echo "[1/3] 启动 Neo4j (Bolt 7200 / HTTP 7201) ..."
    "$NEO4J_HOME/bin/neo4j" start
    echo "[2/3] 启动 Qwen3-32B vLLM (GPU0, :8000) ..."
    if ! curl -s -o /dev/null http://127.0.0.1:8000/health 2>/dev/null; then
      CUDA_VISIBLE_DEVICES=0 nohup "$ENV_PY/vllm" serve /DATA/jm/llms/qwen3-32b \
        --served-model-name qwen3-32b --port 8000 --max-model-len 32768 \
        --gpu-memory-utilization 0.90 --enable-prefix-caching > "$LLM_LOG" 2>&1 &
      echo "   已后台启动（日志 $LLM_LOG，加载约 3-5 分钟）"
    else
      echo "   已在运行"
    fi
    echo "[3/3] 启动 BGE-M3 嵌入 (GPU1, :8001) ..."
    if ! curl -s -o /dev/null http://127.0.0.1:8001/health 2>/dev/null; then
      CUDA_VISIBLE_DEVICES=1 nohup "$ENV_PY/vllm" serve /DATA/jm/llms/bge-m3 \
        --served-model-name bge-m3 --port 8001 --max-model-len 8192 > "$EMBED_LOG" 2>&1 &
      echo "   已后台启动（日志 $EMBED_LOG）"
    else
      echo "   已在运行"
    fi
    ;;
  stop)
    echo "停止 vLLM 服务与 Neo4j ..."
    pkill -f "GMAWGraph/bin/vll[m]" 2>/dev/null
    "$NEO4J_HOME/bin/neo4j" stop
    ;;
  status)
    "$NEO4J_HOME/bin/neo4j" status
    curl -s -o /dev/null -w "Qwen3-32B (:8000): HTTP %{http_code}\n" http://127.0.0.1:8000/health 2>/dev/null || echo "Qwen3-32B (:8000): 未运行"
    curl -s -o /dev/null -w "BGE-M3     (:8001): HTTP %{http_code}\n" http://127.0.0.1:8001/health 2>/dev/null || echo "BGE-M3     (:8001): 未运行"
    ;;
  restart)
    bash "$0" stop
    sleep 2
    bash "$0" start
    ;;
  *)
    echo "用法：bash scripts/manage_services.sh {start|stop|status|restart}"
    exit 1
    ;;
esac
