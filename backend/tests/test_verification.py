import os
import statistics
import time

import httpx


API_URL = os.getenv("API_URL", "http://localhost:8000/v1/chat/completions")
MODEL = os.getenv("MODEL", "/model")
LONG_PREFIX_REPEAT = int(os.getenv("LONG_PREFIX_REPEAT", "1500"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "300"))


LONG_DOCUMENT = "这里是用于验证 PD 分离和 KV Cache 复用的长上下文文档。" * LONG_PREFIX_REPEAT
SYSTEM_PROMPT = (
    "你是一个负责验证分布式推理架构的 AI 助手。"
    "请严格基于以下长文档回答问题：\n"
    f"{LONG_DOCUMENT}"
)


def payload(user_prompt: str) -> dict:
    return {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 128,
        "stream": True,
    }


def run_stream_request(label: str, user_prompt: str) -> dict:
    print(f"\n=== {label} ===")
    started = time.perf_counter()
    first_token_at = None
    chunk_count = 0
    byte_count = 0

    with httpx.stream(
        "POST",
        API_URL,
        json=payload(user_prompt),
        timeout=REQUEST_TIMEOUT_SECONDS,
    ) as response:
        response.raise_for_status()
        prefill_status = response.headers.get("x-prefill-status", "unknown")
        prefill_ms = response.headers.get("x-prefill-ms", "unknown")
        request_id = response.headers.get("x-request-id", "unknown")
        print(
            f"request_id={request_id} "
            f"prefill_status={prefill_status} prefill_ms={prefill_ms}"
        )

        for chunk in response.iter_bytes():
            if not chunk:
                continue
            if first_token_at is None:
                first_token_at = time.perf_counter()
            chunk_count += 1
            byte_count += len(chunk)

    finished = time.perf_counter()
    ttft = (first_token_at or finished) - started
    total = finished - started
    print(
        f"ttft={ttft:.3f}s total={total:.3f}s "
        f"chunks={chunk_count} bytes={byte_count}"
    )
    return {
        "label": label,
        "ttft": ttft,
        "total": total,
        "chunks": chunk_count,
        "bytes": byte_count,
    }


def main() -> None:
    results = [
        run_stream_request("cold-long-prefix", "用一句话总结这份文档的核心主旨。"),
        run_stream_request("repeat-long-prefix-1", "列出这份文档最适合验证的两个指标。"),
        run_stream_request("repeat-long-prefix-2", "说明这个架构为什么适合 RAG 场景。"),
    ]

    repeated_ttft = [item["ttft"] for item in results[1:]]
    print("\n=== Summary ===")
    print(f"cold_ttft={results[0]['ttft']:.3f}s")
    print(f"repeat_ttft_avg={statistics.mean(repeated_ttft):.3f}s")
    print(f"repeat_ttft_min={min(repeated_ttft):.3f}s")
    print(
        "pass_hint="
        "重复长前缀请求的 TTFT 应显著低于 cold-long-prefix；"
        "同时网关日志应显示每次请求先经过 Prefill，再进入 Decode。"
    )


if __name__ == "__main__":
    main()
