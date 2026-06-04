import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "compose" / "docker-compose.yml"
LMCACHE_CONFIG = ROOT / "compose" / "lmcache_config.yaml"
GATEWAY = ROOT / "backend" / "gateway.py"
DESIGN_DOC = ROOT / "docs" / "mvp-pd-separation-design.md"


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def service_block(compose_text: str, service_name: str) -> str:
    pattern = rf"(?ms)^  {re.escape(service_name)}:\n(.*?)(?=^  [a-zA-Z0-9_-]+:\n|^networks:|\Z)"
    match = re.search(pattern, compose_text)
    if not match:
        raise AssertionError(f"service {service_name!r} not found")
    return match.group(1)


class MvpPdAssetsTest(unittest.TestCase):
    def test_compose_declares_pd_topology_and_gateway(self) -> None:
        compose = read(COMPOSE)

        self.assertIn("lmcache-server:", compose)
        self.assertIn("vllm-prefill:", compose)
        self.assertIn("vllm-decode:", compose)
        self.assertIn("gateway:", compose)

        prefill = service_block(compose, "vllm-prefill")
        decode = service_block(compose, "vllm-decode")
        gateway = service_block(compose, "gateway")

        self.assertIn("CUDA_VISIBLE_DEVICES=0,1,2,3", prefill)
        self.assertIn("ids: ['0', '1', '2', '3']", prefill)
        self.assertIn("--tensor-parallel-size 4", prefill)
        self.assertIn("--port 8001", prefill)

        self.assertIn("CUDA_VISIBLE_DEVICES=4,5,6,7", decode)
        self.assertIn("ids: ['4', '5', '6', '7']", decode)
        self.assertIn("--tensor-parallel-size 4", decode)
        self.assertIn("--port 8002", decode)

        for block in (prefill, decode):
            self.assertIn("NCCL_P2P_DISABLE=1", block)
            self.assertIn("NCCL_IB_DISABLE=1", block)
            self.assertIn("LMCACHE_ENABLE=True", block)
            self.assertIn("LMCACHE_SERVER_ADDR=lmcache-server", block)
            self.assertIn("LMCACHE_CONFIG_FILE=/vllm-workspace/lmcache_config.yaml", block)

        self.assertIn('"8000:8000"', gateway)
        self.assertIn("PREFILL_NODE_URL=http://vllm-prefill:8001/v1/chat/completions", gateway)
        self.assertIn("DECODE_NODE_URL=http://vllm-decode:8002/v1/chat/completions", gateway)

    def test_lmcache_shared_backend_contract(self) -> None:
        config = read(LMCACHE_CONFIG)

        self.assertIn("chunk_size: 256", config)
        self.assertIn('backend: "gpu"', config)
        self.assertIn("local_cpu_percentage: 0.4", config)
        self.assertIn('remote_url: "lmcache-server://lmcache-server:65432"', config)

    def test_gateway_is_container_aware_and_observable(self) -> None:
        gateway = read(GATEWAY)

        self.assertIn('os.getenv("PREFILL_NODE_URL"', gateway)
        self.assertIn('os.getenv("DECODE_NODE_URL"', gateway)
        self.assertIn('@app.get("/healthz")', gateway)
        self.assertIn("x-prefill-status", gateway)
        self.assertIn("x-prefill-ms", gateway)
        self.assertIn("StreamingResponse", gateway)
        self.assertIn("JSONResponse", gateway)

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


if __name__ == "__main__":
    unittest.main()
