## DeepSeek + vLLM + LMCache 部署方案审查结论

本节以 `compose/docker-compose.yml` 和 `compose/lmcache_config.yaml` 为最终定版 Docker 部署方案依据。下方原始内容保留为需求讨论背景，不再作为当前实施基线。

### 1. 最终定版 Docker 方案的事实摘要

最终方案不是单个 vLLM 服务占满 8 张卡的部署方式，而是采用 Prefill/Decode 分离的两段式推理架构：

- `lmcache-server`：作为集中式 KV Cache 共享存储，监听 `65432` 端口。
- `vllm-prefill`：绑定 GPU `0,1,2,3`，对外暴露 `8001`，负责长上下文 prefill 和 KV Cache 生产。
- `vllm-decode`：绑定 GPU `4,5,6,7`，对外暴露 `8002`，负责 decode 和流式输出。
- 两个 vLLM 节点均使用 `vllm/vllm-openai:latest`、`--tensor-parallel-size 4`、`--quantization awq`、`--max-model-len 32768`、`--gpu-memory-utilization 0.85`。
- 两个 vLLM 节点通过同一个 `lmcache-server://lmcache-server:65432` 共享 KV Cache。当前 LMCache 配置为 `chunk_size: 256`、`backend: "gpu"`、`local_cpu_percentage: 0.4`。
- 最终 Compose 中声明的模型路径是 `/data/temp/yhb/DeepSeek-V4-Flash:/model`。原讨论稿中的 `/data/models/DeepSeek-V3-AWQ` 不是当前定版配置。
- `backend/gateway.py` 已实现统一入口 `8000/v1/chat/completions` 的 Prefill 到 Decode 转发逻辑，但该网关当前没有纳入 `compose/docker-compose.yml`。因此，仅运行 Compose 只能启动 LMCache、Prefill 和 Decode 三个服务，不能自动得到统一的 `8000` 网关入口。

### 2. 对原需求文档方案的事实性反馈

原始讨论稿中“vLLM + LMCache + Docker Compose + OpenAI 兼容接口”的技术方向是成立的，但实现形态需要以最终 Compose 为准。

需要修正的点如下：

- 原始稿描述的是单个 `vllm-service` 使用 `TP=8`，最终方案已经改为 `vllm-prefill` 和 `vllm-decode` 两个 `TP=4` 节点。
- 原始稿中客户端直接请求 `localhost:8000` 的说法，只有在额外运行 `backend/gateway.py` 时才成立；最终 Compose 本身只暴露 `8001` 和 `8002`。
- “LMCache 解除 GPU 显存对长文本的限制”表述过于绝对。更准确的说法是：LMCache 可以复用和换出 KV Cache，降低重复长前缀请求的 prefill 成本和 TTFT，但单次请求的上下文上限仍受 `--max-model-len`、模型尺寸、并发量、GPU 显存和 CPU 内存约束。
- 使用 `latest` 镜像适合快速 MVP，但不适合作为生产可复现基线。生产阶段应固定 vLLM、LMCache、CUDA、NVIDIA Container Toolkit 和模型版本。
- 4090 无 NVLink/P2P 的约束已经在最终 Compose 中通过 `NCCL_P2P_DISABLE=1` 和 `NCCL_IB_DISABLE=1` 做了稳定性处理，但代价是跨卡通信性能会低于 H100/NVLink 环境。

### 3. MVP 验证可行性结论

结论：MVP 验证可行，但验证目标应限定为“在 8 张 4090 上验证 Prefill/Decode 分离、LMCache 共享 KV Cache、重复长前缀请求 TTFT 下降和 OpenAI 兼容转发链路”，不能把该 MVP 直接等同于生产级 DeepSeek-V3/R1 全量承载能力验证。

MVP 成立的前提条件：

- 宿主机具备 8 张可被 Docker 访问的 NVIDIA GPU，并已安装 NVIDIA 驱动、Docker、Docker Compose 和 NVIDIA Container Toolkit。
- `/data/temp/yhb/DeepSeek-V4-Flash` 指向真实存在且能在 4 卡 TP 下加载的 AWQ 模型权重。
- 宿主机具备足够 CPU 内存和 NVMe 存储，能够支撑 `local_cpu_percentage: 0.4` 的二级 KV Cache 与模型加载。
- 若要验证统一 OpenAI 入口，需要单独运行 `backend/gateway.py`，或把 gateway 增加为 Compose 服务。

