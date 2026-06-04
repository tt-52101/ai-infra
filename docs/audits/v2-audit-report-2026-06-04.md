# vLLM Prefill-Decode 分离部署方案 — 第二轮审核报告 (v2) | 版本：v2.0 | 日期：2026-06-04 | 状态：已发布

## 审核基本信息

| 项目 | 内容 |
|------|------|
| **审核对象** | [docker-compose.yml](file:///d:/ai-infra/compose/docker-compose.yml) v2 + [lmcache_config.yaml](file:///d:/ai-infra/compose/lmcache_config.yaml) + [gateway.py](file:///d:/ai-infra/backend/gateway.py) |
| **部署架构** | Gateway 路由层 → Prefill/Decode 角色分离 + LMCacheMPConnector KV Cache 共享 |
| **硬件环境** | 8 x RTX 4090 GPU (24GB/卡)，无 NVLink |
| **模型规格** | DeepSeek-V4-Flash-AWQ (AWQ 4-bit 量化, MoE 架构) |
| **GPU 分配** | Prefill: GPU 0-3 (TP=4), Decode: GPU 4-7 (TP=4) |
| **上一轮评分** | v1: **5.0 / 10**（2026-06-04 首次评审） |
| **本轮评分** | v2: **6.8 / 10**（+1.8 分提升） |
| **审核方法** | 资深架构师独立评审 + 资深 QA 工程师独立评审（双角色并行） |

---

## 一、v1 → v2 变更摘要与修复追踪

### 1.1 架构变更总览

```
v1 架构 (3 服务):
┌──────────────┐    ┌──────────────┐    ┌──────────────────┐
│ lmcache-server│◄───│ vllm-prefill │    │ vllm-decode       │
│ :65432 暴露   │    │ :8001 暴露    │    │ :8002 暴露        │
└──────────────┘    └──────┬───────┘    └────────┬─────────┘
                            │                     │
                    缺少 PD 参数               缺少 PD 参数
                    无健康检查                无健康检查
                    无 API Key               无 API Key

v2 架构 (4 服务):
┌──────────────┐
│   Gateway     │ :8000 ← 唯一对外暴露
│  (FastAPI)    │
└──────┬───────┘
       │ 认证 + 路由
  ┌────┴────┬────────┐
  ▼         ▼        ▼
┌────────┐ ┌────────┐ ┌──────────────┐
│prefill │ │ decode │ │ lmcache-server│
│:8001   │ │:8002   │ │:5555+:8080    │
│expose  │ │expose  │ │expose         │
│health  │ │health  │ │health         │
│kv_prod │ │kv_cons │ │               │
└────────┘ └────────┘ └──────────────┘
```

### 1.2 v1 问题修复状态追踪

| v1 问题 ID | 问题描述 | v2 状态 | 修复方式 | 备注 |
|-----------|---------|---------|----------|------|
| C-01 | 缺少健康检查 | ✅ 已修复 | 所有服务均添加 healthcheck + condition: service_healthy | 完整覆盖 |
| C-02 | 使用 `image: latest` 标签 | ⚠️ 部分修复 | 改为 `${VAR:-latest}` 参数化，但默认值仍为 latest | 需在 .env 中锁定版本 |
| C-03 | LMCache backend `"gpu"` 与资源不匹配 | ✅ 已修复 | lmcache_config.yaml 改为 `local_cpu: true; max_local_cpu_size: 5` | 配置已重写 |
| C-04 | 缺少 `--enable-prefill-decode-mode` | ✅ 已修复 | 改用 `--kv-transfer-config` + LMCacheMPConnector 方案 | 更先进的实现路径 |
| C-05 | 缺少 `--role prefill/decode` | ✅ 已修复 | kv-transfer-config 中通过 `kv_role: kv_producer/kv_consumer` 指定 | 正确 |
| M-01 | LMCache 端口对外暴露 | ✅ 已修复 | ports → expose，仅内部可访问 | 安全性大幅提升 |
| M-02 | vLLM API 无密钥保护 | ✅ 已修复 | 添加 `--api-key` 参数 + Gateway 双层认证 | 双层防护 |
| M-03 | 缺少 resource limits | ❌ 未修复 | 仍仅有 reservations 无 limits | 建议补充 |
| M-04 | 缺少日志驱动配置 | ✅ 已修复 | YAML anchor `x-default-logging` 统一管理 | 50m*5 轮转 |
| M-05 | 缺少关键 vLLM 调优参数 | ✅ 已修复 | --enable-prefix-caching, --dtype, max-num-seqs 等全部补齐 | Prefill/Decode 差异化调优 |
| M-06 | 显存利用率偏高易 OOM | ✅ 已修复 | 降至 0.80 | 合理 |
| M-07 | local_cpu_percentage 过高 | ✅ 已修复 | 改为 `local_cpu: true; max_local_cpu_size: 5` | 新配置语义 |
| M-08 | 数据持久化路径不规范 | ⚠️ 部分修复 | 参数化为 `${LMCACHE_STORAGE_PATH}` 但默认仍为相对路径 | 建议 .env 中设绝对路径 |

**修复率统计**：12 项中 **9 项完全修复**（75%），**2 项部分修复**（17%），**1 项未修复**（8%）

---

## 二、架构合理性评估（架构师评审）

### 2.1 总体评价：方向正确，Gateway 是关键改进

v2 版本相比 v1 的最大变化是引入了 **Gateway 统一路由层**，这解决了 v1 评审中指出的核心问题——"需要在最前端部署 Nginx 或自定义 Router API 代理"。Gateway 的加入使得：

1. **安全边界收敛**：仅 `:8000` 一个端口对外暴露，所有内部服务通过 `expose` 隔离
2. **PD 流程闭环**：Gateway 实现 Prefill→Decode 两阶段调度，使分离架构真正可用
3. **认证双层化**：外部 Gateway API Key + 内部 vLLM API Key

### 2.2 评分对比

| 维度 | v1 得分 | v2 得分 | 变化 | 主要原因 |
|------|---------|---------|------|----------|
| **架构合理性** | 6.0 | 7.5 | +1.5 | Gateway 补全了 PD 闭环，KV Transfer Config 替代了缺失的 PD 参数 |
| **资源配置** | 7.0 | 7.5 | +0.5 | 参数差异化完善，仍缺 limits |
| **网络通信** | 6.0 | 8.0 | +2.0 | 仅 Gateway 暴露端口，LMCache 完全内网 |
| **高可用性** | 5.0 | 6.5 | +1.5 | 全链路 healthcheck，Gateway 成新 SPOF |
| **性能风险** | 5.0 | 6.5 | +1.5 | 参数调优到位，Gateway 引入新延迟 |
| **安全性** | 3.0 | 7.0 | **+4.0** | 最大单项改进 |
| **运维友好性** | 3.0 | 6.0 | +3.0 | 日志/健康检查/参数化均有实质改善 |
| **综合得分** | **5.0** | **6.8** | **+1.8** | 有实质价值但仍需打磨 |

---

## 三、完整问题清单（v2 新发现问题）

### Critical（必须修复，阻塞上线）

#### ARCH-C01：Gateway PD 调度核心假设未经端到端验证

| 属性 | 内容 |
|------|------|
| **位置** | [gateway.py#L54-L58](file:///d:/ai-infra/backend/gateway.py#L54-L58) (`prefill_payload` 函数) + [docker-compose.yml#L69](file:///d:/ai-infra/compose/docker-compose.yml#L69) (`--kv-transfer-config`) |
| **问题描述** | Gateway 的 PD 分离策略依赖一个关键假设：**Prefill 节点以 `max_tokens=1` 执行请求时，vLLM 会完成全部 prompt 的 KV Cache 计算并通过 LMCacheMPConnector 将 KV 写入 Server，而 Decode 节点后续能从 Server 读取到这些 KV**。这一假设的正确性取决于：(1) LMCacheMPConnector 在 `max_tokens=1` 时是否仍会触发完整的 KV Cache 写入；(2) Decode 节点的 `kv_role: kv_consumer` 是否能正确从 Server 拉取到 Prefill 产生的 KV。这两点**未在代码或文档中得到任何验证证据**。 |
| **影响范围** | 整个 PD 分离方案的核心功能是否生效 |
| **风险等级** | 致命 —— 如果假设不成立，整个 PD 分离退化为"每个请求被发送两次（一次给 Prefill、一次给 Decode）"，不仅没有性能收益，反而增加 ~100% 的计算开销 |
| **验证方法** | 部署后观察：(a) LMCache Server 日志是否有 KV 写入记录；(b) Decode 节点是否有 cache hit 日志；(c) 对比 cold request vs repeat request 的 TTFT 差异；运行 [test_verification.py](file:///d:/ai-infra/backend/tests/test_verification.py) 收集数据 |
| **修复建议** | 在首次 MVP 部署时将此作为**第一优先级验证项**；若假设不成立，需调整 Gateway 调度策略或改用其他 PD 实现方式 |

---

#### GW-C01：AsyncClient 每请求创建销毁导致资源泄漏与性能下降

| 属性 | 内容 |
|------|------|
| **位置** | [gateway.py#L133-L139](file:///d:/ai-infra/backend/gateway.py#L133-L139), [gateway.py#L152-L153](file:///d:/ai-infra/backend/gateway.py#L152-L153), [gateway.py#L179](file:///d:/ai-infra/backend/gateway.py#L179) |
| **问题描述** | Gateway 在每次请求处理过程中创建了 **3 个独立的 httpx.AsyncClient 实例**（run_prefill 1个 + 非流式 decode 1个 + 流式 decode 1个），每个实例都使用 `async with` 上下文管理器即用即销。在高并发场景下：(1) 每次创建 AsyncClient 都会初始化连接池、DNS 解析器等资源，增加 **7-18ms/请求** 的额外开销；(2) 大量短生命周期连接导致 TIME_WAIT 端口堆积，并发 >50 时可能触发 **"Address already in use" 错误**；(3) 无法复用 HTTP keep-alive 连接，丧失连接复用带来的延迟优势 |
| **影响范围** | 高并发下性能严重下降，可能出现端口耗尽导致服务不可用 |
| **风险等级** | 致命 |
| **复现步骤** | 1. 启动 Gateway；2. 使用 Locust 以 100 并发持续发送请求 5 分钟；3. 观察 `netstat -an \| grep TIME_WAIT \| wc -l` 数量持续增长；4. 观察错误日志中出现 "Address already in use" |
| **修复建议** | 使用应用级单例 AsyncClient（带连接池限制），参考以下模式：

```python
import contextlib

# 应用启动时创建一次
_client: httpx.AsyncClient | None = None

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=30.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    yield
    await _client.aclose()

app = FastAPI(lifespan=lifespan)

# 在 handler 中直接使用 _client，不再重复创建
```

---

#### GW-C02：Prefill 失败后仍执行 Decode —— 违反 PD 分离核心契约

| 属性 | 内容 |
|------|------|
| **位置** | [gateway.py#L140-L168](file:///d:/ai-infra/backend/gateway.py#L140-L168) (`dispatch_chat_completions` 函数) |
| **问题描述** | 当 Prefill 阶段失败时（`prefill_error` 非空），Gateway **仅在响应头中设置了 `x-prefill-error` 字段，但仍然继续执行 Decode 阶段**（无论是流式还是非流式）。这意味着：(1) 客户端收到的是 Decode 的结果而非错误信息，但该结果基于不完整/缺失的 KV Cache，输出质量可能受损；(2) 客户端需要自行检查响应头来判断 Prefill 是否成功，违反 OpenAI 兼容协议的常规预期；(3) 浪费了 Decode 节点的计算资源去处理一个已知有问题的请求 |
| **影响范围** | LMCache 故障或 Prefill 节点异常时，返回的结果可能质量受损且难以排查 |
| **风险等级** | 致命 |
| **修复建议** | Prefill 失败时应直接返回错误响应：

```python
if prefill_error:
    return JSONResponse(
        {
            "error": {
                "message": f"Prefill stage failed: {prefill_error}",
                "type": "prefill_error",
                "code": "prefill_stage_failure",
            }
        },
        status_code=502,
        headers=response_headers,
    )
# 仅当 prefill 成功时才继续 decode
if stream:
    return StreamingResponse(...)
# ... 非 stream decode
```

---

#### GW-C03：流式 Decode 完全缺乏异常处理

| 属性 | 内容 |
|------|------|
| **位置** | [gateway.py#L171-L195](file:///d:/ai-infra/backend/gateway.py#L171-L195) (`stream_decode` 异步生成器) |
| **问题描述** | `stream_decode` 异步生成器中的 `client.stream()` 调用和 `aiter_bytes()` 迭代**完全没有 try-except 包裹**。当发生以下任一情况时，异常会直接传播到 FastAPI 框架层，返回 **500 Internal Server Error** 且无有意义的错误信息：(1) Decode 节点不可达（连接拒绝）；(2) Decode 处理中途崩溃（连接重置）；(3) 网络超时；(4) SSE 格式解析错误。客户端收到的只是一个裸 500，无法区分是 Gateway 问题还是上游问题 |
| **影响范围** | 任何 Decode 节点故障在流式模式下都会变成无信息量的 500 错误 |
| **风险等级** | 致命 |
| **修复建议** | 在生成器内部添加异常捕获并转换为 SSE error event：

```python
async def stream_decode(body, headers, request_id):
    started = time.perf_counter()
    chunk_count = 0
    byte_count = 0
    try:
        async with httpx.AsyncClient(timeout=DECODE_TIMEOUT_SECONDS) as client:
            async with client.stream("POST", DECODE_NODE_URL, json=body, headers=headers) as response:
                response.raise_for_status()
                print(f"[{request_id}] decode stream connected status={response.status_code}", flush=True)
                async for chunk in response.aiter_bytes():
                    chunk_count += 1
                    byte_count += len(chunk)
                    yield chunk
    except httpx.HTTPStatusError as exc:
        yield f"data: {json.dumps({'error': {'message': f'Decode node returned {exc.response.status_code}', 'code': 'upstream_error'}})}\n\n".encode()
    except httpx.ConnectError as exc:
        yield f"data: {json.dumps({'error': {'message': 'Decode node unreachable', 'code': 'decode_unreachable'}})}\n\n".encode()
    except Exception as exc:
        yield f"data: {json.dumps({'error': {'message': str(exc)[:200], 'code': 'internal_error'}})}\n\n".encode()
    finally:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        print(f"[{request_id}] decode stream done chunks={chunk_count} bytes={byte_count} elapsed_ms={elapsed_ms}", flush=True)
```

---

### Major（强烈建议修复）

#### GW-M01：缺少请求体大小限制

| 属性 | 内容 |
|------|------|
| **位置** | [gateway.py#L119](file:///d:/ai-infra/backend/gateway.py#L119) (`body = await request.json()`) |
| **问题描述** | Gateway 直接调用 `request.json()` 解析请求体，未设置任何大小限制。恶意用户可发送超大型 JSON（如数百 MB 的 messages 数组），导致：(1) Gateway 内存被耗尽；(2) 上游 Prefill/Decode 节点接收超大 payload 后可能 OOM |
| **风险等级** | Major — 可被利用进行 DoS 攻击 |
| **修复建议** | 在 FastAPI app 层面添加中间件限制请求体大小：

```python
from fastapi.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware

class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > 10 * 1024 * 1024:  # 10MB
            return JSONResponse({"error": "Request body too large"}, status_code=413)
        return await call_next(request)

app = FastAPI(title="...", middleware=[Middleware(RequestSizeLimitMiddleware)])
```

---

#### GW-M02：非流式模式下 AsyncClient 重复创建

| 属性 | 内容 |
|------|------|
| **位置** | [gateway.py#L152-L153](file:///d:/ai-infra/backend/gateway.py#L152-L153) |
| **问题描述** | 在非流式路径中，Prefill 已经使用了一个 AsyncClient 并关闭后，Decode 又创建了第二个 AsyncClient。这两个客户端无法共享连接池，且 Prefill 的客户端在 `async with` 退出时立即关闭，其 TCP 连接进入 TIME_WAIT 状态。这是 GW-C01 的子问题 |
| **风险等级** | Major（合并至 GW-C01 一起修复） |

---

#### GW-M03：缺少速率限制

| 属性 | 内容 |
|------|------|
| **位置** | [gateway.py](file:///d:/ai-infra/backend/gateway.py) 全局 |
| **问题描述** | Gateway 无任何速率限制机制。任何持有有效 API Key 的客户端都可以无限速地发送请求，可能导致：(1) 上游 vLLM 节点过载；(2) GPU 显存 OOM；(3) LMCache Server 被打满 |
| **风险等级** | Major |
| **修复建议** | 引入 `slowapi` 或 `litestar` 的速率限制中间件：

```python
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.post("/v1/chat/completions")
@limiter.limit("60/minute")  # 每分钟最多 60 次
async def dispatch_chat_completions(request: Request, ...):
    ...
```

---

#### GW-M04：日志仅使用 print，无结构化输出

| 属性 | 内容 |
|------|------|
| **位置** | [gateway.py](file:///d:/ai-infa/backend/gateway.py) 全局多处 print 调用 |
| **问题描述** | 所有日志输出均使用 `print()` 函数，存在以下问题：(1) 无日志级别区分（info/warn/error）；(2) 无时间戳（依赖 Docker logging driver 添加）；(3) 无法与集中式日志系统（ELK/Loki）集成；(4) 生产环境下 print 输出可能与 stdout 其他内容混在一起难以过滤 |
| **风险等级** | Major — 影响问题排查效率 |
| **修复建议** | 使用 Python standard `logging` 模块：

```python
import logging

logger = logging.getLogger("gateway")
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
)

# 替换所有 print(...) 为 logger.info(...)/logger.error(...)
```

---

#### CFG-M01：缺少 resource limits（v1 遗留）

| 属性 | 内容 |
|------|------|
| **位置** | [docker-compose.yml#L50-L56](file:///d:/ai-infra/compose/docker-compose.yml#L50-L56) (Prefill), [docker-compose.yml#L103-L109](file:///d:/ai-infra/compose/docker-compose.yml#L103-L109) (Decode) |
| **问题描述** | 所有服务的 deploy.resources 仅设置了 reservations（资源预留），未设置 limits（资源上限）。单个容器出现内存泄漏或异常行为时可能拖垮整台宿主机 |
| **风险等级** | Major |
| **修复建议** |

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

#### SEC-M01：默认 API Key 存在生产部署误用风险

| 属性 | 内容 |
|------|------|
| **位置** | [.env.example#L9-L10](file:///d:/ai-infra/compose/.env.example#L9-L10) + [docker-compose.yml#L68](file:///d:/ai-infra/compose/docker-compose.yml#L68), [docker-compose.yml#L122](file:///d:/ai-infra/compose/docker-compose.yml#L122), [docker-compose.yml#L154-L155](file:///d:/ai-infra/compose/docker-compose.yml#L154-L155) |
| **问题描述** | VLLM_API_KEY 和 GATEWAY_API_KEY 的默认值均为 `sk-mvp-change-me`。如果部署时忘记修改 `.env` 文件中的值，系统虽然启用了认证，但使用的是众所周知的弱密钥，等同于无保护 |
| **风险等级** | Major |
| **修复建议** | 方案 A：移除默认值，启动时检测环境变量若为空或等于占位符则拒绝启动；方案 B：在 Gateway 启动时校验，若 API_KEY 为 `sk-mvp-change-me` 则打印 WARNING 日志 |

---

### Minor（建议优化）

#### GW-m01：缺少 `/v1/models` 端点代理

| 属性 | 内容 |
|------|------|
| **位置** | [gateway.py](file:///d:/ai-infra/backend/gateway.py) |
| **问题描述** | Gateway 仅代理了 `/v1/chat/completions` 和 `/healthz`，未代理 `/v1/models` 等其他 OpenAI 兼容端点。使用 OpenAI SDK 或兼容客户端时可能因缺少 models 接口而报错 |
| **风险等级** | Minor |
| **修复建议** | 添加通用反向代理路由或至少代理 `/v1/models` 到 Prefill 节点 |

---

#### GW-m02：缺少 CORS 配置

| 属性 | 内容 |
|------|------|
| **位置** | [gateway.py#L20](file:///d:/ai-infra/backend/gateway.py#L20) (FastAPI app 创建) |
| **问题描述** | 未配置 CORS 中间件。浏览器端直接调用时会遇到跨域问题 |
| **风险等级** | Minor（若仅服务端调用则不影响） |

---

#### CFG-m01：Compose version 字段已废弃

| 属性 | 内容 |
|------|------|
| **位置** | [docker-compose.yml#L1](file:///d:/ai-infra/compose/docker-compose.yml#L1) |
| **问题描述** | `version: '3.8'` 在 Docker Compose V2 中已被忽略，保留不会报错但属于过时写法 |
| **风险等级** | Info |

---

#### CFG-m02：LMCache storage 路径默认为相对路径

| 属性 | 内容 |
|------|------|
| **位置** | [docker-compose.yml#L21](file:///d:/ai-infra/compose/docker-compose.yml#L21) |
| **问题描述** | `LMCACHE_STORAGE_PATH` 默认值为 `./lmcache_storage`，不同工作目录下 `docker compose up` 行为不一致 |
| **风险等级** | Minor — 建议在 .env.example 中改为绝对路径示例 |

---

#### LMC-m01：lmcache_config.yaml 缺少 remote_url 配置

| 属性 | 内容 |
|------|------|
| **位置** | [lmcache_config.yaml](file:///d:/ai-infra/compose/lmcache_config.yaml) |
| **问题描述** | 当前 config 仅包含 `chunk_size`, `local_cpu`, `max_local_cpu_size` 三个字段，缺少 `remote_url` 或 `remote_serde` 配置。而 docker-compose.yml 中的 `--kv-transfer-config` 已显式指定了 LMCacheMPConnector 和连接参数。需要确认 LMCacheMPConnector 是否完全忽略 lmcache_config.yaml 中的 remote 配置而仅依赖 kv-transfer-config，还是两者需要一致 |
| **风险等级** | Minor — 需要文档确认或实测验证 |

---

### Info（信息性建议）

| ID | 建议 |
|----|------|
| I-01 | 建议为 Gateway 添加 Prometheus metrics 端点（如 `/metrics`），便于监控请求数、延迟分布、错误率 |
| I-02 | 建议添加 `x-vllm-common` YAML anchor 提取 Prefill/Decode 公共配置，减少重复（当前两个 service 定义仍有较多重复） |
| I-03 | 建议为 Gateway Dockerfile 添加非 root 用户运行（security best practice） |
| I-04 | `.env.example` 中建议注释说明 `PREFILL_MAX_TOKENS=1` 的设计意图（这是 PD 分离的关键参数） |

---

## 四、SPOF 分析（更新版）

```
                        ┌──────────────────────────────────┐
                        │           外部客户端              │
                        └─────────────┬────────────────────┘
                                      │ :8000
                                      ▼
                        ┌──────────────────────────────────┐
                        │     ⚠️ Gateway (新 SPOF)          │
                        │     deepseek-pd-gateway           │
                        │     单实例 / 无热备                │
                        └──────┬───────────┬───────────────┘
                               │           │
                    prefill (KV生产)   decode (KV消费)
                               │           │
                    ┌──────────▼──┐  ┌────▼──────────┐
                    │ vllm-prefill │  │ vllm-decode    │
                    │ GPU 0-3      │  │ GPU 4-7        │
                    └──────┬───────┘  └────┬───────────┘
                           │               │
                           └───────┬───────┘
                                   ▼
                       ┌───────────────────────┐
                       │ ⚠️ lmcache-server      │
                       │ (SPOF - 单实例无HA)     │
                       │ port 5555/8080 internal│
                       └───────────────────────┘
```

| SPOF 点 | 风险等级 | 影响 | v1 状态 | v2 变化 | 建议 |
|---------|----------|------|---------|---------|------|
| **Gateway** | **Major (新增)** | 整个集群对外的唯一入口，宕机则全部不可用 | 不存在 | **新增 SPOF** | MVP 阶段可接受；生产化前加 Keepalived/VIP + 双实例 |
| **LMCache Server** | Critical | KV Cache 失效，PD 分离退化为基础模式 | Critical | 无变化（仍为单实例） | 同 v1 建议 |
| **物理主机** | Critical | 整机不可用 | Critical | 无变化 | 多节点 K8s |

---

## 五、故障传播路径分析

### 5.1 正常请求流程

```
Client → Gateway(:8000) → Prefill(:8001, max_tokens=1, kv_producer)
                              │
                         KV写入LMCache(:5555)
                              │
                         Gateway 收到 prefill ok
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
             stream=true          stream=false
                    │                   │
              Decode(:8002)       Decode(:8002)
              (kv_consumer,流式)   (kv_consumer)
                    │                   │
                    ▼                   ▼
              SSE stream 返回       JSON 返回 Client
```

### 5.2 故障场景矩阵

| 故障点 | 故障类型 | Gateway 行为 | 客户端收到 | 当前处理质量 | 改进需求 |
|--------|----------|-------------|-----------|-------------|---------|
| LMCache Server | 崩溃 | Prefill 失败 → **仍执行 Decode** (GW-C02) | Decode 结果(可能受损) or 500 | ❌ 差 | 应返回 502 |
| LMCache Server | 网络分区 | 同上 | 同上 | ❌ 差 | 应返回 502 |
| Prefill 节点 | 崩溃 | Prefill 失败 → **仍执行 Decode** (GW-C02) | 500 (httpx ConnectError) | ❌ 差 | 应返回 502 |
| Prefill 节点 | 超时 | httpx TimeoutException | 500 | ❌ 一般 | 应返回 504 |
| Decode 节点 | 崩溃 (非流式) | httpx ConnectError → 500 | 500 无详细信息 | ❌ 差 | 应返回 502 + 上游错误码 |
| Decode 节点 | 崩溃 (流式) | **无异常处理** (GW-C03) | 500 裸错误 | ❌ 最差 | 应返回 SSE error event |
| Decode 节点 | 超时 (流式) | **无异常处理** (GW-C03) | 500 裸错误 | ❌ 最差 | 应返回 SSE error event |
| Gateway 本身 | OOM | 容器重启 | 连接断开 | ⚠️ 取决于 restart 策略 | 建议加 memory limit |
| Gateway 本身 | 健康检查失败 | Docker 重启容器 | 短暂不可用 (~20-30s) | ✅ 可接受 | — |

---

## 六、安全性评估（更新版）

### 6.1 安全改进确认

| 安全项 | v1 状态 | v2 状态 | 改进方式 |
|--------|---------|---------|----------|
| LMCache 端口暴露 | 对外 65432 | **仅内部 expose** | ports → expose ✅ |
| vLLM API 暴露 | 对外 8001/8002 | **仅内部 expose** | ports → expose ✅ |
| API 认证 | 无 | **双层认证** | Gateway Key + vLLM Key ✅ |
| 模型卷权限 | 读写 | **只读 (:ro)** | volume :ro ✅ |
| 日志安全 | 无限制 | **大小限制** | json-file 50m*5 ✅ |
| 网络隔离 | 无 | **bridge 内部网络** | ds-pd-network ✅ |
| 默认密钥 | N/A | **sk-mvp-change-me** | ⚠️ 弱默认值 |

### 6.2 OWASP Top 10 对照（更新）

| OWASP 类别 | v1 覆盖 | v2 覆盖 | 变化 |
|------------|---------|---------|------|
| A01: 访问控制失效 | ❌ | ✅ 基本覆盖 | 双层 API Key |
| A02: 加密机制失败 | ❌ | ⚠️ 部分 | 内部服务隔离，但无 TLS |
| A05: 安全配置错误 | ⚠️ | ⚠️ 改善 | latest tag 仍在，但已参数化 |
| A06: 过时组件 | ❌ | ⚠️ 部分 | 依赖 .env 锁定版本 |
| A07: 认识和记录失败 | ❌ | ⚠️ 改善 | 有日志轮转，但 gateway 用 print |

**安全评分**：v1 为 **3.0/10** → v2 为 **7.0/10**

---

## 七、Gateway 代码深度审查

### 7.1 架构设计评估

**优点**：
- PD 两阶段调度逻辑清晰：先 Prefill（max_tokens=1 做 KV 生产）再 Decode（KV 消费）
- 双层认证设计合理：外部 Gateway Key + 内部 Upstream Key
- 支持 streaming 和 non-streaming 两种模式
- 响应头传递调试信息（x-request-id, x-prefill-status, x-prefill-ms）
- 健康检查端点暴露关键配置状态

**核心风险**：
- PD 调度的正确性完全依赖于 `max_tokens=1` 能触发完整 KV Cache 写入的假设（ARCH-C01）
- 错误处理路径不完整（GW-C02, GW-C03）
- AsyncClient 生命周期管理不当（GW-C01）

### 7.2 代码质量问题汇总

| 类别 | 数量 | 详情 |
|------|------|------|
| 逻辑错误 | 2 | GW-C02 (Prefill 失败仍 Decode), GW-C03 (流式无异常处理) |
| 资源管理 | 1 | GW-C01 (AsyncClient 泄漏) |
| 安全缺陷 | 1 | GW-M01 (无请求体大小限制) |
| 可靠性缺陷 | 1 | GW-M03 (无速率限制) |
| 工程规范 | 1 | GW-M04 (print 替代 logging) |
| 功能完整性 | 1 | GW-m01 (缺 /v1/models) |
| **合计** | **8** | 其中 Critical 3, Major 4, Minor 1 |

---

## 八、LMCache 配置一致性分析

### 8.1 配置文件 vs kv-transfer-config 对照

| 配置项 | lmcache_config.yaml | kv-transfer-config (docker-compose.yml) | 一致性 |
|--------|---------------------|----------------------------------------|--------|
| 连接地址 | **未指定 remote_url** | `lmcache.mp.host: tcp://lmcache-server` | ⚠️ 需确认 |
| 连接端口 | **未指定** | `lmcache.mp.port: 5555` | ⚠️ 需确认 |
| Chunk 大小 | `chunk_size: 256` | 未指定（使用默认） | ✅ 可能 OK |
| 本地缓存 | `local_cpu: true; max_local_cpu_size: 5` | 未涉及 | ✅ 独立维度 |
| 角色 | 未指定 | `kv_role: kv_producer / kv_consumer` | ✅ 通过 command-line 指定 |
| Connector 类型 | 未指定 | `LMCacheMPConnector` | ✅ 通过 command-line 指定 |

**关键疑问**：LMCacheMPConnector 是否读取 `lmcache_config.yaml`？如果不读取，则当前 yaml 中的配置（特别是 `local_cpu` 和 `chunk_size`）可能不会被 MP Connector 使用。建议通过实测或查阅 LMCache 文档确认。

### 8.2 LMCache Server 端口变更分析

| 项目 | v1 | v2 | 说明 |
|------|----|----|------|
| 协议端口 | 65432 (单一) | **5555 (MP) + 8080 (HTTP)** | v2 拆分为两个端口 |
| 暴露方式 | ports (对外) | **expose (仅内部)** | 安全性提升 |
| 健康检查 | 无 | **socket 探测 5555** | 新增 |

---

## 九、性能风险评估（更新版）

### 9.1 Gateway 引入的额外延迟

```
延迟叠加分析（单次请求）:

Client → Gateway:           ~0.1ms (本地网络)
Gateway → Prefill:          ~0.5ms (bridge 网络)
Prefill 计算 (max_tokens=1): ~200-2000ms (取决于 prompt 长度)
  └─ KV 写入 LMCache:       ~5-20ms
Gateway ← Prefill:          ~0.5ms
Gateway → Decode:           ~0.5ms
Decode 读 KV + 生成:        ~30-200ms/token
Gateway ← Decode → Client:  ~0.6ms

额外开销（vs 直连 vLLM）:
├─ Gateway 代理转发:        ~1-2ms (固定开销)
├─ 两阶段串行执行:          Prefill TTFT 完整计入 (但这是 PD 分离的设计代价)
├─ AsyncClient 创建开销:    ~7-18ms/请求 (GW-C01, 修复后趋近于 0)
└─ 总计额外延迟:            ~8-20ms/请求 (修复 GW-C01 后降至 ~1-3ms)
```

### 9.2 资源竞争分析

| 资源 | v1 风险 | v2 风险 | 变化 |
|------|---------|---------|------|
| CPU | 无限制 | Gateway 增加 Python 进程 CPU 开销 | 略微恶化 |
| 内存 | 无 limit | Gateway 增加 httpx/FastAPI 内存占用 | 略微恶化 |
| FD 句柄 | 低风险 | **AsyncClient 频繁创建导致 TIME_WAIT 堆积** (GW-C01) | 明显恶化（修复前） |
| 网络连接 | LMCache 单通道 | Gateway 增加 2 条到 upstream 的连接 | 可控 |

---

## 十、测试建议

### 10.1 冒烟测试清单（v2 更新版）

| # | 测试项 | 测试命令/方法 | 预期结果 | 优先级 |
|---|--------|---------------|----------|--------|
| SM-01 | 4 容器全部 running | `docker compose ps` | 4 个服务均为 healthy/running | P0 |
| SM-02 | Gateway 健康检查 | `curl -sf http://localhost:8000/healthz` | 200 + JSON 含 status=ok | P0 |
| SM-03 | Prefill 健康检查 | `docker exec` 检查 health | healthy | P0 |
| SM-04 | Decode 健康检查 | `docker exec` 检查 health | healthy | P0 |
| SM-05 | LMCache 健康检查 | `docker exec` 检查 health | healthy | P0 |
| SM-06 | 无 Key 认证拒绝 | POST without Authorization | 401 | P0 |
| SM-07 | 错误 Key 认证拒绝 | POST with wrong Bearer token | 401 | P0 |
| SM-08 | 正确 Key 非流式推理 | POST with valid key, stream=false | 200 + JSON response | P0 |
| SM-09 | 正确 Key 流式推理 | POST with valid key, stream=true | 200 + SSE stream | P0 |
| SM-10 | LMCache 端口不对外 | `curl -sf http://localhost:5555` | Connection refused | P0 |
| SM-11 | vLLM 端口不对外 | `curl -sf http://localhost:8001` | Connection refused | P0 |
| SM-12 | GPU 分配正确 | `nvidia-smi` in containers | 各自看到对应 GPU | P0 |
| SM-13 | **PD 分离有效性** | 观察 LMCache Server 日志 + Decode cache hit | KV 写入/读取正常 | P0 (**核心验证**) |
| SM-14 | Prefill 失败时不执行 Decode | kill prefill 后发请求 | 502 而非 Decode 结果 | P1 (修复 GW-C02 后) |
| SM-15 | 流式 Decode 断连处理 | kill decode 中途观察 | SSE error event 而非 500 | P1 (修复 GW-C03 后) |

### 10.2 Gateway 专项测试用例

| # | 场景 | 输入 | 预期输出 | 优先级 |
|---|------|------|----------|--------|
| GW-T01 | 正常非流式请求 | valid body, stream=false | 200 + choices array | P0 |
| GW-T02 | 正常流式请求 | valid body, stream=true | 200 + SSE data events | P0 |
| GW-T03 | 空 body | `{}` | 400 error | P0 |
| GW-T04 | 非 JSON body | raw text | 400 error | P0 |
| GW-T05 | 超大 body (>10MB) | large messages array | 413 (修复 GW-M01 后) | P1 |
| GW-T06 | 无 authorization header | missing header | 401 | P0 |
| GW-T07 | 错误 API Key | wrong bearer token | 401 | P0 |
| GW-T08 | Prefill 节点 down | (kill prefill) | 502 (修复 GW-C02 后) | P0 |
| GW-T09 | Decode 节点 down (非流式) | (kill decode) | 502 | P0 |
| GW-T10 | Decode 节点 down (流式) | (kill decode during stream) | SSE error event (修复 GW-C03 后) | P0 |
| GW-T11 | Prefill 超时 | 超长 prompt | 504 | P1 |
| GW-T12 | Decode 超时 | 超长 generation | 504 / SSE error | P1 |
| GW-T13 | 高并发 50 qps | Locust 50 并发 | 错误率 < 1%, 无端口耗尽 | P1 (修复 GW-C01 后) |
| GW-T14 | 高并发 100 qps | Locust 100 并发 | 错误率 < 1%, P99 延迟合理 | P1 |
| GW-T15 | healthz 端点 | GET /healthz | 200 + 配置状态 | P0 |

### 10.3 故障注入测试矩阵

| 注入对象 | 注入操作 | 验证点 | 预期行为 |
|----------|----------|--------|----------|
| lmcache-server | `docker kill` | Gateway 请求处理 | Prefill 失败 → 返回 502（修复后）；不崩溃 |
| lmcache-server | `docker start` (恢复) | 自动重连 | 30s 内恢复 KV Cache 功能 |
| vllm-prefill | `docker stop` | Gateway 请求处理 | 返回 502（修复后） |
| vllm-prefill | `docker start` (恢复) | 健康检查恢复 | Gateway depends_on 自动感知 |
| vllm-decode | `docker stop` (流式中) | Gateway 流式响应 | SSE error event（修复后） |
| gateway | `docker kill` | 外部访问 | 全部不可用，~30s 后自动重启 |
| 网络 (gateway→prefill) | iptables DROP | Gateway 日志 | Prefill 超时 → 504 |
| 磁盘满 (lmcache_storage) | dd fill | LMCache Server | 优雅降级或明确错误日志 |

---

## 十一、修复路线图

### Phase 0 — 紧急修复（预计 2-3 小时，阻塞首次部署）

| 步骤 | 操作 | 对应问题 | 预计耗时 |
|------|------|----------|----------|
| F0-01 | 修复 GW-C02：Prefill 失败时返回 502 而非继续 Decode | GW-C02 | 15 min |
| F0-02 | 修复 GW-C03：流式 Decode 添加完整异常处理 | GW-C03 | 30 min |
| F0-03 | 修复 GW-C01：AsyncClient 改为应用级单例 | GW-C01 | 30 min |
| F0-04 | 添加 CFG-M01：resource limits | CFG-M01 | 15 min |
| F0-05 | 修复 SEC-M01：启动时检测弱默认 API Key | SEC-M01 | 15 min |
| F0-06 | 冒烟测试 SM-01 ~ SM-15 全部通过 | 全部 | 60 min |

**Phase 0 出口标准**：5 个 Critical + 1 个 Major 修复完毕，15 项冒烟测试通过。

### Phase 1 — 重要加固（部署后 1 周内）

| 步骤 | 操作 | 对应问题 | 预计耗时 |
|------|------|----------|----------|
| F1-01 | 添加请求体大小限制中间件 | GW-M01 | 20 min |
| F1-02 | 添加速率限制 | GW-M03 | 20 min |
| F1-03 | print 替换为 logging 模块 | GW-M04 | 15 min |
| F1-04 | 添加 /v1/models 代理 | GW-m01 | 10 min |
| F1-05 | 镜像版本锁定（.env 中去除 latest） | v1-C02 遗留 | 10 min |
| F1-06 | 压测验证（50/100 QPS） | 性能基线 | 60 min |

### Phase 2 — 验证与优化（持续）

| 步骤 | 操作 | 说明 |
|------|------|------|
| F2-01 | ARCH-C01 核心假设验证 | 部署后重点观察 LMCache 日志 + 运行 test_verification.py |
| F2-02 | Gateway 热备方案设计 | 生产化前的 HA 方案 |
| F2-03 | LMCache Server HA | 主备或 Redis/TiKV 替代 |
| F2-04 | Prometheus 监控接入 | Gateway + vLLM metrics 汇聚 |

---

## 十二、总结

### 12.1 v1 → v2 改进确认

v2 是一次**高质量的迭代**，解决了 v1 中大部分阻塞性问题：

| 改进领域 | v1 状态 | v2 状态 | 评价 |
|----------|---------|---------|------|
| 安全隔离 | 内部服务端口全部对外暴露 | **仅 Gateway :8000 对外** | 决定性改进 |
| PD 分离实现 | 缺少关键参数，实际不工作 | **LMCacheMPConnector + Gateway 调度** | 架构闭环 |
| 健康检查 | 零覆盖 | **全链路 coverage + condition: service_healthy** | 完整覆盖 |
| 认证机制 | 无 | **双层 API Key** | 达标 |
| 日志管理 | 无 | **YAML anchor 统一 50m*5 轮转** | 达标 |
| 参数调优 | 缺失 7+ 关键参数 | **全部补齐 + Prefill/Decode 差异化** | 超出预期 |

### 12.2 新引入的风险

v2 的主要风险集中在**新增的 Gateway 代码**上：

- 3 个 Critical 级别的代码逻辑问题（错误处理、资源管理）
- Gateway 成为新的 SPOF
- PD 调度核心假设待验证

### 12.3 最终判定

| 维度 | 评级 |
|------|------|
| **能否部署到目标机器做首次验证？** | 可以，但建议先修完 Phase 0 的 5 个 Critical（约 2-3 小时工作量） |
| **能否上线生产环境？** | **不能** —— 需完成 Phase 0 + Phase 1 + ARCH-C01 验证 |
| **整体成熟度** | **MVP 阶段** —— 核心架构正确，工程细节需打磨 |

---

## 十三、审核签名

| 角色 | 评审人 | 日期 | 结论 |
|------|--------|------|------|
| 资深架构师 | AI Agent (senior-fullstack-architect) | 2026-06-04 | 6.8/10，架构方向正确，Gateway 代码需紧急修复 3 个 Critical |
| 资深 QA 工程师 | AI Agent (senior-qa-engineer) | 2026-06-04 | 发现 19 个问题（3 Critical / 7 Major / 5 Minor / 4 Info），不建议直接发布 |
| 业务确认 | ________________ | ____-__-__ | |
| 技术负责人审批 | ________________ | ____-__-__ | |

---

## 附录 A：问题索引速查表

| ID | 级别 | 模块 | 一句话描述 | 修复复杂度 |
|----|------|------|-----------|-----------|
| ARCH-C01 | Critical | 架构 | PD 调度核心假设（max_tokens=1 触发 KV 写入）待验证 | 验证类 |
| GW-C01 | Critical | Gateway | AsyncClient 每请求创建销毁，资源泄漏 | 30min |
| GW-C02 | Critical | Gateway | Prefill 失败后仍执行 Decode | 15min |
| GW-C03 | Critical | Gateway | 流式 Decode 无异常处理 | 30min |
| GW-M01 | Major | Gateway | 无请求体大小限制 | 20min |
| GW-M02 | Major | Gateway | 非流式 AsyncClient 重复创建 | 合并 GW-C01 |
| GW-M03 | Major | Gateway | 无速率限制 | 20min |
| GW-M04 | Major | Gateway | print 替代 logging | 15min |
| CFG-M01 | Major | Compose | 缺少 resource limits | 15min |
| SEC-M01 | Major | 安全 | 默认 API Key 弱值 | 15min |
| GW-m01 | Minor | Gateway | 缺 /v1/models 代理 | 10min |
| GW-m02 | Minor | Gateway | 无 CORS 配置 | 5min |
| CFG-m01 | Minor | Compose | version 字段废弃 | 0min |
| CFG-m02 | Minor | Compose | LMCache 路径默认相对路径 | 5min |
| LMC-m01 | Minor | LMCache | config.yaml 缺 remote_url | 验证类 |
| I-01 | Info | 增强 | Gateway 加 Prometheus metrics | 2h |
| I-02 | Info | 增强 | YAML anchor 提取公共配置 | 15min |
| I-03 | Info | 安全 | Dockerfile 非 root 用户 | 10min |
| I-04 | Info | 文档 | .env.example 注释设计意图 | 5min |

---

*本报告基于双角色 Agent 独立评审生成，所有发现均附带具体位置引用、影响分析和可操作的修复建议。*
*上一轮报告位于 [compose/AUDIT_REPORT.md](file:///d:/ai-infra/compose/AUDIT_REPORT.md)。*
