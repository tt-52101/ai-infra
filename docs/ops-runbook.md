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
VLLM_IMAGE=lmcache/vllm-openai:v0.4.5-cu129
LMCACHE_IMAGE=lmcache/standalone:v0.4.5-cu129
LMCACHE_MP_PORT=6555
VLLM_API_KEY=sk-change-123
GATEWAY_API_KEY=sk-change-123
NCCL_DEBUG=WARN
NCCL_CUMEM_ENABLE=0
NCCL_CUMEM_HOST_ENABLE=0
VLLM_WORKER_MULTIPROC_METHOD=spawn
OMP_NUM_THREADS=1
```

当前 Compose 按 LMCache 官方 Docker 示例使用 host 网络等价部署。Prefill、Decode 和 LMCache 只绑定 `127.0.0.1` 上的内部端口，Gateway 是唯一对外业务入口。

如果目标机访问 `pypi.org` 超时，配置 Gateway 镜像构建使用可访问的 PyPI 镜像源：

```bash
PIP_INDEX_URL=https://pypi.org/simple
PIP_DEFAULT_TIMEOUT=120
PIP_RETRIES=10
```

内网或受限网络环境可把 `PIP_INDEX_URL` 改成公司镜像源；如镜像源使用 HTTP 或私有证书，再按需设置 `PIP_TRUSTED_HOST`。

## 3. 常用命令

Linux:

```bash
bash ops/pd-stack.sh config
bash ops/pd-stack.sh doctor
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
.\ops\pd-stack.ps1 doctor
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
bash ops/pd-stack.sh repair prefill
```

如果日志中仍然出现 `--model` 弃用提示、`disable_custom_all_reduce=False`、`latest-nightly` 或 `free(): double free detected`，不要只执行 `restart`。先执行：

```bash
bash ops/pd-stack.sh doctor
bash ops/pd-stack.sh repair prefill
```

`doctor` 会检查渲染后的 Compose 是否仍包含旧镜像或旧 vLLM 参数；`repair` 会在检查通过后使用 `--force-recreate --remove-orphans` 强制重建目标服务，避免旧容器继续沿用历史启动命令。

## 5. 验证标准

上线 MVP 前至少执行：

```bash
bash ops/pd-stack.sh config
bash ops/pd-stack.sh doctor
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
| vLLM 启动后出现 `free(): double free detected` 或 NCCL worker 初始化失败 | 避免使用 nightly 镜像；确认 `VLLM_IMAGE=lmcache/vllm-openai:v0.4.5-cu129`、模型路径使用位置参数 `/model`、Prefill/Decode 均保留 `--disable-custom-all-reduce`、`NCCL_CUMEM_ENABLE=0`、`NCCL_CUMEM_HOST_ENABLE=0`、`VLLM_WORKER_MULTIPROC_METHOD=spawn` 和 `NCCL_DEBUG=WARN`，然后执行 `bash ops/pd-stack.sh repair prefill` |
| 宿主机外部能访问 `8001` / `8002` / `6555` | 检查 vLLM 是否仍有 `--host 127.0.0.1`，LMCache 是否仍有 `--http-host 127.0.0.1` |
| `lmcache/lmcache-server` 拉取失败 | 当前不再使用该历史仓库镜像，确认 `LMCACHE_IMAGE=lmcache/standalone:v0.4.5-cu129` 或目标机验证过的 standalone tag |
| Gateway 镜像构建时 pip 访问 PyPI 超时 | 在 `compose/.env` 配置 `PIP_INDEX_URL`、`PIP_DEFAULT_TIMEOUT`、`PIP_RETRIES` 后重新执行 `bash ops/pd-stack.sh build gateway` |
| gateway 返回 401 | `GATEWAY_API_KEY` 是否与请求 Bearer token 一致 |
| gateway 返回 prefill failed | `vllm-prefill` 日志、LMCache 健康检查、`UPSTREAM_API_KEY` |
| TTFT 没有下降 | LMCache 日志、长前缀是否完全一致、KV connector 版本兼容性 |