建议的 MVP 通过标准：

- `docker compose -f compose/docker-compose.yml config` 能通过配置解析。
- `lmcache-server`、`vllm-prefill`、`vllm-decode` 能正常启动，且没有 OOM、NCCL 卡死或模型加载失败。
- 直接请求 `8001` 和 `8002` 均能得到 OpenAI 兼容响应。
- 运行网关后，请求 `8000/v1/chat/completions` 能先触发 Prefill，再转发到 Decode 并正常流式返回。
- 使用相同长系统 Prompt 连续请求至少 3 次，第二次及后续请求的 TTFT 相比冷启动请求有可观下降，并能从日志或指标中观察到 LMCache 命中或复用行为。
- 在目标并发下 GPU 显存、CPU 内存和磁盘缓存占用保持稳定，没有持续增长导致的 OOM 风险。

### 4. 总体架构判断

该方案适合作为 MVP 和技术验证基线，核心价值是把长上下文 prefill 成本与 decode 吞吐拆开，并通过 LMCache 验证跨节点 KV Cache 复用。它的主要风险不在架构方向，而在工程完整性：网关尚未容器化、镜像未固定版本、缺少健康检查、认证、限流、监控指标、缓存命中率观测和自动化压测脚本。

明确建议：当前可以进入 MVP 验证阶段，但生产化之前应先完成网关容器化、版本锁定、模型路径参数化、健康检查、日志指标、容量压测和故障降级策略。

---

## 原始部署方案讨论稿

为了在 8 张 RTX 4090 上部署 DeepSeek 的大规模混合专家模型（MoE），我们需要利用 vLLM 作为推理引擎，并通过 LMCache 来解决长文本/多轮对话下的 KV Cache 复用与分布式存储问题。

⚠️ 关于 DeepSeek 模型的特别说明：
截至 2026 年，DeepSeek 的主流生产力模型为 DeepSeek-V3 或 DeepSeek-R1（其参数量通常需要 8 张 H100 或 8 张 4090 通过高精度的 FP8/AWQ 量化来承载）。如果是运行 DeepSeek-V3/R1 的 AWQ/FP8 量化版本（约需要 600GB-700GB 显存，8×4090 拥有 192GB 显存，通常需要部署其 1.5B/7B/8B/14B/32B/70B 等蒸馏版本，或者极端量化版本），下文的架构完全通用。请根据你实际下载的 DeepSeek 准确模型权重路径修改 MODEL_PATH。

以下是完整的分布式部署方案，包含 Docker Compose 配置文件 以及 客户端使用方案。

一、 架构设计
vLLM Engine：利用 Tensor Parallelism (TP=8) 充分榨干 8 张 4090 的算力。

LMCache Server：独立于 vLLM 运行，负责接收 vLLM 换出的 KV Cache，并将其持久化到 CPU 内存或本地高速 NVMe 硬盘上，解除 GPU 显存对长文本的限制。

二、 Docker Compose 部署方案
创建部署目录（例如 /data/deepseek-deploy），并在该目录下编写以下文件：

1. docker-compose.yml
YAML
version: '3.8'

services:
  # LMCache 服务端：负责中心化管理 KV 缓存
  lmcache-server:
    image: lmcache/lmcache-server:latest
    container_name: lmcache-server
    ports:
      - "65432:65432"
    environment:
      - LMCACHE_SERVER_PORT=65432
    volumes:
      - ./lmcache_storage:/tmp/lmcache_storage  # 本地硬盘持久化缓存
    restart: unless-stopped
    networks:
      - llm-network

  # vLLM 推理服务：挂载 8 张 4090
  vllm-service:
    image: vllm/vllm-openai:latest
    container_name: vllm-deepseek
    ports:
      - "8000:8000"
    environment:
      - CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
      # LMCache 客户端配置
      - LMCACHE_ENABLE=True
      - LMCACHE_SERVER_ADDR=lmcache-server
      - LMCACHE_SERVER_PORT=65432
      - LMCACHE_CONFIG_FILE=/vllm-workspace/lmcache_config.yaml
    volumes:
      # 挂载宿主机的模型文件路径
      - /data/models/DeepSeek-V3-AWQ:/model
      # 挂载 LMCache 配置文件
      - ./lmcache_config.yaml:/vllm-workspace/lmcache_config.yaml
    ipc: host  # 必须，多卡通信需要共享内存
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 8
              capabilities: [gpu]
    # 启动命令：配置 8 卡张量并行，并限制显存
    command: >
      --model /model
      --tensor-parallel-size 8
      --trust-remote-code
      --gpu-memory-utilization 0.90
      --max-model-len 32768
      --port 8000
    depends_on:
      - lmcache-server
    restart: unless-stopped
    networks:
      - llm-network

