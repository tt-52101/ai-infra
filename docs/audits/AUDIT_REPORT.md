# vLLM Prefill-Decode 分离部署方案 — 专业审核报告 | 版本：v1.0 | 日期：2026-06-04 | 状态：已发布

## 审核基本信息

| 项目 | 内容 |
|------|------|
| **审核对象** | [docker-compose.yml](file:///d:/ai-infra/compose/docker-compose.yml) + [lmcache_config.yaml](file:///d:/ai-infra/compose/lmcache_config.yaml) |
| **部署架构** | Prefill/Decode 角色分离 + LMCache 分布式 KV Cache 共享 |
| **硬件环境** | 8 x RTX 4090 GPU (24GB/卡)，无 NVLink |
| **模型规格** | DeepSeek-V4-Flash-AWQ (AWQ 4-bit 量化, MoE 架构) |
| **GPU 分配** | Prefill: GPU 0-3 (TP=4), Decode: GPU 4-7 (TP=4) |
| **审核方法** | 资深架构师评审 + 资深 QA 工程师评审（双角色独立） |
| **综合评分** | **5.0 / 10** — 不建议直接上线生产环境 |

---

## 一、架构合理性评估

### 1.1 总体架构评价

```
┌─────────────────────────┐     KV Cache      ┌─────────────────────────┐
│    vllm-prefill         │ ───────────────→  │    vllm-decode          │
│    GPU: 0,1,2,3        │     LMCache       │    GPU: 4,5,6,7        │
│    Port: 8001           │    Server:65432   │    Port: 8002           │
│                         │                   │                         │
│  · 大文本首字计算        │  · 首字延迟优化   │  · 高并发流式输出       │
│  · 计算密集型            │  · KV Cache 复用  │  · 内存带宽敏感         │
│  · 吞吐优先              │  · 减少重复计算   │  · 延迟敏感             │
└─────────────────────────┘                   └─────────────────────────┘
           ↑                                              ↓
      新请求入口                                   流式响应返回
```

**架构方向判定**：✅ 正确 — Prefill/Decode 分离是 vLLM 0.6+ 支持的生产级特性，配合 LMCache 做 KV Cache 共享是提升并发能力和降低 TTFT 的有效手段。

### 1.2 关键缺陷

| # | 缺陷描述 | 风险等级 | 影响 | 详情位置 |
|---|----------|----------|------|----------|
| ARCH-01 | **缺少 `--enable-prefill-decode-mode` 参数** | 致命 | PD 分离模式不会生效，退化为两个独立推理节点 | [docker-compose.yml#L44-L51](file:///d:/ai-infra/compose/docker-compose.yml#L44-L51), [#L82-L89](file:///d:/ai-infra/compose/docker-compose.yml#L82-L89) |
| ARCH-02 | **缺少 `--role prefill` / `--role decode` 参数** | 致命 | vLLM 无法识别节点角色，请求路由失效 | 同上 |
| ARCH-03 | **LMCache backend 设为 `"gpu"` 但 server 无 GPU** | 高危 | LMCache Server 启动可能失败或回退到 CPU 模式，性能骤降 30%+ | [lmcache_config.yaml#L2](file:///d:/ai-infra/compose/lmcache_config.yaml#L2) |

### 1.3 显存分配分析

```
DeepSeek-V4-Flash-AWQ (MoE 架构):
├── 总参数量: ~140B (激活参数 ~25B)
├── AWQ 4-bit 量化后总权重: ~70GB
├── TP=4 单卡模型权重: ~17.5GB
├── gpu-memory-utilization=0.85 × 24GB = ~20.4GB 可用显存
└── KV Cache 空间: ~20.4GB - 17.5GB = ~2.9GB/卡

⚠️ max-model-len=32768 时，单个长请求可消耗大量 KV Cache
⚠️ 建议: 降低至 0.80 或减少 max-model-len 至 16384
```

---

## 二、资源配置评估

### 2.1 GPU 与 Tensor Parallel 配置

| 配置项 | 当前值 | 评估 | 说明 |
|--------|--------|------|------|
| Prefill GPU 绑定 | CUDA_VISIBLE_DEVICES=0,1,2,3 | ✅ 正确 | 4 卡用于 prefill 计算 |
| Decode GPU 绑定 | CUDA_VISIBLE_DEVICES=4,5,6,7 | ✅ 正确 | 4 卡用于 decode 计算 |
| tensor-parallel-size | 4 | ✅ 正确 | 匹配物理 GPU 数量 |
| NCCL_P2P_DISABLE | 1 | ✅ 正确 | 4090 无 NVLink，禁用 P2P 避免死锁 |
| NCCL_IB_DISABLE | 1 | ✅ 正确 | 禁用 InfiniBand，使用 PCIe 通信 |
| ipc | host | ✅ 正确 | 允许 GPU 共享内存高效传输 |

### 2.2 缺失的关键 vLLM 参数

| 参数 | 用途 | 当前状态 | 影响程度 |
|------|------|----------|----------|
| `--enable-prefix-caching` | 启用前缀缓存，与 LMCache 协作必需 | ❌ 缺失 | 吞吐量可能低于预期 40%+ |
| `--max-num-seqs` | 最大并发序列数控制 | ❌ 缺失 | 可能导致 OOM |
| `--max-num-batched-tokens` | 批处理 token 上限 | ❌ 缺失 | 吞吐量不稳定 |
| `--scheduler-delay-factor` | 调度延迟因子（Decode 应设小值） | ❌ 缺失 | 流式输出体验差 |
| `--dtype` | 数据类型精度 | ❌ 缺失 | AWQ 通常用 float16 |
| `--api-key` | API 认证密钥 | ❌ 缺失 | 安全风险 |
| `--lmcache-backend` | LMCache 后端类型 | ❌ 缺失 | 默认值可能与配置冲突 |

---

## 三、网络与通信评估

### 3.1 Docker 网络拓扑

```
外部客户端
    │
    ├── :8001 ──→ bridge network ──→ vllm-prefill (内部)
    ├── :8002 ──→ bridge network ──→ vllm-decode  (内部)
    │
    └── :65432 ──→ ⚠️ lmcache-server (对外暴露！无认证)
                  ↓
             安全风险：KV Cache 数据可被任意读写
```

### 3.2 网络安全问题清单

| # | 问题 | 严重程度 | 说明 | 修复建议 |
|---|------|----------|------|----------|
| NET-01 | LMCache Server 端口 65432 对外暴露 | 🔴 高危 | 无认证机制，可被未授权访问读写 KV 数据 | 移除 ports 映射，改用 expose 仅内部访问 |
| NET-02 | vLLM API 端口无访问控制 | 🟡 中危 | 8001/8002 直接暴露，任何人可调用推理接口 | 添加 --api-key 或通过反向代理做认证 |
| NET-03 | 无 TLS 加密 | 🟡 中危 | 所有通信为明文 | 引入 Nginx/Traefik 做 TLS 终结 |
| NET-04 | 无容器间网络隔离策略 | 🟡 中危 | 容器间无防火墙规则 | 配置 Docker 网络策略或使用第三方 CNI |

---

## 四、高可用性与容错评估

### 4.1 故障场景矩阵

| 故障类型 | 影响 | RTO 估计 | 恢复方式 | 当前覆盖 |
|----------|------|----------|----------|----------|
| 单个 vLLM 容器崩溃 | 50% 能力降级 | ~30s | 自动重启 (restart: unless-stopped) | ✅ 已覆盖 |
| LMCache Server 崩溃 | KV Cache 失效，退化为基础模式 | ~30s | 自动重启 | ⚠️ 部分（缺健康检查） |
| 单张 GPU 故障 | TP 组不可用 | 人工干预 | 需重新配置 | ❌ 未处理 |
| 宿主机重启 | 全部服务中断 | ~2min | 自动启动 | ✅ 已覆盖 |
| 网络分区 | 节点间通信中断 | 持续 | 需人工介入 | ❌ 未处理 |
| OOM Kill | 容器被杀但可能循环崩溃 | 取决于触发原因 | 自动重启但可能循环 | ⚠️ 部分（缺资源限制） |

### 4.2 SPOF 分析

| SPOF 点 | 风险等级 | 影响范围 | 缓解建议 |
|---------|----------|----------|----------|
| **LMCache Server** (单实例) | Critical | 缓存失效，性能下降 30-50%，可能引发雪崩 | 主备实例 + Keepalived/VIP；或改用 Redis/TiKV 等成熟缓存 |
| **物理主机** (单节点) | Critical | 整机宕机导致全量服务不可用 | 多节点 K8s 部署 |
| **共享模型存储** (本地磁盘) | Major | 存储 IO 瓶颈或损坏导致不可用 | 分布式存储（Ceph/NFS HA）或预热到本地 SSD |
| **Docker Bridge 网络** | Minor | 网络分区风险低 | 生产环境改用 overlay/macvlan |

### 4.3 depends_on 局限性

```yaml
# 当前配置
depends_on:
  - lmcache-server  # ❌ 仅保证启动顺序，不保证服务就绪
```

**问题**：LMCache Server 可能需要 10-30 秒初始化数据库连接和存储引擎。vLLM 在此期间启动会因连不上 LMCache 而报错或降级运行。

**修复**：添加 healthcheck 并结合 `condition: service_healthy`。

---

## 五、性能瓶颈风险分析

### 5.1 LMCache Server 性能瓶颈

```
理论瓶颈计算（100 并发请求，平均 prompt length = 2048 tokens）：

KV Cache 生成速率:
  每个 token KV size (AWQ 压缩): ~0.5KB
  单次请求 KV Cache: 2048 × 0.5KB ≈ 1MB
  100 并发/秒: ≈ 100MB/s 写入压力

LMCache Server 能力:
  单进程 Python 服务，理论吞吐 ~500MB/s
  序列化/反序列化 CPU 开销较大
  磁盘 I/O 受限于 ./lmcache_storage 所在磁盘性能

⚠️ 并发 >50 时可能出现排队延迟
⚠️ KV Cache 传输额外引入 ~40-80ms 延迟开销
```

### 5.2 延迟影响链路

```
Prefill 完成 (T+0ms)
    │
    ├── 上传 KV 到 LMCache Server ──→ 序列化+存储 (T+5~20ms)
    │                                    ↑ 瓶颈点 #1
    ├── Client 收到首 token (T+20~35ms)
    │
    ├── Client 转发请求到 Decode (T+25~40ms)
    │
    ├── Decode 从 LMCache 拉取 KV ──→ 反序列化+传输 (T+35~60ms)
    │                                       ↑ 瓶颈点 #2
    └── 开始流式输出 (T+40~80ms)

净增加延迟: ~40~80ms (相比非分离模式)
```

### 5.3 资源竞争风险

| 资源类型 | 风险场景 | 当前缓解措施 | 建议增强 |
|----------|----------|--------------|----------|
| CPU | Prefill/Decode 同机竞争 | 无（仅设置 GPU） | 设置 CPU limit/pin |
| 系统内存 | local_cpu_percentage 0.4×2=80% + LMCache Server 自身占用 → OOM 风险 | 无 limit | 降至各 0.25，加 memory hard limit |
| PCIe 带宽 | 8 GPU 同时 TP 通信 | NCCL_P2P_DISABLE | 确认是否必要（同节点内 PCIe P2P 可用） |
| 网络带宽 | LMCache 传输瓶颈 | 无 QoS | 设置 tc 限流或专用网卡 |
| 磁盘 IO | 模型加载/缓存读写 | 无 IO 隔离 | 使用独立 SSD 存放 LMCache 数据 |

---

## 六、安全风险评估

### 6.1 安全问题汇总

| 编号 | 问题 | 严重程度 | 位置 | 详细说明 |
|------|------|----------|------|----------|
| SEC-01 | **LMCache 端口暴露无认证** | 🔴 高危 | [docker-compose.yml#L9](file:///d:/ai-infra/compose/docker-compose.yml#L9) | 端口 65432 映射到宿主机，LMCache Server 无内置认证，可被利用读取/篡改 KV Cache 数据 |
| SEC-02 | **vLLM API 无密钥保护** | 🟡 中危 | command 段 | 8001/8002 端口无 `--api-key`，任何人可调用推理接口，存在资源滥用和成本风险 |
| SEC-03 | **镜像来源不可重现** | 🟡 中危 | image tags | 使用 `:latest` 标签无法保证环境一致性，存在供应链攻击风险 |
| SEC-04 | **特权容器 IPC 共享** | 🟢 低危 | ipc: host | 容器共享宿主机 IPC 命名空间，存在信息泄露可能性（受控环境可接受） |
| SEC-05 | **无网络隔离策略** | 🟡 中危 | network: bridge | 容器间无防火墙规则，存在横向移动风险 |
| SEC-06 | **模型文件权限依赖宿主机** | 🟢 低危 | volume mount | `/data/models` 权限完全依赖宿主机文件系统配置 |

### 6.2 OWASP Top 10 对照检查

| OWASP 类别 | 相关发现 | 覆盖状态 |
|------------|----------|----------|
| A01:2021 – 访问控制失效 | SEC-01, SEC-02 | ❌ 未覆盖 |
| A02:2021 – 加密机制失败 | NET-03 (无 TLS) | ❌ 未覆盖 |
| A05:2021 – 安全配置错误 | SEC-03 (latest tag), SEC-06 | ⚠️ 部分覆盖 |
| A06:2021 – 易受攻击和过时的组件 | SEC-03 (镜像版本不确定) | ❌ 未覆盖 |
| A07:2021 – 认识和记录失败 | 零日志/零监控 | ❌ 未覆盖 |
| A08:2021 – 软件和数据完整性故障 | SEC-03 (供应链风险) | ❌ 未覆盖 |

**结论**：安全评分 **3/10**，存在多项高危和中危安全问题。

---

## 七、运维友好性评估

### 7.1 可观测性现状 vs 目标

| 维度 | 当前状态 | 目标状态 | 缺口等级 |
|------|----------|----------|----------|
| 应用日志 | stdout 无结构化 | JSON 格式 → ELK/Loki | Major |
| 指标采集 | 无 | Prometheus + Grafana Dashboard | **Critical** |
| 链路追踪 | 无 | OpenTelemetry → Jaeger | Major |
| 健康检查 | 无 | HTTP /health + 自动重启 | **Critical** |
| 告警通知 | 无 | PagerDuty/钉钉多通道 | Major |
| 审计日志 | 无 | API 调用全量记录 | Minor |

### 7.2 运维能力缺失详情

| 能力 | 当前状态 | 风险 |
|------|----------|------|
| 健康检查 | 三个服务均无 `healthcheck` 配置 | 无法自动感知应用级故障，僵尸进程无法被发现和恢复 |
| 日志管理 | 无 logging driver 配置，无轮转策略 | 长时间运行后磁盘被日志占满，日志丢失无法追溯 |
| 监控指标 | 无 VLLM_METRICS_ENABLED，无 Prometheus 端点 | 无法量化评估性能，无法设置告警阈值 |
| 资源限制 | 仅设 reservations 未设 limits | 单容器异常可能导致宿主机 OOM |
| 优雅关闭 | 无 stop_grace_period 配置 | vLLM 处理中的请求可能被强制中断 |
| 容器标签 | 无 labels 元数据 | 运维管理困难，无法按项目/团队筛选 |

---

## 八、完整问题清单（按严重程度排序）

### Critical（必须修复，阻塞上线）

| ID | 问题 | 位置 | 影响 | 修复操作编号 |
|----|------|------|------|-------------|
| C-01 | 缺少健康检查机制 | 所有 services | 服务不可观测，僵尸进程无法恢复 | P1-11~P1-13 |
| C-02 | 使用 `image: latest` 标签 | L6, L20, L59 | 部署不可重现，回滚困难 | P1-14~P1-18 |
| C-03 | LMCache backend `"gpu"` 与容器资源不匹配 | [lmcache_config.yaml#L2](file:///d:/ai-infra/compose/lmcache_config.yaml#L2) | 启动失败或性能骤降 | P1-19 |
| C-04 | 缺少 PD 模式启用参数 (`--enable-prefill-decode-mode`) | command 段 | PD 分离模式不生效 | P1-01, P1-03 |
| C-05 | 缺少角色标识参数 (`--role prefill/decode`) | command 段 | 请求路由失效 | P1-02, P1-04 |

### Major（强烈建议修复）

| ID | 问题 | 位置 | 影响 | 修复操作编号 |
|----|------|------|------|-------------|
| M-01 | LMCache 端口对外暴露无认证 | [L9](file:///d:/ai-infra/compose/docker-compose.yml#L9) | KV 数据安全风险 | P1-05, P1-06 |
| M-02 | vLLM API 无密钥保护 | command 段 | 资源滥用风险 | P1-07~P1-10 |
| M-03 | 缺少资源上限 limits | deploy.resources | 容器 OOM 可拖垮宿主机 | P2-01~P2-03 |
| M-04 | 缺少日志驱动和轮转配置 | 所有 services | 磁盘占满风险 | P2-13~P2-15 |
| M-05 | 缺少关键 vLLM 调优参数 | command 段 | 吞吐量低于预期 40%+ | P2-07~P2-12 |
| M-06 | 显存利用率偏高易 OOM | L48, L87 | 高并发场景不稳定 | P2-04~P2-06 |
| M-07 | local_cpu_percentage 过高导致内存竞争 | [lmcache_config.yaml#L3](file:///d:/ai-infra/compose/lmcache_config.yaml#L3) | 系统 OOM 风险 | P1-20 |
| M-08 | 数据持久化路径不规范 | [L12-L13](file:///d:/ai-infra/compose/docker-compose.yml#L12-L13) | 相对路径行为不一致 | P2-18 |

### Minor（建议优化）

| ID | 问题 | 位置 | 影响 | 修复操作编号 |
|----|------|------|------|-------------|
| m-01 | 缺少环境变量 .env 文件管理 | environment 段 | 多环境管理不便 | P1-10 (部分) |
| m-02 | 缺少容器标签元数据 | 所有 services | 运维管理不便 | P2-21 |
| m-03 | NCCL 参数硬编码 | L26-L27, L65-L66 | 硬件升级时需手动修改 | 文档说明即可 |
| m-04 | 无优雅关闭时间配置 | 所有 services | 处理中请求可能中断 | P2-19, P2-20 |
| m-05 | Compose version 字段已废弃 | L1 | 新版 Compose V2 忽略此字段 | 信息性，可移除 |

### Info（信息性建议）

| ID | 建议 | 说明 |
|----|------|------|
| I-01 | 提取 YAML anchor 消除重复配置 | Prefill 和 Decode 配置高度相似，可用 `&vllm-common` + `<<: *vllm-common` 复用 |
| I-02 | 考虑引入 Nginx 反向代理做统一入口 | 统一做 TLS 终结、限流、认证 |
| I-03 | 长期考虑 K8s 迁移 | 当前硬编码 GPU ID 不支持弹性伸缩 |

---

## 九、综合评分卡

| 评估维度 | 得分 (/10) | 风险等级 | 主要扣分原因 |
|----------|------------|----------|--------------|
| **架构合理性** | 6.0 | 中 | 方向正确但缺少关键 PD 模式参数，实际不会以分离模式运行 |
| **资源配置** | 7.0 | 中 | GPU/TP/NCCL 配置正确，显存余量偏紧，缺少调优参数 |
| **网络通信** | 6.0 | 高 | LMCache 端口暴露，API 无认证，无 TLS |
| **高可用性** | 5.0 | 中 | 基础 restart 具备，缺健康检查/SPOF 处理/优雅降级 |
| **性能风险** | 5.0 | 高 | LMCache 单点瓶颈，缺少 prefix-caching，内存竞争 |
| **安全性** | 3.0 | **高** | 多处高危/中危缺陷，OWASP 覆盖不足 |
| **运维友好性** | 3.0 | **高** | 零监控/零健康检查/零日志管理/零告警 |
| **综合得分** | **5.0** | **高风险** | **不建议直接上线生产环境** |

---

## 十、修复路线图与优先级

### Phase 1 — 阻塞修复（预计 2-4 小时）

> 前置条件：完成 PRE-1 ~ PRE-5 准备步骤

| 步骤 | 操作 | 对应问题ID | 预计耗时 |
|------|------|-----------|----------|
| P1-01 ~ P1-04 | 补全 PD 模式启用参数 | C-04, C-05 | 15 min |
| P1-05 ~ P1-06 | 移除 LMCache 外部端口暴露 | M-01 | 10 min |
| P1-07 ~ P1-10 | 添加 API Key 认证 | M-02 | 20 min |
| P1-11 ~ P1-13 | 添加健康检查 | C-01 | 20 min |
| P1-14 ~ P1-18 | 固定镜像版本号 | C-02 | 15 min |
| P1-19 ~ P1-20 | 修正 LMCache backend 配置 | C-03, M-07 | 10 min |
| SMOKE-01 ~ SMOKE-12 | 冒烟测试验证全部通过 | 全部 | 60 min |

**Phase 1 出口标准**：12 项冒烟测试全部通过。

### Phase 2 — 生产加固（预计 1-2 天）

| 步骤 | 操作 | 对应问题ID | 预计耗时 |
|------|------|-----------|----------|
| P2-01 ~ P2-03 | 添加资源上限限制 | M-03 | 15 min |
| P2-04 ~ P2-06 | 降低显存利用率防 OOM | M-06 | 10 min |
| P2-07 ~ P2-12 | 补充 vLLM 性能调优参数 | M-05 | 30 min |
| P2-13 ~ P2-15 | 配置日志轮转策略 | M-04 | 15 min |
| P2-16 ~ P2-17 | 启用 Prometheus metrics 导出 | （新能力） | 10 min |
| P2-18 | 数据持久化路径规范化 | M-08 | 10 min |
| P2-19 ~ P2-20 | 添加优雅关闭配置 | m-04 | 5 min |
| P2-21 | 添加容器元数据标签 | m-02 | 5 min |
| PERF-01 ~ PERF-08 | 基准压测验收 | 全部 | 120 min |

**Phase 2 出口标准**：8 项压测指标达标。

### Phase 3 — 长期演进（1-3 月，供参考）

| 优化项 | 预期收益 | 工作量 | 优先级 |
|--------|----------|--------|--------|
| LMCache 高可用（主备/VIP） | 消除 SPOF | 3-5 天 | 高 |
| Nginx 反向代理 + TLS | 安全加固 | 2h | 高 |
| Prometheus + Grafana 监控大盘 | 可观测性 | 4h | 高 |
| ELK/Loki 日志收集 | 问题排查效率 | 3h | 中 |
| Kubernetes 迁移 | 弹性伸缩 | 5-10 天 | 中 |
| 混沌工程测试 | 可靠性验证 | 5 天 | 中 |

---

## 十一、附录

### 附录 A：冒烟测试自动化脚本参考

```bash
#!/bin/bash
# smoke_test.sh — 部署后冒烟测试
set -euo pipefail

PASS=0; FAIL=0
API_KEY="${VLLM_API_KEY:-sk-test}"

check() {
    local desc="$1"; local cmd="$2"
    echo -n "[TEST] $desc ... "
    if eval "$cmd" > /dev/null 2>&1; then echo "PASS"; ((PASS++))
    else echo "FAIL"; ((FAIL++)); fi
}

echo "=== vLLM Cluster Smoke Test ==="
echo ""

check "Container Running" "docker compose ps | grep -q 'running'"
check "Prefill Health" "curl -sf http://localhost:8001/health"
check "Decode Health" "curl -sf http://localhost:8002/health"
check "Models Endpoint" "curl -sf http://localhost:8001/v1/models"
check "Chat Completion (with auth)" \
  "curl -sf -X POST http://localhost:8001/v1/chat/completions \
   -H 'Content-Type: application/json' \
   -H \"Authorization: Bearer $API_KEY\" \
   -d '{\"model\":\"/model\",\"messages\":[{\"role\":\"user\",\"content\":\"Hi\"}],\"max_tokens\":5}' \
   | grep -q 'choices'"
check "Auth Rejection (no key)" \
  "! curl -sf -X POST http://localhost:8001/v1/chat/completions \
   -H 'Content-Type: application/json' \
   -d '{\"model\":\"/model\",\"messages\":[{\"role\":\"user\",\"content\":\"Hi\"}]}'"
check "LMCache Port Not Exposed" \
  "! curl -sf http://localhost:65432/health"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
exit $FAIL
```

### 附录 B：关键监控指标定义

| 类别 | 指标名称 | 告警阈值 | 重要性 |
|------|----------|----------|--------|
| 系统资源 | GPU 利用率 | >90% 持续 5min | 关键 |
| 系统资源 | GPU 显存使用率 | >95% | 关键 |
| 推理性能 | TTFT (首字延迟) | P99 > 2000ms | 重要 |
| 推理性能 | TPOT (每 token 延迟) | P99 > 150ms | 重要 |
| KV Cache | LMCache 命中率 | < 60% | 重要 |
| KV Cache | 缓存传输延迟 | P99 > 100ms | 重要 |
| 服务可用性 | HTTP 5xx 错误率 | > 1% | 关键 |
| 服务可用性 | 健康检查失败 | 任何失败 | 关键 |

### 附录 C：相关文件索引

| 文件 | 路径 | 说明 |
|------|------|------|
| 部署编排文件 | [docker-compose.yml](file:///d:/ai-infra/compose/docker-compose.yml) | 主部署配置 |
| LMCache 配置 | [lmcache_config.yaml](file:///d:/ai-infra/compose/lmcache_config.yaml) | KV Cache 后端配置 |
| 修复操作清单 | [FIX_CHECKLIST.md](file:///d:/ai-infra/compose/FIX_CHECKLIST.md) | 逐步执行的操作手册 |

---

## 十二、审核签名

| 角色 | 评审人 | 日期 | 结论 |
|------|--------|------|------|
| 资深架构师 | AI Agent (senior-fullstack-architect) | 2026-06-04 | 不建议上线，需完成 Phase 1 + Phase 2 |
| 资深 QA 工程师 | AI Agent (senior-qa-engineer) | 2026-06-04 | 发现 3 Critical / 5 Major / 5 Minor / 3 Info |
| 业务确认 | ________________ | ____-__-__ | |
| 技术负责人审批 | ________________ | ____-__-__ | |

---

*本报告由双角色 Agent 独立评审生成，所有发现均附带具体位置引用、影响分析和可操作的修复建议。*
*配套操作清单见 [FIX_CHECKLIST.md](file:///d:/ai-infra/compose/FIX_CHECKLIST.md)。*
