import json
import os
import time
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse


PREFILL_NODE_URL = os.getenv("PREFILL_NODE_URL", "http://vllm-prefill:8001/v1/chat/completions")
DECODE_NODE_URL = os.getenv("DECODE_NODE_URL", "http://vllm-decode:8002/v1/chat/completions")
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY", "")
GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY", "")
PREFILL_TIMEOUT_SECONDS = float(os.getenv("PREFILL_TIMEOUT_SECONDS", "300"))
DECODE_TIMEOUT_SECONDS = float(os.getenv("DECODE_TIMEOUT_SECONDS", "300"))
PREFILL_MAX_TOKENS = int(os.getenv("PREFILL_MAX_TOKENS", "1"))

app = FastAPI(title="DeepSeek vLLM PD Separation Gateway")


def authorize_client(request: Request) -> None:
    if not GATEWAY_API_KEY:
        return
    expected = f"Bearer {GATEWAY_API_KEY}"
    actual = request.headers.get("authorization", "")
    if actual != expected:
        raise PermissionError("invalid or missing bearer token")


def upstream_headers(request: Request) -> dict[str, str]:
    headers = dict(request.headers)
    for key in ("host", "content-length", "authorization"):
        headers.pop(key, None)
    if UPSTREAM_API_KEY:
        headers["authorization"] = f"Bearer {UPSTREAM_API_KEY}"
    return headers


def summarize_messages(body: dict[str, Any]) -> str:
    messages = body.get("messages", [])
    summary = [
        {
            "role": message.get("role", ""),
            "content": str(message.get("content", ""))[:120],
        }
        for message in messages[:3]
        if isinstance(message, dict)
    ]
    return json.dumps(summary, ensure_ascii=False)


def prefill_payload(body: dict[str, Any]) -> dict[str, Any]:
    payload = dict(body)
    payload["max_tokens"] = PREFILL_MAX_TOKENS
    payload["stream"] = False
    return payload


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "prefill_node_url": PREFILL_NODE_URL,
        "decode_node_url": DECODE_NODE_URL,
        "prefill_max_tokens": PREFILL_MAX_TOKENS,
        "gateway_auth_enabled": bool(GATEWAY_API_KEY),
        "upstream_auth_enabled": bool(UPSTREAM_API_KEY),
    }


async def run_prefill(
    client: httpx.AsyncClient,
    body: dict[str, Any],
    headers: dict[str, str],
    request_id: str,
) -> tuple[str, int, str]:
    started = time.perf_counter()
    try:
        response = await client.post(
            PREFILL_NODE_URL,
            json=prefill_payload(body),
            headers=headers,
        )
        response.raise_for_status()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        print(
            f"[{request_id}] prefill ok status={response.status_code} "
            f"elapsed_ms={elapsed_ms}",
            flush=True,
        )
        return "ok", elapsed_ms, ""
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        message = str(exc)[:500]
        print(
            f"[{request_id}] prefill failed elapsed_ms={elapsed_ms} error={message}",
            flush=True,
        )
        return "failed", elapsed_ms, message


def pd_headers(prefill_status: str, prefill_ms: int, request_id: str) -> dict[str, str]:
    return {
        "x-request-id": request_id,
        "x-prefill-status": prefill_status,
        "x-prefill-ms": str(prefill_ms),
    }


@app.post("/v1/chat/completions")
async def dispatch_chat_completions(request: Request) -> Response:
    try:
        authorize_client(request)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)

    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)

    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    headers = upstream_headers(request)
    stream = bool(body.get("stream", False))

    print(
        f"[{request_id}] inbound model={body.get('model')} stream={stream} "
        f"max_tokens={body.get('max_tokens')} messages={summarize_messages(body)}",
        flush=True,
    )

    async with httpx.AsyncClient(timeout=PREFILL_TIMEOUT_SECONDS) as client:
        prefill_status, prefill_ms, prefill_error = await run_prefill(
            client,
            body,
            headers,
            request_id,
        )

    response_headers = pd_headers(prefill_status, prefill_ms, request_id)
    if prefill_error:
        response_headers["x-prefill-error"] = prefill_error

    if stream:
        return StreamingResponse(
            stream_decode(body, headers, request_id),
            media_type="text/event-stream",
            headers=response_headers,
        )

    async with httpx.AsyncClient(timeout=DECODE_TIMEOUT_SECONDS) as client:
        decode_response = await client.post(DECODE_NODE_URL, json=body, headers=headers)

    content_type = decode_response.headers.get("content-type", "application/json")
    if "application/json" in content_type:
        return JSONResponse(
            decode_response.json(),
            status_code=decode_response.status_code,
            headers=response_headers,
        )

    return Response(
        decode_response.content,
        status_code=decode_response.status_code,
        media_type=content_type,
        headers=response_headers,
    )


async def stream_decode(
    body: dict[str, Any],
    headers: dict[str, str],
    request_id: str,
):
    started = time.perf_counter()
    chunk_count = 0
    byte_count = 0
    async with httpx.AsyncClient(timeout=DECODE_TIMEOUT_SECONDS) as client:
        async with client.stream("POST", DECODE_NODE_URL, json=body, headers=headers) as response:
            print(
                f"[{request_id}] decode stream connected status={response.status_code}",
                flush=True,
            )
            async for chunk in response.aiter_bytes():
                chunk_count += 1
                byte_count += len(chunk)
                yield chunk

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    print(
        f"[{request_id}] decode stream done chunks={chunk_count} "
        f"bytes={byte_count} elapsed_ms={elapsed_ms}",
        flush=True,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