networks:
  llm-network:
    driver: bridge
2. lmcache_config.yaml (LMCache 策略配置)
在同级目录下创建该文件，用于定义 KV Cache 的换入换出逻辑：

YAML
# LMCache 客户端与服务端交互配置
chunk_size: 256            # 多少个 token 划分为一个缓存块进行匹配
backend: "gpu"             # 基础后端

# 启用远程混合缓存，GPU满了往 CPU 倒腾，CPU 满了去 LMCache Server
local_cpu_percentage: 0.2  # 允许占用容器所在宿主机 20% 的 CPU 内存作为二级缓存
remote_url: "lmcache-server://lmcache-server:65432"
3. 启动服务
在当前目录执行以下命令启动集群：

Bash
docker compose up -d
可以通过 docker compose logs -f vllm-service 查看 vLLM 的 8 卡初始化进度。

三、 客户端使用方案
vLLM 启动成功后，会提供完全兼容 OpenAI API 的接口。因为有了 LMCache 的存在，当多个用户输入包含相同前缀（例如：系统 Prompt、长篇 RAG 上下文、长代码库）的请求时，Time to First Token (TTFT) 会大幅下降。

方案 A：使用 Python (OpenAI SDK) 接入
Python
from openai import OpenAI

client = OpenAI(
    api_key="anything_is_ok",  # vLLM 不需要真正的 key
    base_url="http://localhost:8000/v1"
)

# 模拟长文本 RAG 或系统 Prompt
system_prompt = "你是一个精通 2026 年最新全栈技术的架构师。" + "很长的背景文档..." * 100

# 第一次请求：LMCache 发生 Cache Miss，vLLM 会正常做 Prefill 并把 KV 缓存同步到 LMCache Server
response1 = client.chat.completions.create(
    model="/model",
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "请帮我设计一个高并发的分布式缓存架构。"}
    ]
)
print("第一次回答：", response1.choices[0].message.content)

# 第二次请求（换个问题，但前缀相同）：LMCache 命中！跳过 Prefill 计算，瞬间出字
response2 = client.chat.completions.create(
    model="/model",
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "如果上述架构中 Redis 宕机了怎么办？"}
    ]
)
print("第二次回答（秒开）：", response2.choices[0].message.content)
方案 B：使用 cURL 直接流式调用
你可以直接在终端中测试：

Bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "/model",
    "messages": [
      {"role": "user", "content": "你好，请介绍一下你自己。"}
    ],
    "temperature": 0.7,
    "stream": true
  }'
四、 4090 部署避坑指南 (Production Tips)
P2P 通信限制 (P2P & PCIe)：
RTX 4090 在消費级主板上默认不支持显卡间的 P2P 直接通信（Peer-to-Peer）。当 tensor-parallel-size=8 时，跨卡通信开销极大。

如果启动时报错提示 NCCL 相关错误，请在 vllm-service 的环境配置（environment）中添加一行：
- NCCL_P2P_DISABLE=1

这会强制通过系统内存进行中转通信，虽然速度比 NVLink 慢，但能确保 8 卡 4090 能够稳定跑起来。

OOM 与 Max Model Len 权衡：
DeepSeek 原生支持极长的上下文（如 128K）。但在 4090 环境下，受限于 24GB 显存，建议将 --max-model-len 设为 32768 或 16384。LMCache 会帮你在内存中保留这些长上下文的 KV，但单次请求的极限依然受限于该参数。

数据卷挂载速度：
存放 DeepSeek 模型的 /data/models/DeepSeek-V3-AWQ 路径，请务必存放在 NVMe SSD 上。8 卡并发加载 100G+ 的模型，如果用机械硬盘会卡死数小时。

你在部署具体容量的 DeepSeek 蒸馏版或量化版本时，如果遇到显存溢出（OOM）或 NCCL 通信卡死，随时告诉我报错信息！
