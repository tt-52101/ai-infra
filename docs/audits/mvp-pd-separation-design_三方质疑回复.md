# 三方质疑正面回复

本文针对 `docs/audits/mvp-pd-separation-design_三方评审.md` 中的质疑逐项回应，并记录已经采纳的实现调整。

## 1. 总体回应

这次质疑的核心方向成立：当前方案虽然适合作为 MVP 验证基线，但如果按工业级高并发生产环境评估，仍需要继续收敛启动可靠性、NCCL 通信稳定性、缓存配置一致性和运行时观测。

本次处理采取三个原则：

- 对会导致启动失败或死锁的风险直接采纳并修正。
- 对能增强 MVP 稳定性的调优项先纳入配置。
- 对会扩大安全暴露面、与当前 LMCache MP connector 方案冲突、或需要目标机验证的建议，不直接采纳，但保留验证路线。

## 2. 逐项回应

| 质疑点 | 回复 | 当前处理 |
| --- | --- | --- |
| Decode 同时使用宿主机 GPU `device_ids: ['4','5','6','7']` 和 `CUDA_VISIBLE_DEVICES=4,5,6,7` 可能导致容器内编号冲突 | 采纳。物理 GPU 由 Docker/NVIDIA runtime 负责隔离后，容器内应使用可见设备的本地编号。 | Decode 已改为 `CUDA_VISIBLE_DEVICES=0,1,2,3`，同时保留 `device_ids: ['4','5','6','7']` 做宿主机物理卡绑定。 |
| Prefill/Decode 不是两个普通 vLLM 节点即可天然完成原生 PD | 采纳其风险判断。当前设计已经通过 `--kv-transfer-config` 声明 `kv_producer` / `kv_consumer`，不是只靠两个普通节点。 | 保留 `LMCacheMPConnector`，并在文档中强调这是 MVP 级 PD 验证，不等同于完整生产 Router。 |
| Prefill 节点应使用 Chunked Prefill 抑制长上下文峰值 | 采纳。长上下文场景下，Prefill 节点更适合先启用分块来降低峰值压力。 | Prefill 增加 `--enable-chunked-prefill`。 |
| `depends_on` 只保证启动顺序，不保证就绪 | 已采纳。当前 Compose 已使用 `condition: service_healthy`。 | 维持四个服务的 healthcheck 与健康依赖。 |
| 4090 禁用 P2P 后需启用 SHM 回退，并解除 memlock | 采纳。无 NVLink 的 4090 环境需要更明确地允许 SHM 路径。 | Prefill/Decode 增加 `NCCL_SHM_DISABLE=0`、`ulimits.memlock=-1`、`nofile=65536`。 |
| LMCache 配置必须与挂载卷和后端一致 | 采纳。当前配置只保留 MP connector 方案需要的本地 CPU 缓存字段。 | `lmcache_config.yaml` 保持 `chunk_size: 256`、`local_cpu: true`、`max_local_cpu_size: 5`。 |
| 需要前端 Router/API 代理 | 已实现 MVP 版本。 | `backend/gateway.py` 已作为统一 OpenAI 兼容入口，负责 Prefill -> Decode 编排、认证和观测头。 |

## 3. 不采纳或不照搬的建议

### 3.1 不重新暴露 LMCache 端口

评审样例把 `lmcache-server` 暴露为宿主机端口 `65432:65432`。这个写法便于调试，但与前序安全审计结论冲突。

当前不采纳该写法。Compose 已按 LMCache 官方 Docker 示例采用 `network_mode: host`，但 LMCache MP 与 HTTP 管理面绑定 loopback，外部唯一业务入口仍是 gateway `8000`。原因：

- LMCache 端口不是业务 API，不应暴露给外部客户端。
- KV Cache 可能承载 prompt 上下文派生数据，应减少未授权访问面。
- 调试可以通过 `docker exec` 或内部网络探测完成，不需要宿主机端口映射。

### 3.2 不把 `remote_url: "lmcache://lmcache-server:65432"` 写回当前配置

