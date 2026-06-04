import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "compose" / "docker-compose.yml"
COMPOSE_LMCACHE = ROOT / "compose" / "docker-compose.lmcache.yml"
COMPOSE_PREFILL = ROOT / "compose" / "docker-compose.prefill.yml"
COMPOSE_DECODE = ROOT / "compose" / "docker-compose.decode.yml"
COMPOSE_GATEWAY = ROOT / "compose" / "docker-compose.gateway.yml"
LMCACHE_CONFIG = ROOT / "compose" / "lmcache_config.yaml"
LMCACHE_DOCKERFILE = ROOT / "compose" / "lmcache.Dockerfile"
ENV_EXAMPLE = ROOT / "compose" / ".env.example"
GATEWAY_DOCKERFILE = ROOT / "backend" / "Dockerfile"
GATEWAY = ROOT / "backend" / "gateway.py"
GATEWAY_REQUIREMENTS = ROOT / "backend" / "requirements.txt"
DESIGN_DOC = ROOT / "docs" / "mvp-pd-separation-design.md"
OPS_RUNBOOK = ROOT / "docs" / "ops-runbook.md"
AUDIT_RESPONSE = ROOT / "docs" / "audits" / "AUDIT_RESPONSE.md"
THIRD_PARTY_RESPONSE = ROOT / "docs" / "audits" / "mvp-pd-separation-design_三方质疑回复.md"
OPS_SH = ROOT / "ops" / "pd-stack.sh"
OPS_PS1 = ROOT / "ops" / "pd-stack.ps1"
OPS_REMOTE_SH = ROOT / "ops" / "pd-remote.sh"
OPS_REMOTE_PS1 = ROOT / "ops" / "pd-remote.ps1"

COMPOSE_FILES = [
    COMPOSE,
    COMPOSE_LMCACHE,
    COMPOSE_PREFILL,
    COMPOSE_DECODE,
    COMPOSE_GATEWAY,
]


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def compose_text() -> str:
    return "\n".join(read(path) for path in COMPOSE_FILES)


def service_block(compose_text: str, service_name: str) -> str:
    pattern = rf"(?ms)^  {re.escape(service_name)}:\n(.*?)(?=^  [a-zA-Z0-9_-]+:\n|^networks:|\Z)"
    match = re.search(pattern, compose_text)
    if not match:
        raise AssertionError(f"service {service_name!r} not found")
    return match.group(1)


