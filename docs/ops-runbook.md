# DeepSeek PD MVP 运维手册

## 1. 文件结构

Compose 按能力拆分：

| 文件 | 能力 |
| --- | --- |
| `compose/docker-compose.yml` | 基础网络 |
| `compose/docker-compose.lmcache.yml` | LMCache KV Cache 服务 |
| `compose/docker-compose.prefill.yml` | Prefill 节点 |
| `compose/docker-compose.decode.yml` | Decode 节点 |
| `compose/docker-compose.gateway.yml` | Gateway 统一入口 |

运维脚本：

| 脚本 | 适用环境 |
| --- | --- |
| `ops/pd-stack.sh` | Linux / Bash |
| `ops/pd-stack.ps1` | Windows / PowerShell |

## 2. 初始化

```bash
cd /path/to/ai-infra
cp compose/.env.example compose/.env
```

编辑 `compose/.env`，至少确认：

```bash
MODEL_PATH=/data/temp/yhb/DeepSeek-V4-Flash
VLLM_IMAGE=lmcache/vllm-openai:latest-nightly
LMCACHE_IMAGE=lmcache/standalone:nightly
LMCACHE_MP_PORT=6555
VLLM_API_KEY=sk-change-123
GATEWAY_API_KEY=sk-change-123
```

## 3. 常用命令

Linux:

```bash
bash ops/pd-stack.sh config
bash ops/pd-stack.sh up
bash ops/pd-stack.sh ps
bash ops/pd-stack.sh logs gateway
bash ops/pd-stack.sh restart decode
bash ops/pd-stack.sh verify
bash ops/pd-stack.sh down
```

PowerShell:

```powershell
.\ops\pd-stack.ps1 config
.\ops\pd-stack.ps1 up
.\ops\pd-stack.ps1 ps
.\ops\pd-stack.ps1 logs gateway
.\ops\pd-stack.ps1 restart decode
.\ops\pd-stack.ps1 verify
.\ops\pd-stack.ps1 down
```

## 4. Target 说明

脚本支持按能力操作：

| Target | 服务 |
| --- | --- |
| `all` | `lmcache-server`, `vllm-prefill`, `vllm-decode`, `gateway` |
| `cache` / `lmcache` | `lmcache-server` |
| `prefill` | `vllm-prefill` |
| `decode` | `vllm-decode` |
| `gateway` | `gateway` |

示例：

```bash
bash ops/pd-stack.sh logs prefill
bash ops/pd-stack.sh restart decode
bash ops/pd-stack.sh up gateway
```

## 5. 验证标准

上线 MVP 前至少执行：

```bash
bash ops/pd-stack.sh config
bash ops/pd-stack.sh up
bash ops/pd-stack.sh health
bash ops/pd-stack.sh verify
```

通过标准：

- `config` 能渲染完整 Compose。
- `ps` 中四个服务处于 running/healthy。
- 外部只能访问 gateway `8000`。
- 无 Bearer token 的推理请求返回 401。
- `verify` 中重复长前缀请求的 TTFT 相比冷请求下降。

## 6. 故障处理

| 症状 | 优先检查 |
| --- | --- |
| `vllm-prefill` 启动失败 | `MODEL_PATH`、GPU 0-3 可见性、模型是否支持 TP=4 |
| `vllm-decode` 启动失败 | GPU 4-7 物理绑定、容器内 CUDA 编号是否为 0-3 |
| `lmcache/lmcache-server` 拉取失败 | 当前不再使用该历史仓库镜像，确认 `LMCACHE_IMAGE=lmcache/standalone:nightly` 或目标机验证过的 standalone tag |
| gateway 返回 401 | `GATEWAY_API_KEY` 是否与请求 Bearer token 一致 |
| gateway 返回 prefill failed | `vllm-prefill` 日志、LMCache 健康检查、`UPSTREAM_API_KEY` |
| TTFT 没有下降 | LMCache 日志、长前缀是否完全一致、KV connector 版本兼容性 |