当前实现采用 `LMCacheMPConnector` 和 `--kv-transfer-config`。在这个方案下，vLLM 通过 connector extra config 指定 `lmcache.mp.port=${LMCACHE_MP_PORT:-6555}`，依赖官方 Docker 示例的 host 网络模型访问本机 LMCache Standalone，而不是通过旧式 `remote_url` 字段表达远程缓存。

因此不采纳评审样例中的 `remote_url` 配置，避免同一个 MVP 同时存在两套 LMCache 接入模型。

### 3.3 不直接固定到评审样例中的镜像版本

评审早期建议过 `lmcache/lmcache-server` 方向，但目标环境已经验证 Docker Hub 不存在该镜像发布。当前实现改为 `lmcache/standalone`，vLLM 侧使用 `lmcache/vllm-openai`，固定版本方向仍然正确，但具体 tag 或 digest 必须在目标 GPU 服务器上验证：

- 是否包含 `LMCacheMPConnector`。
- 是否支持当前 `--kv-transfer-config` 字段。
- 是否支持目标 DeepSeek AWQ 权重。
- 是否与当前 LMCache 配置兼容。

所以当前仍保留 `.env` 参数化镜像，目标机验证成功后再把 tag 或 digest 固化。

## 4. 已完成的实现调整

### 4.1 容器内 GPU 编号修正

Decode 节点当前配置：

```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          device_ids: ['4', '5', '6', '7']
          capabilities: [gpu]
environment:
  - CUDA_VISIBLE_DEVICES=0,1,2,3
```

含义：

- `ids` 锁定宿主机物理 GPU 4-7。
- 容器内 CUDA 只看见被映射进来的 4 张卡，并按本地可见设备使用 `0,1,2,3`。

### 4.2 NCCL 和 ulimit 加固

Prefill/Decode 均增加：

```yaml
environment:
  - NCCL_SHM_DISABLE=${NCCL_SHM_DISABLE:-0}
ulimits:
  memlock: -1
  nofile:
    soft: 65536
    hard: 65536
```

这对应 4090 无 NVLink、禁用 P2P 后必须确保共享内存路径可用的要求。

### 4.3 Prefill 分块

Prefill 节点增加：

```text
--enable-chunked-prefill
```

该参数用于降低长上下文 Prefill 峰值压力。Decode 节点不强制开启该参数，避免把 Decode 侧目标从低延迟流式输出变成长上下文吞吐处理。

## 5. 后续验证要求

这次修正仍然需要在目标 8 卡 4090 服务器上完成运行时验证：

1. `docker compose config` 通过。
2. `docker compose up -d --build` 能启动四个服务。
3. `vllm-prefill` 只绑定宿主机 GPU 0-3。
4. `vllm-decode` 只绑定宿主机 GPU 4-7，但容器内 CUDA 编号为 0-3。
5. 无认证访问 gateway 推理接口返回 401。
6. `8001`、`8002`、`6555` 只绑定 loopback，不作为外部业务入口。
7. 长上下文重复请求 TTFT 有下降。
8. Prefill 高负载期间 Decode 流式输出没有明显长停顿。

## 6. 当前结论

本次质疑中最重要的启动风险和 NCCL 风险已经采纳并反映到实现中。当前方案仍坚持“安全收敛的 MVP 验证基线”：只暴露 gateway，内部节点通过 host 网络的 loopback 地址通信，PD 角色通过 `LMCacheMPConnector` 和 `--kv-transfer-config` 声明。

生产化前仍必须完成目标机实测，并基于实际可用的 vLLM / LMCache 镜像版本固定 tag 或 digest。

参考依据：

- [NVIDIA Container Toolkit Docker Specialized Configurations](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/docker-specialized.html)
- [vLLM Disaggregated Prefilling](https://docs.vllm.ai/en/stable/features/disagg_prefill.html)
- [LMCache Multiprocessing Configuration](https://docs.lmcache.ai/mp/configuration.html)
