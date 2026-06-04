#!/usr/bin/env python3
"""
Gateway Critical Issues 高并发模拟验证脚本
============================================

针对 v2 审核报告中的 3 个 Gateway Critical 问题进行压力验证：

  GW-C01: AsyncClient 每请求创建销毁 → fd 耗尽 / TIME_WAIT 堆积
  GW-C02: Prefill 失败仍执行 Decode → 错误行为验证
  GW-C03: 流式 Decode 无异常处理 → 500 裸错误验证

使用方法:
  1. 确保 Gateway 已启动: cd compose && docker compose up -d gateway
  2. 运行本脚本:  python tests/test_critical_stress.py
  3. 观察控制台输出的实时指标和最终报告

依赖: pip install httpx asyncio aiohttp

注意: 此脚本模拟高并发场景，会对 Gateway 产生真实负载。
     请勿在生产环境运行。
"""

import asyncio
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import httpx

# ============================================================
# 配置区 —— 根据实际环境调整
# ============================================================

GATEWAY_BASE_URL = os.getenv("GATEWAY_URL", "http://localhost:8000")
GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY", "sk-mvp-change-me")

# 并发参数
CONCURRENT_USERS = os.getenv("STRESS_CONCURRENT", "50")  # 默认 50 并发
RAMP_UP_SECONDS = float(os.getenv("STRESS_RAMP_UP", "5"))  # 5 秒内爬升到目标并发
TEST_DURATION_SECONDS = float(os.getenv("STRESS_DURATION", "30"))  # 持续 30 秒

# 请求参数
REQUEST_TIMEOUT = 60.0  # 单请求超时秒数


# ============================================================
# 数据模型
# ============================================================

class TestScenario(Enum):
    NORMAL_NON_STREAM = "normal_non_stream"
    NORMAL_STREAM = "normal_stream"
    PREFILL_FAILURE = "prefill_failure"
    DECODE_STREAM_FAILURE = "decode_stream_failure"


@dataclass
class RequestResult:
    scenario: TestScenario
    request_id: str
    status_code: int
    elapsed_ms: float
    error: str | None = None
    has_sse_error: bool = False
    sse_error_code: str | None = None
    prefill_status_header: str | None = None
    prefill_ms_header: str | None = None
    has_prefill_error_header: bool = False
    response_snippet: str = ""


