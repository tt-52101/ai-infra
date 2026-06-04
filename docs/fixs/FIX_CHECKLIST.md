# vLLM Prefill-Decode 部署方案修复操作清单 | 版本：v1.0 | 日期：2026-06-04 | 状态：待执行

> 本文档为 [docker-compose.yml](file:///d:/ai-infra/compose/docker-compose.yml) 及 [lmcache_config.yaml](file:///d:/ai-infra/compose/lmcache_config.yaml) 的修复操作清单。
> 请按步骤顺序逐项执行，每完成一项在「执行结果」栏标记，**Phase 1 全部完成后方可进入 Phase 2**。

---

## 执行前准备

| # | 准备项 | 说明 | 状态 |
|---|--------|------|------|
| PRE-1 | 备份当前配置文件 | `cp docker-compose.yml docker-compose.yml.bak` | ☐ |
| PRE-2 | 备份 LMCache 配置 | `cp lmcache_config.yaml lmcache_config.yaml.bak` | ☐ |
| PRE-3 | 确认 vLLM 版本 | 检查 `vllm/vllm-openai:latest` 对应的实际版本号（用于后续固定） | ☐ |
| PRE-4 | 生成 API Key | 生成安全的 API 密钥字符串，用于 vLLM 认证 | ☐ |
| PRE-5 | 停止当前运行的服务（如有） | `docker compose down` | ☐ |

---

## Phase 1 — 阻塞修复（上线前必须完成）

> **预计耗时**：2-4 小时  
> **验收标准**：所有 P0 项完成 → 冒烟测试全部通过

### 1.1 补全 Prefill-Decode 模式启用参数

| 步骤 | 操作项 | 具体操作 | 目标文件 | 行号参考 | 执行结果 |
|------|--------|----------|----------|----------|----------|
| P1-01 | 为 Prefill 节点添加 `--enable-prefill-decode-mode` 参数 | 在 `command:` 段末尾追加一行 `--enable-prefill-decode-mode` | docker-compose.yml | ~L44-L51 | ☐ |
| P1-02 | 为 Prefill 节点添加 `--role prefill` 参数 | 在 command 段追加 `--role prefill` | docker-compose.yml | ~L44-L51 | ☐ |
| P1-03 | 为 Decode 节点添加 `--enable-prefill-decode-mode` 参数 | 在 `command:` 段末尾追加一行 `--enable-prefill-decode-mode` | docker-compose.yml | ~L82-L89 | ☐ |
| P1-04 | 为 Decode 节点添加 `--role decode` 参数 | 在 command 段追加 `--role decode` | docker-compose.yml | ~L82-L89 | ☐ |

**验证方法**：
```bash
# 启动后检查日志是否包含 PD 模式相关输出
docker logs vllm-prefill-cluster 2>&1 | grep -i "prefill.*decode\|pd.*mode"
docker logs vllm-decode-cluster 2>&1 | grep -i "prefill.*decode\|pd.*mode"
```

---

### 1.2 安全加固 — 移除 LMCache 外部端口暴露

| 步骤 | 操作项 | 具体操作 | 目标文件 | 行号参考 | 执行结果 |
|------|--------|----------|----------|----------|----------|
| P1-05 | 删除 lmcache-server 的 ports 映射 | 将 `ports:` 和 `- "65432:65432"` 两行删除或注释 | docker-compose.yml | L8-L9 | ☐ |
| P1-06 | 改用 expose 仅内部访问 | 在删除 ports 后，添加 `expose: ["65432"]` | docker-compose.yml | L9 之后 | ☐ |

**修改前**：
```yaml
    ports:
      - "65432:65432"
```

**修改后**：
```yaml
    expose:
      - "65432"
```

**验证方法**：
```bash
# 外部应无法访问
curl -s http://localhost:65432/health && echo "FAIL: port exposed" || echo "PASS: port not exposed"

# 内部容器间应可访问
docker exec vllm-prefill-cluster curl -sf http://lmcache-server:65432/health && echo "PASS" || echo "FAIL"
```

---

### 1.3 安全加固 — 添加 API Key 认证

| 步骤 | 操作项 | 具体操作 | 目标文件 | 行号参考 | 执行结果 |
|------|--------|----------|----------|----------|----------|
| P1-07 | 创建 `.env` 文件存放密钥 | 新建文件 `d:\ai-infra\compose\.env`，写入 `VLLM_API_KEY=sk-<your-secure-key>` | .env (新建) | N/A | ☐ |
| P1-08 | Prefill 节点添加 `--api-key` | 在 command 段追加 `--api-key "${VLLM_API_KEY}"` | docker-compose.yml | ~L44-L51 | ☐ |
| P1-09 | Decode 节点添加 `--api-key` | 在 command 段追加 `--api-key "${VLLM_API_KEY}"` | docker-compose.yml | ~L82-L89 | ☐ |
| P1-10 | 在 env_file 或 environment 中加载 .env | 确保 Docker Compose 能读取环境变量（Compose V2 自动读取） | docker-compose.yml | 文件顶部 | ☐ |

**验证方法**：
```bash
# 无 Key 应返回 401
curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"/model","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'
# 期望: 401

# 有 Key 应返回 200
curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-<your-secure-key>" \
  -d '{"model":"/model","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'
# 期望: 200
```

---

### 1.4 添加健康检查机制

| 步骤 | 操作项 | 具体操作 | 目标文件 | 行号参考 | 执行结果 |
|------|--------|----------|----------|----------|------|
| P1-11 | 为 lmcache-server 添加 healthcheck | 在 `restart:` 之后插入 healthcheck 配置块 | docker-compose.yml | L14 之后 | ☐ |
| P1-12 | 为 vllm-prefill 添加 healthcheck | 在 `restart:` 之后插入 healthcheck 配置块（start_period: 300s） | docker-compose.yml | L53 之后 | ☐ |
| P1-13 | 为 vllm-decode 添加 healthcheck | 在 `restart:` 之后插入 healthcheck 配置块（start_period: 300s） | docker-compose.yml | L92 之后 | ☐ |

**lmcache-server 健康检查配置**：
```yaml
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:65432/health"]
      interval: 20s
      timeout: 5s
      retries: 3
      start_period: 10s
```

**vLLM 服务健康检查配置**（Prefill 和 Decode 都要加）：
```yaml
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:{port}/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 300s  # 模型加载需要较长时间
```

**验证方法**：
```bash
# 等待服务就绪后查看健康状态
docker inspect --format='{{.State.Health.Status}}' lmcache-server
docker inspect --format='{{.State.Health.Status}}' vllm-prefill-cluster
docker inspect --format='{{.State.Health.Status}}' vllm-decode-cluster
# 期望输出: healthy
```

---

### 1.5 固定镜像版本号

| 步骤 | 操作项 | 具体操作 | 目标文件 | 行号引用 | 执行结果 |
|------|--------|----------|----------|----------|----------|
| P1-14 | 查询当前 latest 镜像实际版本 | 执行 `docker image inspect vllm/vllm-openai:latest --format '{{.Id}}'` 并记录 digest | 终端 | N/A | ☐ |
| P1-15 | 替换 vllm-prefill 镜像标签 | 将 `image: vllm/vllm-openai:latest` 改为固定版本号 | docker-compose.yml | L20 | ☐ |
| P1-16 | 替换 vllm-decode 镜像标签 | 将 `image: vllm/vllm-openai:latest` 改为相同固定版本号 | docker-compose.yml | L59 | ☐ |
| P1-17 | 替换 lmcache-server 镜像标签 | 将 `image: lmcache/lmcache-server:latest` 改为固定版本号 | docker-compose.yml | L6 | ☐ |
| P1-18 | 记录锁定版本信息到清单底部 | 在本文档「版本锁定记录」区填写 | 本文档 | 底部 | ☐ |

**示例**（请以实际查询结果为准）：
```yaml
# 修改前
image: vllm/vllm-openai:latest
# 修改后（示例版本号）
image: vllm/vllm-openai:v0.6.3.post1
```

---

### 1.6 修正 LMCache backend 配置

| 步骤 | 操作项 | 具体操作 | 目标文件 | 行号引用 | 执行结果 |
|------|--------|----------|----------|----------|------|
| P1-19 | 将 backend 从 `"gpu"` 改为 `"remote_only"` | 避免 GPU 显存竞争，所有 KV Cache 通过远程 Server 存储/获取 | lmcache_config.yaml | L2 | ☐ |
| P1-20 | 降低 local_cpu_percentage 至 `0.25` | 双容器各 25% = 总计 50%，留余量给 LMCache Server 和系统 | lmcache_config.yaml | L3 | ☐ |

**修改前**：
```yaml
backend: "gpu"
local_cpu_percentage: 0.4
```

**修改后**：
```yaml
backend: "remote_only"
local_cpu_percentage: 0.25
```

**验证方法**：
```bash
# 检查 LMCache Server 日志确认无 GPU 相关报错
docker logs lmcache-server 2>&1 | grep -i "error\|warn\|cuda\|backend"
# 应无 CUDA/backend 错误
```

---

### 1.7 Phase 1 验收 — 冒烟测试

| # | 测试项 | 测试命令 | 预期结果 | 通过 |
|---|--------|----------|----------|------|
| SMOKE-01 | 所有容器运行中 | `docker compose ps` | 3 个服务均为 running 状态 | ☐ |
| SMOKE-02 | Prefill 健康检查通过 | `docker inspect --format '{{.State.Health.Status}}' vllm-prefill-cluster` | `healthy` | ☐ |
| SMOKE-03 | Decode 健康检查通过 | `docker inspect --format '{{.State.Health.Status}}' vllm-decode-cluster` | `healthy` | ☐ |
| SMOKE-04 | LMCache 健康检查通过 | `docker inspect --format '{{.State.Health.Status}}' lmcache-server` | `healthy` | ☐ |
| SMOKE-05 | PD 模式已激活 | `docker logs vllm-prefill-cluster 2>&1 \| grep -i "prefill.*decode"` | 日志含 PD 模式标识 | ☐ |
| SMOKE-06 | PD 模式已激活 (Decode) | `docker logs vllm-decode-cluster 2>&1 \| grep -i "prefill.*decode"` | 日志含 PD 模式标识 | ☐ |
| SMOKE-07 | API Key 认证生效 | 无 Key 调用返回 401 | HTTP 401 | ☐ |
| SMOKE-08 | 带 Key 推理正常 | POST /v1/chat/completions 带 Authorization header | HTTP 200 + 正常返回 | ☐ |
| SMOKE-09 | LMCache 端口未对外暴露 | `curl -sf http://localhost:65432/health` | 连接失败/拒绝 | ☐ |
| SMOKE-10 | GPU 分配正确 (Prefill) | `docker exec vllm-prefill-cluster nvidia-smi -L` | 仅显示 GPU 0,1,2,3 | ☐ |
| SMOKE-11 | GPU 分配正确 (Decode) | `docker exec vllm-decode-cluster nvidia-smi -L` | 仅显示 GPU 4,5,6,7 | ☐ |
| SMOKE-12 | LMCache 内部通信正常 | vLLM 日志无连接失败错误 | Connected / 无 error | ☐ |

> **全部 12 项通过后方可进入 Phase 2**

---

## Phase 2 — 生产加固（上线后 1-2 周内完成）

> **预计耗时**：1-2 天  
> **验收标准**：所有 P1 项完成 → 基准压测通过 → 监控大盘可观测

### 2.1 添加资源上限限制

| 步骤 | 操作项 | 具体操作 | 目标文件 | 行号引用 | 执行结果 |
|------|--------|----------|----------|----------|------|
| P2-01 | 为 vllm-prefill 添加 memory limit | 在 `deploy.resources.reservations` 同级添加 `limits.memory` | docker-compose.yml | L37-L42 | ☐ |
| P2-02 | 为 vllm-decode 添加 memory limit | 同上 | docker-compose.yml | L76-L81 | ☐ |
| P2-03 | 为 lmcache-server 添加 memory limit | 添加 deploy.resources.limits | docker-compose.yml | L12 之后 | ☐ |

**配置模板**：
```yaml
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              ids: ['0', '1', '2', '3']
              capabilities: [gpu]
        limits:
          memory: 128GB  # 根据宿主机物理内存调整
```

---

### 2.2 降低显存利用率防 OOM

| 步骤 | 操作项 | 具体操作 | 目标文件 | 行号引用 | 执行结果 |
|------|--------|----------|----------|----------|------|
| P2-04 | Prefill gpu-memory-utilization 降至 0.80 | 将 `--gpu-memory-utilization 0.85` 改为 `0.80` | docker-compose.yml | L48 | ☐ |
| P2-05 | Decode gpu-memory-utilization 降至 0.80 | 同上 | docker-compose.yml | L87 | ☐ |
| P2-06 | 可选：降低 max-model-len 至 16384 | 若仍频繁 OOM，将 `--max-model-len 32768` 改为 `16384` | docker-compose.yml | L49, L88 | ☐ |

---

### 2.3 补充 vLLM 性能调优参数

| 步骤 | 操作项 | 具体操作 | 目标文件 | 适用节点 | 执行结果 |
|------|--------|----------|----------|----------|------|
| P2-07 | 添加 `--enable-prefix-caching` | 启用前缀缓存，与 LMCache 协作必需 | docker-compose.yml | 两者都加 | ☐ |
| P2-08 | 添加 `--max-num-seqs 256` | 控制最大并发序列数，防止 OOM | docker-compose.yml | 两者都加 | ☐ |
| P2-09 | 添加 `--max-num-batched-tokens 8192` | 批处理 token 上限，稳定吞吐量 | docker-compose.yml | 两者都加 | ☐ |
| P2-10 | 添加 `--dtype float16` | 明确数据类型精度 | docker-compose.yml | 两者都加 | ☐ |
| P2-11 | Decode 节点专属：`--scheduler-delay-factor 0.1` | 降低调度延迟，优化流式输出体验 | docker-compose.yml | 仅 Decode | ☐ |
| P2-12 | Decode 节点专属：`--max-num-seqs 512` | Decode 节点可承受更高并发 | docker-compose.yml | 仅 Decode | ☐ |

**修改后的完整 command 示例（Prefill）**：
```yaml
    command: >
      --model /model
      --quantization awq
      --tensor-parallel-size 4
      --trust-remote-code
      --gpu-memory-utilization 0.80
      --max-model-len 16384
      --port 8001
      --enable-prefill-decode-mode
      --role prefill
      --api-key "${VLLM_API_KEY}"
      --lmcache-backend remote_only
      --enable-prefix-caching
      --max-num-seqs 256
      --max-num-batched-tokens 8192
      --dtype float16
```

**修改后的完整 command 示例（Decode）**：
```yaml
    command: >
      --model /model
      --quantization awq
      --tensor-parallel-size 4
      --trust-remote-code
      --gpu-memory-utilization 0.80
      --max-model-len 16384
      --port 8002
      --enable-prefill-decode-mode
      --role decode
      --api-key "${VLLM_API_KEY}"
      --lmcache-backend remote_only
      --enable-prefix-caching
      --max-num-seqs 512
      --max-num-batched-tokens 8192
      --dtype float16
      --scheduler-delay-factor 0.1
```

---

### 2.4 配置日志轮转策略

| 步骤 | 操作项 | 具体操作 | 目标文件 | 行号引用 | 执行结果 |
|------|--------|----------|----------|----------|------|
| P2-13 | 为 vllm-prefill 添加 logging 配置 | 在 service 级别添加 logging 段 | docker-compose.yml | container_name 之后 | ☐ |
| P2-14 | 为 vllm-decode 添加 logging 配置 | 同上 | docker-compose.yml | container_name 之后 | ☐ |
| P2-15 | 为 lmcache-server 添加 logging 配置 | 同上 | docker-compose.yml | container_name 之后 | ☐ |

**配置模板**：
```yaml
    logging:
      driver: json-file
      options:
        max-size: "100m"
        max-file: "5"
        labels: "service,environment"
```

**验证方法**：
```bash
# 运行一段时间后检查日志大小
docker inspect --format='{{.HostConfig.LogConfig}}' vllm-prefill-cluster
# 确认 max-size 和 max-file 已生效
```

---

### 2.5 启用 Prometheus 监控指标导出

| 步骤 | 操作项 | 具体操作 | 目标文件 | 行号引用 | 执行结果 |
|------|--------|----------|----------|----------|------|
| P2-16 | Prefill 添加 VLLM_METRICS_ENABLED 环境变量 | 在 environment 段追加 | docker-compose.yml | L24-L31 | ☐ |
| P2-17 | Decode 添加 VLLM_METRICS_ENABLED 环境变量 | 在 environment 段追加 | docker-compose.yml | L63-L70 | ☐ |

**追加内容**：
```yaml
      - VLLM_METRICS_ENABLED=true
```

**验证方法**：
```bash
# 检查 metrics 端点是否可用
curl -s http://localhost:8001/metrics | head -20
# 期望: 返回 Prometheus 格式的指标数据
```

---

### 2.6 数据持久化路径规范化

| 步骤 | 操作项 | 具体操作 | 目标文件 | 行号引用 | 执行结果 |
|------|--------|----------|----------|----------|------|
| P2-18 | 将 LMCache 存储卷改为绝对路径 | 将 `./lmcache_storage:/tmp/lmcache_storage` 改为规范路径 | docker-compose.yml | L12-L13 | ☐ |

**建议改为**：
```yaml
    volumes:
      - /data/lmcache/storage:/var/lib/lmcache/storage
```
> 注意：需提前在宿主机创建 `/data/lmcache/storage` 目录并设置适当权限。

---

### 2.7 添加优雅关闭配置

| 步骤 | 操作项 | 具体操作 | 目标文件 | 行号引用 | 执行结果 |
|------|--------|----------|----------|----------|------|
| P2-19 | 为 vllm-prefill 添加 stop_grace_period | 在 restart 之前添加 | docker-compose.yml | L53 之前 | ☐ |
| P2-20 | 为 vllm-decode 添加 stop_grace_period | 同上 | docker-compose.yml | L92 之前 | ☐ |

**配置**：
```yaml
    stop_grace_period: 120s
```

---

### 2.8 添加容器元数据标签

| 步骤 | 操作项 | 具体操作 | 目标文件 | 行号引用 | 执行结果 |
|------|--------|----------|----------|----------|------|
| P2-21 | 为所有三个 service 添加 labels | 在 service 顶层添加 labels 段 | docker-compose.yml | 各 service | ☐ |

**配置模板**：
```yaml
    labels:
      - "team=ai-infra"
      - "environment=production"
      - "project=vllm-pd-cluster"
```

---

### 2.9 Phase 2 验收 — 基准压测

| # | 测试项 | 测试方法 | 通过标准 | 通过 |
|---|--------|----------|----------|------|
| PERF-01 | 资源限制生效 | `docker stats --no-stream` | memory 不超过 limits 值 | ☐ |
| PERF-02 | 日志轮转生效 | 检查日志文件数量和大小 | 单文件 ≤100MB，最多 5 个 | ☐ |
| PERF-03 | Metrics 端口可达 | `curl -s http://localhost:8001/metrics` | 返回 Prometheus 指标 | ☐ |
| PERF-04 | TTFT 基准测试 | 发送 10 条 512-token 请求，测量首字延迟 | P50 < 500ms, P99 < 2000ms | ☐ |
| PERF-05 | TPOT 基准测试 | 测量每 token 输出延迟 | P50 < 50ms, P99 < 150ms | ☐ |
| PERF-06 | 并发稳定性测试 | 50 并发持续 10 分钟 | 错误率 < 1%，无 OOM | ☐ |
| PERF-07 | LMCache 命中率检查 | 观察 LMCache Server 日志/指标 | 命中率 > 60%（取决于请求重复度） | ☐ |
| PERF-08 | 优雅关闭测试 | `docker compose stop` 后观察 | 容器在 120s 内完成关闭 | ☐ |

---

## 版本锁定记录

| 镜像 | 锁定版本 | Digest（可选） | 锁定日期 | 操作人 |
|------|----------|----------------|----------|--------|
| vllm/vllm-openai | _______________ | _______________ | ____-__-__ | |
| lmcache/lmcache-server | _______________ | _______________ | ____-__-__ | |

---

## 执行记录

| 阶段 | 开始时间 | 完成时间 | 执行人 | 验收人 | 备注 |
|------|----------|----------|--------|--------|------|
| Phase 1 | | | | | |
| Phase 2 | | | | | |

---

## 回滚方案

若修复后出现严重问题，按以下步骤回滚：

```bash
# 1. 停止当前服务
docker compose down

# 2. 恢复备份
cp docker-compose.yml.bak docker-compose.yml
cp lmcache_config.yaml.bak lmcache_config.yaml

# 3. 重新启动（恢复到修复前状态）
docker compose up -d

# 4. 验证回滚后服务正常
docker compose ps
```