class MvpPdAssetsTest(unittest.TestCase):
    def test_compose_is_split_by_capability(self) -> None:
        for path in COMPOSE_FILES:
            self.assertTrue(path.exists(), f"{path.name} should exist")

        base = read(COMPOSE)
        self.assertIn("name: deepseek-pd-mvp", base)
        self.assertNotIn("lmcache-server:", base)
        self.assertNotIn("vllm-prefill:", base)
        self.assertNotIn("vllm-decode:", base)
        self.assertNotIn("gateway:", base)

        self.assertIn("lmcache-server:", read(COMPOSE_LMCACHE))
        self.assertIn("vllm-prefill:", read(COMPOSE_PREFILL))
        self.assertIn("vllm-decode:", read(COMPOSE_DECODE))
        self.assertIn("gateway:", read(COMPOSE_GATEWAY))

    def test_compose_uses_current_schema(self) -> None:
        for path in COMPOSE_FILES:
            content = read(path)
            self.assertNotRegex(content, r"(?m)^version:")
            self.assertNotRegex(content, r"(?m)^\s+ids:")

    def test_compose_declares_pd_topology_and_gateway(self) -> None:
        compose = compose_text()

        self.assertIn("lmcache-server:", compose)
        self.assertIn("vllm-prefill:", compose)
        self.assertIn("vllm-decode:", compose)
        self.assertIn("gateway:", compose)

        prefill = service_block(compose, "vllm-prefill")
        decode = service_block(compose, "vllm-decode")
        gateway = service_block(compose, "gateway")

        self.assertIn("CUDA_VISIBLE_DEVICES=0,1,2,3", prefill)
        self.assertIn("device_ids: ['0', '1', '2', '3']", prefill)
        self.assertNotIn("--model /model", prefill)
        self.assertIn("  /model", prefill)
        self.assertIn("--tensor-parallel-size 4", prefill)
        self.assertIn("--disable-custom-all-reduce", prefill)
        self.assertIn("--host 127.0.0.1", prefill)
        self.assertIn("--port 8001", prefill)

        self.assertIn("CUDA_VISIBLE_DEVICES=0,1,2,3", decode)
        self.assertIn("device_ids: ['4', '5', '6', '7']", decode)
        self.assertNotIn("--model /model", decode)
        self.assertIn("  /model", decode)
        self.assertIn("--tensor-parallel-size 4", decode)
        self.assertIn("--disable-custom-all-reduce", decode)
        self.assertIn("--host 127.0.0.1", decode)
        self.assertIn("--port 8002", decode)

        for block in (prefill, decode):
            self.assertIn("network_mode: host", block)
            self.assertIn("ipc: host", block)
            self.assertIn("NCCL_P2P_DISABLE=${NCCL_P2P_DISABLE:-1}", block)
            self.assertIn("NCCL_IB_DISABLE=${NCCL_IB_DISABLE:-1}", block)
            self.assertIn("NCCL_SHM_DISABLE=${NCCL_SHM_DISABLE:-0}", block)
            self.assertIn("NCCL_DEBUG=${NCCL_DEBUG:-WARN}", block)
            self.assertIn("NCCL_CUMEM_ENABLE=${NCCL_CUMEM_ENABLE:-0}", block)
            self.assertIn("NCCL_CUMEM_HOST_ENABLE=${NCCL_CUMEM_HOST_ENABLE:-0}", block)
            self.assertIn("VLLM_WORKER_MULTIPROC_METHOD=${VLLM_WORKER_MULTIPROC_METHOD:-spawn}", block)
            self.assertIn("OMP_NUM_THREADS=${OMP_NUM_THREADS:-1}", block)
            self.assertIn("LMCACHE_CONFIG_FILE=/vllm-workspace/lmcache_config.yaml", block)
            self.assertIn("--kv-transfer-config", block)
            self.assertIn("LMCacheMPConnector", block)
            self.assertIn("ulimits:", block)
            self.assertIn("memlock: -1", block)
            self.assertIn("nofile:", block)

        self.assertIn("--enable-chunked-prefill", prefill)

        self.assertIn("network_mode: host", gateway)
        self.assertNotIn("ports:", gateway)
        self.assertIn("PREFILL_NODE_URL=http://127.0.0.1:8001/v1/chat/completions", gateway)
        self.assertIn("DECODE_NODE_URL=http://127.0.0.1:8002/v1/chat/completions", gateway)

    def test_audit_security_hardening_contract(self) -> None:
        compose = compose_text()
        lmcache = service_block(compose, "lmcache-server")
        prefill = service_block(compose, "vllm-prefill")
        decode = service_block(compose, "vllm-decode")
        gateway = service_block(compose, "gateway")

        self.assertIn('max-size: "50m"', compose)
        self.assertIn('max-file: "5"', compose)

        self.assertIn("network_mode: host", lmcache)
        self.assertIn("ipc: host", lmcache)
        self.assertNotIn("ports:", lmcache)
        self.assertNotIn("expose:", lmcache)
        self.assertIn("${LMCACHE_MP_PORT:-6555}", lmcache)
        self.assertIn("${LMCACHE_HTTP_PORT:-8080}", lmcache)
        self.assertIn("${LMCACHE_IMAGE:-deepseek-lmcache-standalone:local}", lmcache)
        self.assertIn("build:", lmcache)
        self.assertIn("dockerfile: lmcache.Dockerfile", lmcache)
        self.assertIn("LMCACHE_BASE_IMAGE: ${LMCACHE_BASE_IMAGE:-lmcache/standalone:v0.4.5-cu129}", lmcache)
        self.assertNotIn("lmcache/lmcache-server", lmcache)
        self.assertIn("/opt/venv/bin/lmcache", lmcache)
        self.assertIn("server", lmcache)
        self.assertIn("--http-host", lmcache)
        self.assertIn("127.0.0.1", lmcache)
        self.assertIn("--l1-size-gb", lmcache)
        self.assertIn("${LMCACHE_L1_SIZE_GB:-60}", lmcache)
        self.assertIn("--eviction-policy", lmcache)
        self.assertIn("--max-workers", lmcache)
        self.assertIn("${LMCACHE_MAX_WORKERS:-4}", lmcache)
        self.assertIn("/healthcheck", lmcache)

        for block in (prefill, decode):
            self.assertNotIn("ports:", block)
            self.assertNotIn("expose:", block)
            self.assertIn("${VLLM_IMAGE:-lmcache/vllm-openai:v0.4.5-cu129}", block)
            self.assertIn("--api-key", block)
            self.assertIn("${VLLM_API_KEY:-sk-mvp-change-me}", block)
            self.assertIn("--enable-prefix-caching", block)
            self.assertIn("--kv-transfer-config", block)
            self.assertIn("LMCacheMPConnector", block)
            self.assertIn("lmcache.integration.vllm.lmcache_mp_connector", block)
            self.assertNotIn("lmcache.mp.host", block)
            self.assertIn('"lmcache.mp.port":${LMCACHE_MP_PORT:-6555}', block)
            self.assertIn("logging:", block)
            self.assertIn('max-size: "50m"', block)

        self.assertNotIn("ports:", gateway)
        self.assertIn("UPSTREAM_API_KEY=${VLLM_API_KEY:-sk-mvp-change-me}", gateway)
        self.assertIn("GATEWAY_API_KEY=${GATEWAY_API_KEY:-sk-mvp-change-me}", gateway)
        self.assertIn("healthcheck:", gateway)
        self.assertIn("logging:", gateway)
        self.assertIn('max-size: "50m"', gateway)

    def test_ops_scripts_wrap_compose_files(self) -> None:
        shell_script = read(OPS_SH)
        powershell_script = read(OPS_PS1)

        for script in (shell_script, powershell_script):
            self.assertIn("docker-compose.yml", script)
            self.assertIn("docker-compose.lmcache.yml", script)
            self.assertIn("docker-compose.prefill.yml", script)
            self.assertIn("docker-compose.decode.yml", script)
            self.assertIn("docker-compose.gateway.yml", script)
            self.assertIn("config", script)
            self.assertIn("up", script)
            self.assertIn("down", script)
            self.assertIn("logs", script)
            self.assertIn("health", script)
            self.assertIn("verify", script)

    def test_ops_scripts_can_repair_stale_vllm_containers(self) -> None:
        shell_script = read(OPS_SH)
        powershell_script = read(OPS_PS1)

        for script in (shell_script, powershell_script):
            self.assertIn("doctor", script)
            self.assertIn("repair", script)
            self.assertIn("--force-recreate", script)
            self.assertIn("--remove-orphans", script)
            self.assertIn("docker rm -f", script)
            self.assertIn("lmcache-server", script)
            self.assertIn("vllm-prefill-cluster", script)
            self.assertIn("latest-nightly", script)
            self.assertIn("--model /model", script)
            self.assertIn("--disable-custom-all-reduce", script)
            self.assertIn("NCCL_DEBUG", script)
            self.assertIn("NCCL_CUMEM_HOST_ENABLE", script)
            self.assertIn("VLLM_WORKER_MULTIPROC_METHOD", script)
            self.assertIn("lmcache/vllm-openai:v0.4.5-cu129", script)
            self.assertIn("deepseek-lmcache-standalone:local", script)

        runbook = read(OPS_RUNBOOK)
        self.assertIn("bash ops/pd-stack.sh doctor", runbook)
        self.assertIn("bash ops/pd-stack.sh repair prefill", runbook)

    def test_lmcache_standalone_image_is_patched_for_cli_runtime(self) -> None:
        dockerfile = read(LMCACHE_DOCKERFILE)
        lmcache_compose = service_block(compose_text(), "lmcache-server")
        env_example = read(ENV_EXAMPLE)

        self.assertIn("ARG LMCACHE_BASE_IMAGE=lmcache/standalone:v0.4.5-cu129", dockerfile)
        self.assertIn("FROM ${LMCACHE_BASE_IMAGE}", dockerfile)
        self.assertIn("python -m pip install", dockerfile)
        self.assertIn('"openai>=1,<2"', dockerfile)
        self.assertIn("--index-url", dockerfile)
        self.assertIn("--timeout", dockerfile)
        self.assertIn("--retries", dockerfile)

        self.assertIn("image: ${LMCACHE_IMAGE:-deepseek-lmcache-standalone:local}", lmcache_compose)
        self.assertIn("build:", lmcache_compose)
        self.assertIn("context: .", lmcache_compose)
        self.assertIn("dockerfile: lmcache.Dockerfile", lmcache_compose)
        self.assertIn("LMCACHE_BASE_IMAGE: ${LMCACHE_BASE_IMAGE:-lmcache/standalone:v0.4.5-cu129}", lmcache_compose)
        self.assertIn("PIP_INDEX_URL: ${PIP_INDEX_URL:-https://pypi.org/simple}", lmcache_compose)
        self.assertIn("PIP_EXTRA_INDEX_URL: ${PIP_EXTRA_INDEX_URL:-}", lmcache_compose)
        self.assertIn("PIP_TRUSTED_HOST: ${PIP_TRUSTED_HOST:-}", lmcache_compose)
        self.assertIn("PIP_DEFAULT_TIMEOUT: ${PIP_DEFAULT_TIMEOUT:-120}", lmcache_compose)
        self.assertIn("PIP_RETRIES: ${PIP_RETRIES:-10}", lmcache_compose)

        self.assertIn("VLLM_IMAGE=lmcache/vllm-openai:v0.4.5-cu129", env_example)
        self.assertIn("LMCACHE_IMAGE=deepseek-lmcache-standalone:local", env_example)
        self.assertIn("LMCACHE_BASE_IMAGE=lmcache/standalone:v0.4.5-cu129", env_example)
        self.assertNotIn("latest-nightly", env_example)
        self.assertNotIn("standalone:nightly", env_example)

    def test_remote_ops_scripts_verify_target_over_ssh(self) -> None:
        shell_script = read(OPS_REMOTE_SH)
        powershell_script = read(OPS_REMOTE_PS1)

        for script in (shell_script, powershell_script):
            self.assertIn("PD_REMOTE", script)
            self.assertIn("PD_REMOTE_DIR", script)
            self.assertIn("PD_REMOTE_PORT", script)
            self.assertIn("PD_REMOTE_PASSWORD", script)
            self.assertIn("ssh", script)
            self.assertIn("-p", script)
            self.assertIn("docker compose version", script)
            self.assertIn("bash ops/pd-stack.sh doctor", script)
            self.assertIn("bash ops/pd-stack.sh repair prefill", script)
            self.assertIn("bash ops/pd-stack.sh logs prefill", script)
            self.assertIn("exec", script)

        runbook = read(OPS_RUNBOOK)
        self.assertIn("PD_REMOTE=root@117.190.94.226", runbook)
        self.assertIn("PD_REMOTE_PORT=24132", runbook)
        self.assertIn("PD_REMOTE_PASSWORD", runbook)
        self.assertIn("bash ops/pd-remote.sh doctor", runbook)
        self.assertIn(".\\ops\\pd-remote.ps1 doctor", runbook)

    def test_lmcache_shared_backend_contract(self) -> None:
        config = read(LMCACHE_CONFIG)

        self.assertIn("chunk_size: 256", config)
        self.assertIn("local_cpu: true", config)
        self.assertIn("max_local_cpu_size: 5", config)
        self.assertNotIn('backend: "gpu"', config)
        self.assertNotIn("local_cpu_percentage: 0.4", config)
        self.assertNotIn("remote_url:", config)

    def test_gateway_is_container_aware_and_observable(self) -> None:
        gateway = read(GATEWAY)

        self.assertIn('os.getenv("PREFILL_NODE_URL"', gateway)
        self.assertIn('os.getenv("DECODE_NODE_URL"', gateway)
        self.assertIn('os.getenv("UPSTREAM_API_KEY"', gateway)
        self.assertIn('os.getenv("GATEWAY_API_KEY"', gateway)
        self.assertIn('@app.get("/healthz")', gateway)
        self.assertIn("authorize_client", gateway)
        self.assertIn("upstream_headers", gateway)
        self.assertIn("x-prefill-status", gateway)
        self.assertIn("x-prefill-ms", gateway)
        self.assertIn("StreamingResponse", gateway)
        self.assertIn("JSONResponse", gateway)

    def test_gateway_image_build_supports_restricted_networks(self) -> None:
        dockerfile = read(GATEWAY_DOCKERFILE)
        gateway_compose = service_block(compose_text(), "gateway")
        requirements = read(GATEWAY_REQUIREMENTS)

        self.assertIn("fastapi>=0.110,<1", requirements)
        self.assertIn("httpx>=0.27,<1", requirements)
        self.assertIn("uvicorn[standard]>=0.27,<1", requirements)

        self.assertIn("ARG PIP_INDEX_URL", dockerfile)
        self.assertIn("ARG PIP_EXTRA_INDEX_URL", dockerfile)
        self.assertIn("ARG PIP_TRUSTED_HOST", dockerfile)
        self.assertIn("ARG PIP_DEFAULT_TIMEOUT", dockerfile)
        self.assertIn("ARG PIP_RETRIES", dockerfile)
        self.assertIn("--index-url", dockerfile)
        self.assertIn("--extra-index-url", dockerfile)
        self.assertIn("--trusted-host", dockerfile)
        self.assertIn("--timeout", dockerfile)
        self.assertIn("--retries", dockerfile)
        self.assertIn("python -m pip install", dockerfile)

        self.assertIn("PIP_INDEX_URL: ${PIP_INDEX_URL:-https://pypi.org/simple}", gateway_compose)
        self.assertIn("PIP_EXTRA_INDEX_URL: ${PIP_EXTRA_INDEX_URL:-}", gateway_compose)
        self.assertIn("PIP_TRUSTED_HOST: ${PIP_TRUSTED_HOST:-}", gateway_compose)
        self.assertIn("PIP_DEFAULT_TIMEOUT: ${PIP_DEFAULT_TIMEOUT:-120}", gateway_compose)
        self.assertIn("PIP_RETRIES: ${PIP_RETRIES:-10}", gateway_compose)

    def test_final_design_doc_exists_and_states_mvp_contract(self) -> None:
        doc = read(DESIGN_DOC)

        self.assertIn("# DeepSeek vLLM 多节点 PD 分离 MVP 定稿设计方案", doc)
        self.assertIn("MVP 结论", doc)
        self.assertIn("TP=8", doc)
        self.assertIn("TP=4", doc)
        self.assertIn("KV Cache", doc)
        self.assertIn("Prefill", doc)
        self.assertIn("Decode", doc)
        self.assertIn("验证指标", doc)
        self.assertIn("5 卡 Prefill / 3 卡 Decode", doc)
        self.assertIn("docker-compose.lmcache.yml", doc)
        self.assertIn("ops/pd-stack.sh", doc)

    def test_ops_runbook_exists(self) -> None:
        doc = read(OPS_RUNBOOK)

        self.assertIn("# DeepSeek PD MVP 运维手册", doc)
        self.assertIn("docker-compose.lmcache.yml", doc)
        self.assertIn("ops/pd-stack.sh", doc)
        self.assertIn("ops/pd-stack.ps1", doc)
        self.assertIn("restart decode", doc)
        self.assertIn("verify", doc)

    def test_third_party_challenge_response_doc_exists(self) -> None:
        doc = read(THIRD_PARTY_RESPONSE)

        self.assertIn("# 三方质疑正面回复", doc)
        self.assertIn("容器内 GPU 编号", doc)
        self.assertIn("CUDA_VISIBLE_DEVICES=0,1,2,3", doc)
        self.assertIn("NCCL_SHM_DISABLE=0", doc)
        self.assertIn("memlock", doc)
        self.assertIn("--enable-chunked-prefill", doc)
        self.assertIn("不采纳", doc)
        self.assertIn("LMCache 端口", doc)

    def test_audit_response_doc_exists(self) -> None:
        doc = read(AUDIT_RESPONSE)

        self.assertIn("# 三方审计报告响应与优化方案", doc)
        self.assertIn("正面回复", doc)
        self.assertIn("采纳", doc)
        self.assertIn("不直接采纳", doc)
        self.assertIn("--kv-transfer-config", doc)
        self.assertIn("LMCacheMPConnector", doc)
        self.assertIn("优化路线", doc)


if __name__ == "__main__":
    unittest.main()
