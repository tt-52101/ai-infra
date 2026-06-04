# Gateway Phase 0 紧急修复操作清单 | 版本：v1.0 | 日期：2026-06-04 | 状态：待执行

> 本文档针对 [v2 审核报告](file:///d:/ai-infra/docs/audits/v2-audit-report-2026-06-04.md) 中发现的 **3 个 Critical + 2 个 Major** 问题，提供逐行级别的代码修改步骤。
>
> **目标文件**：[gateway.py](file:///d:/ai-infra/backend/gateway.py)
>
> **预计总耗时**：**90-120 分钟**（含验证）

---

## 执行前准备

| # | 准备项 | 操作命令 | 状态 |
|---|--------|----------|------|
| PRE-01 | 备份当前 gateway.py | `copy backend\gateway.py backend\gateway.py.bak` | ☐ |
| PRE-02 | 确认 Python 环境 | `python --version` (需 >=3.11) | ☐ |
| PRE-03 | 安装依赖 | `pip install -r backend\requirements.txt` | ☐ |

---

## F0-01：修复 GW-C02 — Prefill 失败时返回 502 而非继续 Decode

**问题编号**：GW-C02 (Critical)
**问题位置**：[gateway.py#L112-L168](file:///d:/ai-infra/backend/gateway.py#L112-L168) — `dispatch_chat_completions` 函数
**预期耗时**：15 分钟
**风险等级**：修改核心调度逻辑，需重点验证

### 问题描述

当 Prefill 阶段失败时，Gateway 仅在响应头设置 `x-prefill-error` 但仍然继续执行 Decode。这违反 PD 分离契约——没有有效 KV Cache 的 Decode 结果质量受损，且浪费计算资源。

### 修改步骤

#### Step 1：定位修改区域

打开 [gateway.py](file:///d:/ai-infra/backend/gateway.py)，找到第 **141-150 行**：

```python
    # === 当前代码（修改前）===
    response_headers = pd_headers(prefill_status, prefill_ms, request_id)
    if prefill_error:                              # ← L142: 仅设置 header
        response_headers["x-prefill-error"] = prefill_error

    if stream:                                     # ← L145: 无论 prefill 是否成功都继续
        return StreamingResponse(
            stream_decode(body, headers, request_id),
            media_type="text/event-stream",
            headers=response_headers,
        )

    async with httpx.AsyncClient(timeout=DECODE_TIMEOUT_SECONDS) as client:  # ← L152
        decode_response = await client.post(DECODE_NODE_URL, json=body, headers=headers)
```

#### Step 2：替换为以下代码

将 **L141 到 L152** 的整个区块替换为：

```python
    # === 修改后代码 ===
    response_headers = pd_headers(prefill_status, prefill_ms, request_id)

    # GW-C02 FIX: Prefill 失败时立即返回 502，不再执行 Decode
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

    # Prefill 成功后，根据 stream 参数路由到 Decode
    if stream:
        return StreamingResponse(
            stream_decode(body, headers, request_id),
            media_type="text/event-stream",
            headers=response_headers,
        )

    async with httpx.AsyncClient(timeout=DECODE_TIMEOUT_SECONDS) as client:
        decode_response = await client.post(DECODE_NODE_URL, json=body, headers=headers)
```

### 关键变更点说明

| 变更 | 原行为 | 新行为 |
|------|--------|--------|
| Prefill 失败时的处理 | 设置 header 后继续执行 Decode | **立即返回 502 JSON 错误响应** |
| HTTP 状态码 | 200 (Decode 的结果) + 隐藏的 error header | **502 Bad Gateway** |
| 响应体 | 正常的 chat completion 结果 | **结构化错误信息** `{error: {message, type, code}}` |
| 资源消耗 | 浪费一次 Decode 计算 | **不执行 Decode** |

### 验证方法

```bash
# 1. 启动 Gateway（确保 vLLM 服务可用）
cd d:\ai-infra\compose
docker compose up -d gateway

# 2. 模拟 Prefill 失败场景：临时停止 prefill 节点
docker stop vllm-prefill-cluster

# 3. 发送请求（预期返回 502，而非等待超时或返回 Decode 结果）
curl -s -w "\nHTTP_CODE:%{http_code}" \
  -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-mvp-change-me" \
  -d '{"model":"/model","messages":[{"role":"user","content":"hello"}],"max_tokens":10}'

# 预期输出：
# {"error":{"message":"Prefill stage failed: ...","type":"prefill_error","code":"prefill_stage_failure"}}
# HTTP_CODE:502

# 4. 恢复 prefill 节点
docker start vllm-prefill-cluster
```

**验收标准**：HTTP 状态码为 **502**，响应体包含 `type: "prefill_error"` 字段。

---

## F0-02：修复 GW-C03 — 流式 Decode 添加完整异常处理

**问题编号**：GW-C03 (Critical)
**问题位置**：[gateway.py#L171-L195](file:///d:/ai-infra/backend/gateway.py#L171-L195) — `stream_decode` 异步生成器
**预期耗时**：30 分钟
**风险等级**：修改流式生成器的异常行为，需测试多种故障模式

### 问题描述

`stream_decode` 生成器中的网络调用完全没有 try-except。任何上游故障（连接拒绝、超时、重置）都会传播为裸 500 Internal Server Error。

### 修改步骤

#### Step 1：定位修改区域

找到 **L171-L195 行** 的 `stream_decode` 函数全部内容。

#### Step 2：替换整个函数

将 `stream_decode` 函数从 `async def stream_decode(` 开始到函数结束，整体替换为：

```python
async def stream_decode(
    body: dict[str, Any],
    headers: dict[str, str],
    request_id: str,
):
    """GW-C03 FIX: 流式 Decode 添加完整的异常处理和 SSE error event 输出。"""
    started = time.perf_counter()
    chunk_count = 0
    byte_count = 0

    def _sse_error(error_message: str, error_code: str) -> bytes:
        """构造符合 OpenAI SSE 规范的 error event。"""
        error_payload = json.dumps({
            "error": {
                "message": error_message,
                "type": "server_error",
                "code": error_code,
            }
        }, ensure_ascii=False)
        return f"data: {error_payload}\n\n".encode("utf-8")

    try:
        async with httpx.AsyncClient(timeout=DECODE_TIMEOUT_SECONDS) as client:
            try:
                async with client.stream(
                    "POST", DECODE_NODE_URL, json=body, headers=headers
                ) as response:
                    response.raise_for_status()
                    print(
                        f"[{request_id}] decode stream connected "
                        f"status={response.status_code}",
                        flush=True,
                    )
                    async for chunk in response.aiter_bytes():
                        chunk_count += 1
                        byte_count += len(chunk)
                        yield chunk

            except httpx.HTTPStatusError as exc:
                # 上游返回了 4xx/5xx 状态码
                status = exc.response.status_code if exc.response is not None else 502
                print(
                    f"[{request_id}] decode upstream error status={status} "
                    f"detail={str(exc)[:200]}",
                    flush=True,
                )
                yield _sse_error(
                    f"Decode node returned HTTP {status}: {str(exc)[:100]}",
                    f"upstream_http_{status}",
                )

            except httpx.ConnectError as exc:
                # Decode 节点不可达（连接拒绝、DNS 失败等）
                print(
                    f"[{request_id}] decode connect failed error={str(exc)[:200]}",
                    flush=True,
                )
                yield _sse_error(
                    "Decode node is unreachable. Please retry later.",
                    "decode_unreachable",
                )

            except httpx.ReadTimeout as exc:
                # 读取超时（单次 chunk 等待过久）
                print(
                    f"[{request_id}] decode read timeout error={str(exc)[:200]}",
                    flush=True,
                )
                yield _sse_error(
                    "Decode response timed out.",
                    "decode_read_timeout",
                )

            except httpx.WriteTimeout as exc:
                # 写入超时
                print(
                    f"[{request_id}] decode write timeout error={str(exc)[:200]}",
                    flush=True,
                )
                yield _sse_error(
                    "Decode request send timed out.",
                    "decode_write_timeout",
                )

            except httpx.ConnectTimeout as exc:
                # 连接超时
                print(
                    f"[{request_id}] decode connect timeout error={str(exc)[:200]}",
                    flush=True,
                )
                yield _sse_error(
                    "Decode node connection timed out.",
                    "decode_connect_timeout",
                )

    except Exception as exc:
        # 兜底：未预期的其他异常（如 AsyncClient 创建失败）
        print(
            f"[{request_id}] decode unexpected error type={type(exc).__name__} "
            f"detail={str(exc)[:300]}",
            flush=True,
        )
        yield _sse_error(
            f"Internal gateway error: {str(exc)[:100]}",
            "internal_error",
        )
    finally:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        print(
            f"[{request_id}] decode stream done chunks={chunk_count} "
            f"bytes={byte_count} elapsed_ms={elapsed_ms}",
            flush=True,
        )
```

### 关键变更点说明

| 异常类型 | 原行为 | 新行为 | SSE error code |
|----------|--------|--------|----------------|
| HTTPStatusError (4xx/5xx) | 裸 500 | SSE error event | `upstream_http_{status}` |
| ConnectError (拒绝/DNS) | 裸 500 | SSE error event | `decode_unreachable` |
| ReadTimeout | 裸 500 | SSE error event | `decode_read_timeout` |
| WriteTimeout | 裸 500 | SSE error event | `decode_write_timeout` |
| ConnectTimeout | 裸 500 | SSE error event | `decode_connect_timeout` |
| 其他 Exception | 裸 500 | SSE error event | `internal_error` |

### 验证方法

```bash
# 场景 A：Decode 节点不可达
docker stop vllm-decode-cluster

curl -N -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-mvp-change-me" \
  -d '{"model":"/model","messages":[{"role":"user","content":"hi"}],"stream":true}'

# 预期输出（SSE 格式的 error event）:
# data: {"error":{"message":"Decode node is unreachable...","type":"server_error","code":"decode_unreachable"}}

# 场景 B：Decode 流式中途断开
# 1. 先发一个正常请求让它开始流式输出
# 2. 在输出过程中另一个终端执行: docker stop vllm-decode-cluster
# 3. 观察 client 是否收到 SSE error event 而非连接直接断开

# 恢复
docker start vllm-decode-cluster
```

**验收标准**：
- 故障场景下客户端收到的是 **SSE data event 格式的结构化错误**，而非裸 500
- error 中包含可识别的 `code` 字段用于程序化判断

---

## F0-03：修复 GW-C01 — AsyncClient 改为应用级单例

**问题编号**：GW-C01 (Critical)
**问题位置**：[gateway.py 全局](file:///d:/ai-infa/backend/gateway.py) — AsyncClient 创建/销毁分散在 L133, L152, L179 三处
**预期耗时**：30 分钟
**风险等级**：修改应用生命周期管理，影响所有 HTTP 通信路径

### 问题描述

每个请求创建 3 个独立 AsyncClient（即用即销），高并发下导致：(1) 连接无法复用增加 7-18ms/请求延迟；(2) TIME_WAIT 端口堆积可能耗尽 fd。

### 修改步骤

此修改涉及 **4 个区域**，按顺序逐一修改。

#### Step 1：在文件头部 import 区域添加 lifespan 导入

**位置**：[gateway.py#L1-L9](file:///d:/ai-infra/backend/gateway.py#L1-L9)

在现有 import 之后添加 `contextlib` 导入：

```python
# === 当前代码（L1-L9）===
import json
import os
import time
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

# === 修改后 ===
import contextlib   # ← 新增
import json
import os
import time
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
```

#### Step 2：在全局变量区（L12-L18 之后）添加共享 client 和 lifespan

**位置**：[gateway.py#L18](file:///d:/ai-infra/backend/gateway.py#L18) 之后（`PREFILL_MAX_TOKENS` 之后）

在 `PREFILL_MAX_TOKENS` 那行之后、空行之后、`app = FastAPI(...)` 之前，插入以下代码：

```python
# === 在此处新增（L19 附近）===

# GW-C01 FIX: 应用级共享 AsyncClient，避免每请求创建/销毁
_shared_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """获取应用级共享的 httpx AsyncClient 单例。"""
    if _shared_client is None:
        raise RuntimeError("Shared httpx client not initialized — check lifespan")
    return _shared_client


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时创建共享 client，关闭时清理资源。"""
    global _shared_client
    _shared_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=10.0,
            read=max(PREFILL_TIMEOUT_SECONDS, DECODE_TIMEOUT_SECONDS),
            write=30.0,
            pool=30.0,
        ),
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
        ),
    )
    print("[gateway] shared httpx client initialized", flush=True)
    yield
    await _shared_client.aclose()
    _shared_client = None
    print("[gateway] shared httpx client closed", flush=True)


# === 原有 app 定义改为使用 lifespan ===
```

#### Step 3：修改 FastAPI app 创建，启用 lifespan

**位置**：[gateway.py#L20](file:///d:/ai-infa/backend/gateway.py#L20)

```python
# === 当前代码 ===
app = FastAPI(title="DeepSeek vLLM PD Separation Gateway")

# === 修改后 ===
app = FastAPI(title="DeepSeek vLLM PD Separation Gateway", lifespan=lifespan)
```

#### Step 4：替换 run_prefill 中的 AsyncClient 创建

**位置**：[gateway.py#L133](file:///d:/ai-infra/backend/gateway.py#L133)

```python
    # === 当前代码（L133）===
    async with httpx.AsyncClient(timeout=PREFILL_TIMEOUT_SECONDS) as client:
        prefill_status, prefill_ms, prefill_error = await run_prefill(

    # === 修改后 ===
    client = get_client()
    prefill_status, prefill_ms, prefill_error = await run_prefill(
```

同时删除缩进（`run_prefill` 调用不再在 `async with` 块内）：

```python
    client = get_client()
    prefill_status, prefill_ms, prefill_error = await run_prefill(   # 不再缩进
        client,
        body,
        headers,
        request_id,
    )                                                                   # 与 if 对齐
```

#### Step 5：替换非流式 Decode 中的 AsyncClient 创建

**位置**：[gateway.py#L152-L153](file:///d:/ai-infra/backend/gateway.py#L152-L153)

```python
    # === 当前代码 ===
    async with httpx.AsyncClient(timeout=DECODE_TIMEOUT_SECONDS) as client:
        decode_response = await client.post(DECODE_NODE_URL, json=body, headers=headers)

    # === 修改后 ===
    decode_response = await get_client().post(DECODE_NODE_URL, json=body, headers=headers)
```

#### Step 6：替换流式 Decode 中的 AsyncClient 创建

**位置**：[gateway.py#L179](file:///d:/ai-infra/backend/gateway.py#L179)（注意：这是在 F0-03 修改后的新行号，原始为 L179）

```python
    # === 当前代码 ===
    async with httpx.AsyncClient(timeout=DECODE_TIMEOUT_SECONDS) as client:
        async with client.stream("POST", DECODE_NODE_URL, json=body, headers=headers) as response:

    # === 修改后 ===
    shared = get_client()
    async with shared.stream("POST", DECODE_NODE_URL, json=body, headers=headers) as response:
```

### 修改前后对比总览

| 位置 | 修改前 | 修改后 |
|------|--------|--------|
| Import 区 | 无 `contextlib` | 新增 `import contextlib` |
| 全局变量区 | 无共享 client | `_shared_client` + `get_client()` + `lifespan()` |
| app 创建 | `FastAPI(title=...)` | `FastAPI(title=..., lifespan=lifespan)` |
| L133 (Prefill 调用处) | `async with httpx.AsyncClient(...) as client:` | `client = get_client()` |
| L152 (非流式 Decode) | `async with httpx.AsyncClient(...) as client:` | `get_client().post(...)` |
| L179 (流式 Decode) | `async with httpx.AsyncClient(...) as client:` | `shared = get_client(); shared.stream(...)` |

### 验证方法

```bash
# 1. 重启 Gateway 容器（因为代码已修改需要重新 build）
cd d:\ai-infra\compose
docker compose build gateway
docker compose up -d gateway

# 2. 确认日志中出现 client 初始化消息
docker logs deepseek-pd-gateway 2>&1 | grep "shared httpx"

# 3. 发送正常请求确认功能不受影响
curl -s -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-mvp-change-me" \
  -d '{"model":"/model","messages":[{"role":"user","content":"hi"}],"stream":false}' \
  | python -m json.tool

# 4. 高并发压测观察 TIME_WAIT 数量（对比修复前后的差异）
# 详见 F0-05 的并发测试脚本
```

**验收标准**：
- 日志出现 `[gateway] shared httpx client initialized`
- 功能请求正常返回
- 高并发下 TIME_WAIT 数量显著减少（见 F0-05 验证数据）

---

## F0-04：修复 CFG-M01 — 添加 resource limits

**问题编号**：CFG-M01 (Major)
**问题位置**：[docker-compose.yml](file:///d:/ai-infra/compose/docker-compose.yml) — deploy.resources 段
**预期耗时**：15 分钟
**风险等级**：低（仅添加 limits，不影响正常运行）

### 修改步骤

#### Step 1：为 vllm-prefill 添加 memory limit

**位置**：[docker-compose.yml#L50-L56](file:///d:/ai-infra/compose/docker-compose.yml#L50-L56)

```yaml
    # === 当前代码 ===
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              ids: ['0', '1', '2', '3']
              capabilities: [gpu]

    # === 修改后 ===
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              ids: ['0', '1', '2', '3']
              capabilities: [gpu]
        limits:
          memory: 128GB
```

#### Step 2：为 vllm-decode 添加 memory limit

**位置**：[docker-compose.yml#L103-L109](file:///d:/ai-infra/compose/docker-compose.yml#L103-L109)

同样的修改，在 `reservations` 同级添加 `limits.memory: 128GB`。

#### Step 3：为 lmcache-server 添加 memory limit

**位置**：[docker-compose.yml lmcache-server service](file:///d:/ai-infra/compose/docker-compose.yml#L11-L33)

lmcache-server 当前没有 `deploy` 段，需要在 `restart:` 之前添加：

```yaml
    restart: unless-stopped
    # === 新增 ===
    deploy:
      resources:
        limits:
          memory: 16GB
    logging: *default-logging
```

#### Step 4：为 gateway 添加 memory limit

**位置**：[docker-compose.yml gateway service](file:///d:/ai-infra/compose/docker-compose.yml#L143-L176)

同样在 `restart:` 之前添加：

```yaml
    restart: unless-stopped
    # === 新增 ===
    deploy:
      resources:
        limits:
          memory: 4GB
    logging: *default-logging
```

### 注意事项

- **128GB / 16GB / 4GB 为建议值**，请根据宿主机实际物理内存调整
- 如果宿主机总内存为 256GB，上述配置预留了约 108GB 给系统和其他进程
- Docker Compose 的 `memory` limit 单位可以是 `B`, `K`, `M`, `G`

---

## F0-05：修复 SEC-M01 — 启动时检测弱默认 API Key

**问题编号**：SEC-M01 (Major)
**问题位置**：[gateway.py](file:///d:/ai-infra/backend/gateway.py) — 启动逻辑
**预期耗时**：15 分钟
**风险等级**：低（仅添加警告检测，不阻断启动）

### 修改步骤

#### Step 1：在 app 创建之后添加弱密钥检测

**位置**：[gateway.py#L20](file:///d:/ai-infra/backend/gateway.py#L20) 之后（`app = FastAPI(...)` 之后）

```python
    # === 当前代码 ===
    app = FastAPI(title="DeepSeek vLLM PD Separation Gateway", lifespan=lifespan)

    # === 修改后 ===
    app = FastAPI(title="DeepSeek vLLM PD Separation Gateway", lifespan=lifespan)

    # SEC-M01 FIX: 检测弱默认 API Key 并打印警告
    _WEAK_DEFAULT_KEYS = {"sk-mvp-change-me", "", "none", "null"}
    if GATEWAY_API_KEY in _WEAK_DEFAULT_KEYS:
        print(
            "[WARNING] GATEWAY_API_KEY is set to a weak default value. "
            "Set a strong key via the GATEWAY_API_KEY environment variable.",
            flush=True,
        )
    if UPSTREAM_API_KEY in _WEAK_DEFAULT_KEYS and UPSTREAM_API_KEY != "":
        print(
            "[WARNING] UPSTREAM_API_KEY is set to a weak default value. "
            "Set a strong key via the VLLM_API_KEY environment variable.",
            flush=True,
        )
```

### 验证方法

```bash
# 使用默认 .env（含 sk-mvp-change-me）启动
docker compose up gateway 2>&1 | head -20

# 预期看到:
# [WARNING] GATEWAY_API_KEY is set to a weak default value...
# [WARNING] UPSTREAM_API_KEY is set to a weak default value...

# 使用强密钥启动（不应出现警告）
GATEWAY_API_KEY=sk-real-strong-key-abc123 docker compose up gateway 2>&1 | head -20
```

---

## 修改完成后的完整 gateway.py 结构预览

```
gateway.py (F0-01~F0-03 全部修改后):

  L1-L11:   import 区 (新增 contextlib)
  L12-L18:  全局环境变量 (不变)
  L19-L45:  新增: _shared_client + get_client() + lifespan() + 弱密钥检测
  L46:      app = FastAPI(lifespan=lifespan)
  L47-L70:  authorize_client (不变)
  L71-L85:  upstream_headers (不变)
  L86-L101: summarize_messages (不变)
  L102-L114: prefill_payload (不变)
  L116-L126: healthz (不变)
  L127-L155: run_prefill (不变,但接收外部传入的 client)
  L156-L165: pd_headers (不变)
  L167-L210: dispatch_chat_completions
             ├── F0-02: prefill_error → 立即 return 502 (L180-L188 新增)
             └── F0-03: 使用 get_client() 替代 AsyncClient 创建 (L198, L207)
  L212-L280: stream_decode
             └── F0-03: 完整异常处理 + _sse_error() 内部函数 (替换全部)
  L282-L285: __main__ (不变)
```

---

## F0-06：冒烟测试全量验证

完成 F0-01 ~ F0-05 后，**必须** 执行以下全部测试项。

| # | 测试项 | 操作命令 | 预期结果 | 通过 |
|---|--------|----------|----------|------|
| V-01 | Gateway 启动正常 | `docker compose up -d gateway && sleep 5 && docker ps \| grep gateway` | Status: Up / healthy | ☐ |
| V-02 | 共享 Client 初始化 | `docker logs deepseek-pd-gateway 2>&1 \| grep "shared httpx"` | 出现初始化日志 | ☐ |
| V-03 | 弱密钥警告（如适用） | `docker logs deepseek-pd-gateway 2>&1 \| grep WARNING` | 如使用默认密钥则出现 WARNING | ☐ |
| V-04 | Healthz 端点 | `curl -sf http://localhost:8000/healthz` | 200 + JSON | ☐ |
| V-05 | 认证拒绝（无 Key） | `curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8000/v1/chat/completions -H "Content-Type: application/json" -d '{"messages":[{"role":"user","content":"hi"}]}'` | 401 | ☐ |
| V-06 | 非**流式正常请求 | `curl -sf -X POST http://localhost:8000/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer sk-mvp-change-me" -d '{"model":"/model","messages":[{"role":"user","content":"say hi"}],"stream":false,"max_tokens":5}'` | 200 + choices | ☐ |
| V-07 | **流式正常请求** | `curl -N -sf -X POST http://localhost:8000/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer sk-mvp-change-me" -d '{"model":"/model","messages":[{"role":"user","content":"say hi"}],"stream":true,"max_tokens":5}'` | 200 + SSE events | ☐ |
| V-08 | **Prefill 失败→502** (F0-02) | `docker stop vllm-prefill-cluster` 后发送请求 | 502 + prefill_error body | ☐ |
| V-09 | **流式 Decode 断连→SSE error** (F0-03) | 流式请求中途 `docker stop vllm-decode-cluster` | SSE error event (非裸 500) | ☐ |
| V-10 | Resource Limits 生效 | `docker inspect deepseek-pd-gateway --format='{{.HostConfig.Memory}}'` | 显示限制值 | ☐ |
| V-11 | 恢复服务 | `docker start vllm-prefill-cluster vllm-decode-cluster` | 两节点恢复 healthy | ☐ |
| V-12 | **高并发 fd 监控** (F0-01) | 运行 `test_critical_stress.py` 见下方脚本 | TIME_WAIT 稳定无增长 | ☐ |

> **V-08 和 V-09 是本次修复的核心验证项，必须手动操作确认。**

---

## 回滚方案

若修改后出现问题：

```bash
# 1. 回滚 gateway.py
copy backend\gateway.py.bak backend\gateway.py

# 2. 重新构建并启动
cd d:\ai-infra\compose
docker compose build gateway
docker compose up -d gateway

# 3. 验证回滚后功能正常
curl -sf http://localhost:8000/healthz
```

---

## 执行记录

| 步骤 | 修复项 | 开始时间 | 完成时间 | 执行人 | 状态 |
|------|--------|----------|----------|--------|------|
| F0-01 | GW-C02: Prefill 失败返回 502 | | | | ☐ 待执行 |
| F0-02 | GW-C03: 流式异常处理 | | | | ☐ 待执行 |
| F0-03 | GW-C01: AsyncClient 单例化 | | | | ☐ 待执行 |
| F0-04 | CFG-M01: resource limits | | | | ☐ 待执行 |
| F0-05 | SEC-M01: 弱密钥检测 | | | | ☐ 待执行 |
| F0-06 | 冒烟测试全量验证 | | | | ☐ 待执行 |