@dataclass
class ScenarioStats:
    scenario: TestScenario
    total: int = 0
    success: int = 0
    failures: int = 0
    status_codes: dict[int, int] = field(default_factory=dict)
    latencies: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    sse_error_count: int = 0
    bare_500_count: int = 0  # 无结构的裸 500
    correct_502_on_prefill_fail: int = 0  # GW-C02 修复验证
    correct_sse_error_on_decode_fail: int = 0  # GW-C03 修复验证

    @property
    def avg_latency(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def p50_latency(self) -> float:
        if not self.latencies:
            return 0.0
        s = sorted(self.latencies)
        return s[int(len(s) * 0.50)]

    @property
    def p99_latency(self) -> float:
        if not self.latencies:
            return 0.0
        s = sorted(self.latencies)
        return s[min(int(len(s) * 0.99), len(s) - 1)]

    @property
    def error_rate(self) -> float:
        return self.failures / self.total * 100 if self.total > 0 else 0.0


@dataclass
class SystemMetrics:
    timestamp: float
    tcp_established: int = 0
    tcp_time_wait: int = 0
    tcp_close_wait: int = 0
    active_fd_count: int = 0
    process_memory_mb: float = 0.0


# ============================================================
# 系统指标采集（跨平台）
# ============================================================

def collect_system_metrics() -> SystemMetrics:
    """采集当前系统 TCP 连接状态和 FD 使用情况。"""
    metrics = SystemMetrics(timestamp=time.time())

    system = platform.system().lower()

    if system == "windows":
        # Windows: 使用 netstat 采集 TCP 状态
        try:
            result = subprocess.run(
                ["netstat", "-an"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            lines = result.stdout.splitlines()
            for line in lines:
                line_upper = line.upper()
                if "ESTABLISHED" in line_upper:
                    metrics.tcp_established += 1
                elif "TIME_WAIT" in line_upper:
                    metrics.tcp_time_wait += 1
                elif "CLOSE_WAIT" in line_upper:
                    metrics.tcp_close_wait += 1
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        # Windows 进程 FD 信息有限，跳过
    else:
        # Linux/macOS: 使用 ss 或 netstat
        for cmd in [["ss", "-s"], ["netstat", "-an"]]:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                lines = result.stdout.splitlines()
                for line in lines:
                    upper = line.upper()
                    if "ESTAB" in upper:
                        # ss -s 格式: "TCP:   X (estab Y, ...)"
                        import re
                        m = re.search(r'estab\s+(\d+)', upper)
                        if m:
                            metrics.tcp_established = int(m.group(1))
                    elif "TIME_WAIT" in upper:
                        metrics.tcp_time_wait += 1
                    elif "CLOSE_WAIT" in upper:
                        metrics.tcp_close_wait += 1
                break
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue

        # Linux: 读取 /proc/self/fd
        try:
            metrics.active_fd_count = len(os.listdir("/proc/self/fd"))
        except (FileNotFoundError, PermissionError):
            pass

    return metrics


# ============================================================
# 请求构造
# ============================================================

def make_chat_payload(stream: bool = False) -> dict[str, Any]:
    """构造标准的 chat completion 请求体。"""
    return {
        "model": "/model",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Say hello in one sentence."},
        ],
        "max_tokens": 10,
        "stream": stream,
        "temperature": 0.7,
    }


def make_headers() -> dict[str, str]:
    """构造带认证的请求头。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if GATEWAY_API_KEY:
        headers["Authorization"] = f"Bearer {GATEWAY_API_KEY}"
    return headers


# ============================================================
# 响应解析与验证
# ============================================================

def parse_non_stream_response(
    scenario: TestScenario,
    resp: httpx.Response,
    elapsed_ms: float,
) -> RequestResult:
    """解析非流式响应，提取关键验证字段。"""
    result = RequestResult(
        scenario=scenario,
        request_id=resp.headers.get("x-request-id", "unknown"),
        status_code=resp.status_code,
        elapsed_ms=elapsed_ms,
        prefill_status_header=resp.headers.get("x-prefill-status"),
        prefill_ms_header=resp.headers.get("x-prefill-ms"),
        has_prefill_error_header="x-prefill-error" in resp.headers,
    )

    try:
        body = resp.json()
        result.response_snippet = json.dumps(body, ensure_ascii=False)[:200]
    except Exception:
        result.response_snippet = resp.text[:200]

    # GW-C02 验证: Prefill 失败场景是否正确返回 502
    if scenario == TestScenario.PREFILL_FAILURE:
        if resp.status_code == 502:
            try:
                err = resp.json().get("error", {})
                if err.get("code") == "prefill_stage_failure":
                    result.correct_502_on_prefill_fail = True
            except Exception:
                pass
        # 反模式检测: 如果是 200 且有 prefill error header → 说明 GW-C02 未修复
        if resp.status_code == 200 and result.has_prefill_error_header:
            result.error = "BUG: Prefill failed but still returned 200 with decode result!"

    # 裸 500 检测
    if resp.status_code == 500:
        try:
            body = resp.json()
            if "error" not in body or not isinstance(body.get("error"), dict):
                result.bare_500_count = 1
        except Exception:
            result.bare_500_count = 1

    if resp.status_code >= 400:
        result.error = f"HTTP {resp.status_code}: {result.response_snippet[:100]}"

    return result


async def parse_stream_response(
    scenario: TestScenario,
    request_id: str,
    elapsed_ms: float,
    raw_chunks: list[bytes],
    status_code: int,
    response_headers: dict[str, str],
) -> RequestResult:
    """解析流式响应，检测 SSE error event 的正确性。"""
    result = RequestResult(
        scenario=scenario,
        request_id=request_id,
        status_code=status_code,
        elapsed_ms=elapsed_ms,
        prefill_status_header=response_headers.get("x-prefill-status"),
        prefill_ms_header=response_headers.get("x-prefill-ms"),
        has_prefill_error_header="x-prefill-error" in response_headers,
    )

    full_body = b"".join(raw_chunks).decode("utf-8", errors="replace")
    result.response_snippet = full_body[:300]

    # 解析 SSE events
    for line in full_body.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            continue
        try:
            event_data = json.loads(data_str)
            if "error" in event_data:
                result.has_sse_error = True
                err = event_data.get("error", {})
                result.sse_error_code = err.get("code", "unknown")
                result.sse_error_count = 1

                # GW-C03 验证: Decode 故障场景是否返回结构化 SSE error
                if scenario == TestScenario.DECODE_STREAM_FAILURE:
                    if result.sse_error_code in (
                        "decode_unreachable",
                        "upstream_http_502",
                        "upstream_http_503",
                        "internal_error",
                    ):
                        result.correct_sse_error_on_decode_fail = True
        except json.JSONDecodeError:
            pass

    # 裸 500 检测: 如果 status_code=500 且无 SSE error → 裸错误
    if status_code == 500 and not result.has_sse_error:
        # 检查响应体是否像是一个裸 500 HTML 页面
        if "<!DOCTYPE" in full_body or "<html" in full_body.lower():
            result.bare_500_count = 1
            result.error = "BARE 500: No structured error in streaming failure"

    if status_code >= 400 and not result.error:
        result.error = f"HTTP {status_code} in stream mode"

    return result


# ============================================================
# 测试场景执行器
# ============================================================

async def execute_normal_non_stream(client: httpx.AsyncClient) -> RequestResult:
    """场景 A: 正常非流式请求（基线）。"""
    rid = f"ns-{uuid.uuid4().hex[:8]}"
    payload = make_chat_payload(stream=False)
    start = time.perf_counter()

    try:
        resp = await client.post(
            f"{GATEWAY_BASE_URL}/v1/chat/completions",
            json=payload,
            headers=make_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        elapsed = (time.perf_counter() - start) * 1000
        return parse_non_stream_response(TestScenario.NORMAL_NON_STREAM, resp, elapsed)
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000
        return RequestResult(
            scenario=TestScenario.NORMAL_NON_STREAM,
            request_id=rid,
            status_code=0,
            elapsed_ms=elapsed,
            error=str(exc)[:200],
        )


async def execute_normal_stream(client: httpx.AsyncClient) -> RequestResult:
    """场景 B: 正常流式请求（基线）。"""
    rid = f"st-{uuid.uuid4().hex[:8]}"
    payload = make_chat_payload(stream=True)
    start = time.perf_counter()

    try:
        chunks = []
        resp_headers = {}
        async with client.stream(
            "POST",
            f"{GATEWAY_BASE_URL}/v1/chat/completions",
            json=payload,
            headers=make_headers(),
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            resp_headers = dict(resp.headers)
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
        elapsed = (time.perf_counter() - start) * 1000
        return await parse_stream_response(
            TestScenario.NORMAL_STREAM, rid, elapsed, chunks,
            resp.status_code, resp_headers,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000
        return RequestResult(
            scenario=TestScenario.NORMAL_STREAM,
            request_id=rid,
            status_code=0,
            elapsed_ms=elapsed,
            error=f"Stream exception: {str(exc)[:200]}",
        )


async def execute_prefill_failure(client: httpx.AsyncClient) -> RequestResult:
    """
    场景 C: 模拟 Prefill 失败。

    策略: 发送一个超大 prompt（接近 max-model-len），触发 Prefill OOM 或超时。
    同时也作为 GW-C02 的验证场景: 即使 Prefill 失败也不应返回 Decode 结果。
    """
    rid = f"pf-{uuid.uuid4().hex[:8]}"
    # 构造一个较大的 prompt 来增加 Prefill 失败概率
    payload = {
        "model": "/model",
        "messages": [
            {
                "role": "user",
                "content": "Please repeat the word 'stress' exactly 1000 times: "
                           + ". " * 500,
            }
        ],
        "max_tokens": 5,
        "stream": False,
    }
    start = time.perf_counter()

    try:
        resp = await client.post(
            f"{GATEWAY_BASE_URL}/v1/chat/completions",
            json=payload,
            headers=make_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        elapsed = (time.perf_counter() - start) * 1000
        result = parse_non_stream_response(TestScenario.PREFILL_FAILURE, resp, elapsed)

        # 额外检查: 如果返回了 200 但带有 x-prefill-error header → GW-C02 BUG
        if resp.status_code == 200 and "x-prefill-error" in resp.headers:
            result.error = (
                "GW-C02 BUG DETECTED: Prefill failed but Gateway returned 200 "
                f"with decode result! x-prefill-error={resp.headers['x-prefill-error'][:100]}"
            )
        return result
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000
        return RequestResult(
            scenario=TestScenario.PREFILL_FAILURE,
            request_id=rid,
            status_code=0,
            elapsed_ms=elapsed,
            error=str(exc)[:200],
        )


async def execute_decode_stream_failure(client: httpx.AsyncClient) -> RequestResult:
    """
    场景 D: 流式 Decode 故障验证。

    策略: 发送流式请求并在极短超时下执行，
    增加 Decode 端连接中断/超时的概率来触发 GW-C03 相关代码路径。
    """
    rid = f"ds-{uuid.uuid4().hex[:8]}"
    payload = make_chat_payload(stream=True)
    # 使用较短的超时来增加触发概率
    start = time.perf_counter()

    try:
        chunks = []
        resp_headers = {}
        short_timeout = 5.0  # 故意缩短超时
        async with client.stream(
            "POST",
            f"{GATEWAY_BASE_URL}/v1/chat/completions",
            json=payload,
            headers=make_headers(),
            timeout=short_timeout,
        ) as resp:
            resp_headers = dict(resp.headers)
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
        elapsed = (time.perf_counter() - start) * 1000
        return await parse_stream_response(
            TestScenario.DECODE_STREAM_FAILURE, rid, elapsed, chunks,
            resp.status_code, resp_headers,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000
        result = RequestResult(
            scenario=TestScenario.DECODE_STREAM_FAILURE,
            request_id=rid,
            status_code=0,
            elapsed_ms=elapsed,
            error=str(exc)[:200],
        )
        # 对于 ReadTimeout / ConnectError 等，如果被 GW-C03 正确处理，
        # 异常不应该传播到这里（应该被转为 SSE error event）
        # 如果到了这里 → 可能 GW-C03 未完全生效
        exc_type = type(exc).__name__
        if exc_type in ("ReadTimeout", "ConnectError", "ConnectTimeout",
                         "WriteTimeout", "ProtocolError"):
            result.error = (
                f"GW-C03 POSSIBLE BUG: {exc_type} escaped to client instead of "
                f"being converted to SSE error event. Detail: {str(exc)[:150]}"
            )
        return result


# ============================================================
# 并发负载生成器
# ============================================================

async def worker_routine(
    worker_id: int,
    scenario: TestScenario,
    results_queue: asyncio.Queue[RequestResult],
    stop_event: asyncio.Event,
    rate_limiter: asyncio.Semaphore,
):
    """单个工作协程: 持续发送请求直到收到停止信号。"""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        request_count = 0
        while not stop_event.is_set():
            async with rate_limiter:
                if stop_event.is_set():
                    break

                try:
                    match scenario:
                        case TestScenario.NORMAL_NON_STREAM:
                            result = await execute_normal_non_stream(client)
                        case TestScenario.NORMAL_STREAM:
                            result = await execute_normal_stream(client)
                        case TestScenario.PREFILL_FAILURE:
                            result = await execute_prefill_failure(client)
                        case TestScenario.DECODE_STREAM_FAILURE:
                            result = await execute_decode_stream_failure(client)
                        case _:
                            result = RequestResult(
                                scenario=scenario,
                                request_id=f"unk-{worker_id}",
                                status_code=0,
                                elapsed_ms=0,
                                error=f"Unknown scenario: {scenario}",
                            )

                    await results_queue.put(result)
                    request_count += 1

                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    await results_queue.put(RequestResult(
                        scenario=scenario,
                        request_id=f"err-{worker_id}-{request_count}",
                        status_code=0,
                        elapsed_ms=0,
                        error=f"Worker exception: {str(exc)[:200]}",
                    ))


async def run_load_test(
    scenario: TestScenario,
    concurrent: int,
    duration: float,
    ramp_up: float,
) -> ScenarioStats:
    """运行单个场景的负载测试。"""
    stats = ScenarioStats(scenario=scenario)
    results_queue: asyncio.Queue[RequestResult] = asyncio.Queue()
    stop_event = asyncio.Event()
    rate_limiter = asyncio.Semaphore(concurrent * 2)  # 轻微过载以产生背压

    # 创建 worker
    workers = []
    for i in range(concurrent):
        # 爬升延迟: 将 worker 启动分散在 ramp_up 时间内
        delay = (i / concurrent) * ramp_up if ramp_up > 0 else 0
        worker = asyncio.create_task(
            worker_routine(i, scenario, results_queue, stop_event, rate_limiter)
        )
        if delay > 0:
            await asyncio.sleep(delay)
        workers.append(worker)

    # 运行指定时长
    await asyncio.sleep(duration)

    # 发送停止信号
    stop_event.set()

    # 等待 worker 结束
    await asyncio.gather(*workers, return_exceptions=True)

    # 收集结果
    while not results_queue.empty():
        try:
            result = results_queue.get_nowait()
            stats.total += 1

            if result.status_code > 0:
                stats.status_codes[result.status_code] = (
                    stats.status_codes.get(result.status_code, 0) + 1
                )

            if result.error or result.status_code >= 400:
                stats.failures += 1
                if result.error:
                    # 只保留前 20 条不同错误
                    if len(stats.errors) < 20 or result.error not in stats.errors:
                        stats.errors.append(result.error[:150])
            else:
                stats.success += 1

            if result.elapsed_ms > 0:
                stats.latencies.append(result.elapsed_ms)

            stats.sse_error_count += result.sse_error_count
            stats.bare_500_count += result.bare_500_count
            stats.correct_502_on_prefill_fail += result.correct_502_on_prefill_fail
            stats.correct_sse_error_on_decode_fail += result.correct_sse_error_on_decode_fail

        except asyncio.QueueEmpty:
            break

    return stats


# ============================================================
# 报告输出
# ============================================================

def print_banner():
    """打印测试 banner。"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("=" * 72)
    print(f"  Gateway Critical Issues Stress Test")
    print(f"  Timestamp (UTC): {now}")
    print(f"  Target: {GATEWAY_BASE_URL}")
    print(f"  Concurrency: {CONCURRENT_USERS} users")
    print(f"  Duration: {TEST_DURATION_SECONDS}s per scenario")
    print(f"  Platform: {platform.system()} {platform.release()}")
    print("=" * 72)
    print()


def print_scenario_report(stats: ScenarioStats, sys_before: SystemMetrics, sys_after: SystemMetrics):
    """打印单个场景的详细报告。"""
    print(f"\n{'─' * 68}")
    print(f"  SCENARIO: {stats.scenario.value}")
    print(f"{'─' * 68}")

    print(f"  Total Requests:     {stats.total:>6}")
    print(f"  Successful:         {stats.success:>6}")
    print(f"  Failed:              {stats.failures:>6}")
    print(f"  Error Rate:          {stats.error_rate:>6.2f}%")
    print()

    if stats.latencies:
        print(f"  Latency (ms):")
        print(f"    Avg:               {stats.avg_latency:>8.1f}")
        print(f"    P50:               {stats.p50_latency:>8.1f}")
        print(f"    P99:               {stats.p99_latency:>8.1f}")
    print()

    print(f"  Status Codes:")
    for code in sorted(stats.status_codes.keys()):
        count = stats.status_codes[code]
        bar = "#" * min(count, 50)
        marker = "← CRITICAL" if code >= 500 else ""
        print(f"    {code}: {count:>5}  {bar} {marker}")
    print()

    # Critical Issue 特定指标
    print(f"  --- Critical Issue Verification ---")

    # GW-C01 验证: TIME_WAIT 变化
    tw_delta = sys_after.tcp_time_wait - sys_before.tcp_time_wait
    tw_rate = tw_delta / max(stats.total, 1)
    print(f"  [GW-C01] TIME_WAIT delta:    +{tw_delta} ({tw_rate:.1f}/req)")
    if tw_delta > 100:
        print(f"           ⚠️  HIGH: TIME_WAIT accumulated significantly!")
    elif tw_delta > 20:
        print(f"           ⚡ MODERATE: Some TIME_WAIT buildup observed")
    else:
        print(f"           ✅ LOW: Minimal TIME_WAIT accumulation")

    est_delta = sys_after.tcp_established - sys_before.tcp_established
    print(f"  [GW-C01] ESTABLISHED delta: {est_delta:+d}")

    # GW-C02 验证: Prefill 失败行为
    if stats.scenario == TestScenario.PREFILL_FAILURE:
        print(f"  [GW-C02] Correct 502 on prefill fail: {stats.correct_502_on_prefill_fail}/{stats.total}")
        if stats.correct_502_on_prefill_fail > 0:
            print(f"           ✅ FIXED: Gateway correctly returns 502 on prefill failure")
        # 检测是否有 200 + prefill error header (bug pattern)
        bug_200_with_error = sum(
            1 for e in stats.errors if "GW-C02 BUG" in e
        )
        if bug_200_with_error > 0:
            print(f"           🔴 NOT FIXED: {bug_200_with_error} requests got 200 despite prefill fail!")

    # GW-C03 验证: 流式错误处理
    if stats.scenario in (TestScenario.NORMAL_STREAM, TestScenario.DECODE_STREAM_FAILURE):
        print(f"  [GW-C03] SSE error events:       {stats.sse_error_count}")
        print(f"  [GW-C03] Bare 500 responses:      {stats.bare_500_count}")
        if stats.scenario == TestScenario.DECODE_STREAM_FAILURE:
            print(f"  [GW-C03] Structured SSE on decode fail: {stats.correct_sse_error_on_decode_fail}")
            if stats.correct_sse_error_on_decode_fail > 0:
                print(f"           ✅ FIXED: Decode failures produce structured SSE errors")
            if stats.bare_500_count > 0:
                print(f"           🔴 NOT FIXED: {stats.bare_500_count} bare 500 errors detected!")

    if stats.errors:
        print(f"\n  Sample Errors (unique, first 5):")
        for i, err in enumerate(stats.errors[:5]):
            print(f"    [{i+1}] {err[:120]}")


def print_final_summary(all_stats: dict[TestScenario, ScenarioStats]):
    """打印最终汇总判定。"""
    print(f"\n{'=' * 72}")
    print(f"  FINAL VERDICT")
    print(f"{'=' * 72}")

    verdicts = []

    # GW-C01 判定
    ns = all_stats.get(TestScenario.NORMAL_NON_STREAM)
    st = all_stats.get(TestScenario.NORMAL_STREAM)
    if ns and st:
        combined_total = ns.total + st.total
        combined_tw = (ns.latencies and 0) + 0  # 简化: 主要看场景报告中 TIME_WAIT
        gw_c01_pass = True  # 由各场景的 TIME_WAIT delta 综合判断
        # 这里简化: 如果 TIME_WAIT/req < 1.0 则认为通过
        # 实际判定由人工查看各场景报告
        print(f"\n  [GW-C01] AsyncClient Lifecycle:")
        print(f"           Review each scenario's TIME_WAIT delta above.")
        print(f"           Target: < 1.0 TIME_WAIT per request.")
        print(f"           Verdict: MANUAL REVIEW REQUIRED ⚠️ ")

    # GW-C02 判定
    pf = all_stats.get(TestScenario.PREFILL_FAILURE)
    if pf:
        if pf.correct_502_on_prefill_fail > 0 and pf.bare_500_count == 0:
            print(f"\n  [GW-C02] Prefill Failure Handling:")
            print(f"           ✅ PASS: Returns 502 on prefill failure ({pf.correct_502_on_prefill_fail} confirmed)")
            verdicts.append(("GW-C02", "PASS"))
        else:
            print(f"\n  [GW-C02] Prefill Failure Handling:")
            print(f"           🔴 FAIL: Expected 502 not observed")
            if any("GW-C02 BUG" in e for e in pf.errors):
                print(f"           Evidence: Got 200 with decode result after prefill fail")
            verdicts.append(("GW-C02", "FAIL"))

    # GW-C03 判定
    ds = all_stats.get(TestScenario.DECODE_STREAM_FAILURE)
    if ds:
        if ds.correct_sse_error_on_decode_fail > 0 and ds.bare_500_count == 0:
            print(f"\n  [GW-C03] Stream Error Handling:")
            print(f"           ✅ PASS: Decode failures produce SSE errors ({ds.correct_sse_error_on_decode_fail})")
            verdicts.append(("GW-C03", "PASS"))
        else:
            print(f"\n  [GW-C03] Stream Error Handling:")
            print(f"           🔴 FAIL: Bare 500 errors detected: {ds.bare_500_count}")
            verdicts.append(("GW-C03", "FAIL"))

    print(f"\n  {'─' * 40}")
    total_pass = sum(1 for _, v in verdicts if v == "PASS")
    total_fail = sum(1 for _, v in verdicts if v == "FAIL")
    print(f"  Summary: {total_pass} PASSED, {total_fail} FAILED out of {len(verdicts)} checks")
    print(f"{'=' * 72}\n")


# ============================================================
# 主流程
# ============================================================

async def main():
    print_banner()

    # 连通性预检
    print("[1/5] Connectivity pre-check...")
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            resp = await c.get(f"{GATEWAY_BASE_URL}/healthz")
            if resp.status_code == 200:
                health = resp.json()
                print(f"       ✅ Gateway healthy: {json.dumps(health, ensure_ascii=False)}")
            else:
                print(f"       ❌ Gateway healthz returned {resp.status_code}")
                print(f"       Aborting. Ensure Gateway is running at {GATEWAY_BASE_URL}")
                sys.exit(1)
    except Exception as exc:
        print(f"       ❌ Cannot reach Gateway: {exc}")
        print(f"       Aborting. Start with: cd compose && docker compose up -d gateway")
        sys.exit(1)

    concurrent = int(CONCURRENT_USERS)
    scenarios = [
        (TestScenario.NORMAL_NON_STREAM, "Normal Non-Stream (baseline)"),
        (TestScenario.NORMAL_STREAM, "Normal Stream (baseline)"),
        (TestScenario.PREFILL_FAILURE, "Prefill Failure (GW-C02 test)"),
        (TestScenario.DECODE_STREAM_FAILURE, "Decode Stream Failure (GW-C03 test)"),
    ]

    all_stats: dict[TestScenario, ScenarioStats] = {}

    for idx, (scenario, desc) in enumerate(scenarios):
        print(f"\n[{idx+2}/{len(scenarios)+1}] Running: {desc}")
        print(f"       Concurrency={concurrent}, Duration={TEST_DURATION_SECONDS}s")

        sys_before = collect_system_metrics()
        stats = await run_load_test(
            scenario=scenario,
            concurrent=concurrent,
            duration=TEST_DURATION_SECONDS,
            ramp_up=RAMP_UP_SECONDS,
        )
        sys_after = collect_system_metrics()

        all_stats[scenario] = stats
        print_scenario_report(stats, sys_before, sys_after)

        # 场景间冷却
        if idx < len(scenarios) - 1:
            print(f"\n  Cooling down 5s before next scenario...")
            await asyncio.sleep(5)

    print_final_summary(all_stats)

    # 输出 JSON 结果供 CI/CD 消费
    output_file = f"stress_test_result_{int(time.time())}.json"
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "gateway_url": GATEWAY_BASE_URL,
            "concurrency": concurrent,
            "duration_seconds": TEST_DURATION_SECONDS,
        },
        "results": {
            s.value: {
                "total": st.total,
                "success": st.success,
                "failures": st.failures,
                "error_rate": round(st.error_rate, 2),
                "avg_latency_ms": round(st.avg_latency, 1),
                "p99_latency_ms": round(st.p99_latency, 1),
                "sse_errors": st.sse_error_count,
                "bare_500": st.bare_500_count,
                "correct_502_prefill": st.correct_502_on_prefill_fail,
                "correct_sse_decode": st.correct_sse_error_on_decode_fail,
            }
            for s, st in all_stats.items()
        },
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"Results saved to: {output_file}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
        sys.exit(130)
