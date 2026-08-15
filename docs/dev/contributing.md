---
doc_type: dev-guide
slug: contributing
component: repository
status: draft
summary: 面向公开仓库贡献者的本地环境、模块边界、测试和提交指南。
tags: [development, contributing, testing, mqtt, simulation, replay]
last_reviewed: 2026-08-17
---

# 贡献者开发指南

## 概述

本指南面向准备修改 Python Agent、MQTT 闭环、连续仿真、Three.js 控制台或 C/FreeRTOS 参考层的贡献者。项目最重要的工程目标不是扩大模型权限，而是保持高层决策、协议传输、端侧安全和仿真真值之间的清晰边界。

## 前置依赖

- Python 3.12+；
- Node.js 20.19+ 或 22.12+；
- npm；
- Git；
- 用于 MQTT 集成测试的本机 Mosquitto 或兼容 Broker；
- 可选：Keil uVision/ArmClang，用于 Cortex-M4F 静态库编译证据。

## 快速上手

Windows PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
Push-Location web
npm ci
npm run build
Pop-Location
```

运行最小回归：

```powershell
.venv\Scripts\python.exe -m pytest -q
Push-Location web
npm test
npm run build
Pop-Location
```

## 核心概念

### 权威状态

Python Simulation Core 是连续位姿、逻辑时间、碰撞、三向距离、动作完成和任务结果的唯一权威源。Three.js 客户端可以做插值、镜头和材质效果，但不能计算或回写 `blocked`、传感器距离或最终位姿。

### 模型权限

模型只允许产生以下六个 Tool Call：

```text
move_robot
turn_robot
get_robot_state
scan_obstacles
emergency_stop
inspect_semantic_world
```

模型文本不得传给 `eval`、`exec`、Shell、编译器或脚本解释器。新增模型能力时必须经过 Tool Registry、严格 Schema、Safety 和测试，不要在 provider 或 UI 中建立旁路。

### MQTT 和幂等

QoS 1 是至少一次投递，不是恰好一次。Command 和 Observation 使用 `(task_id, seq)` 关联；重复相同 payload 应重放原结果，不同 payload 的相同 key 必须冲突拒绝，旧 seq 必须拒绝。

### 真实性边界

- FakePlanner 是确定性演示规划器，不是 Qwen/DeepSeek；
- `model_configured=true` 只说明启动配置可用；只有匹配源事件链的 Replay 才能标 `native_tool_calls_verified`；
- Mosquitto 是本机 Broker，不是 EMQX Dashboard；
- Python/C/Keil 仿真和编译证据不是 STM32 板级运行；
- simulation ground truth 不是实际传感器或视觉识别；
- 本机 RTT 不是实机链路时延。

## 主要模块

| 路径 | 责任 |
| --- | --- |
| `src/embodied_agent/schemas.py` | Command、Observation、Telemetry 和设备事件契约 |
| `src/embodied_agent/safety.py` | 云端/业务安全限制 |
| `src/embodied_agent/tool_registry.py` | 六个模型可见 Tool 及 Function Calling Schema；语义查询只读 |
| `src/embodied_agent/agent_graph.py` | 有界 Action-Observation Loop |
| `src/embodied_agent/mqtt_transport.py` | QoS、correlation、timeout 和连接失败 |
| `src/embodied_agent/mqtt_device_service.py` | 设备服务、Telemetry 和 Last Will |
| `src/embodied_agent/simulation/` | 世界、Engine、Adapter、任务/Fault 协调、Replay Recorder、Acceptance CLI 和 runtime |
| `web/src/` | 严格契约解析、Three.js 场景和 Mission/Fault/Replay Console |
| `firmware_sim/` | 平台无关 C 协议参考 |
| `firmware_runtime/` | UART/NDJSON/Queue/FreeRTOS 适配 |

## 常见开发场景

### 修改 Python 行为

先运行与模块相邻的测试，再运行全量测试：

```powershell
.venv\Scripts\python.exe -m pytest -q tests\test_safety.py tests\test_tool_registry.py
.venv\Scripts\python.exe -m pytest -q
```

涉及 MQTT 的行为应同时覆盖无 Broker 的明确失败和真实 Broker 下的 correlation/幂等。测试不得用直接函数调用冒充 MQTT 证据。

### 修改前端

```powershell
Push-Location web
npm test
npm run build
npm run dev
Pop-Location
```

开发服务器只用于前端调试。完整产品演示应由 `embodied_agent.cli simulation` 托管 API、WebSocket 和生产构建，避免前后端状态来源不一致。

### 修改 C/FreeRTOS 参考层

```powershell
.venv\Scripts\python.exe scripts\firmware_deps.py fetch
.venv\Scripts\python.exe -m pytest -q `
  tests\test_firmware_protocol_source.py `
  tests\test_firmware_runtime_source.py `
  tests\test_firmware_delivery.py
```

只有安装了 Keil/ArmClang 的环境才会运行对应交叉编译。不要提交 `.deps/`、`firmware_keil/Objects/` 或本机工具链日志。

GitHub Actions 的 `Firmware host and source checks` job 会在 Ubuntu 上重新获取并校验固定 cJSON/FreeRTOS 版本，然后实际编译运行平台无关 host C 场景，并检查 FreeRTOS Binding 与 Keil 静态库工程结构。该 job 不提供 Keil/ArmClang，也不会把 Linux host 编译写成 Cortex-M4 或 STM32 实机证据。

### 引入第三方代码或资产

在 PR 中记录来源 URL、固定版本或 commit、许可证、修改方式和分发义务，并同步更新 `THIRD_PARTY_NOTICES.md`。模型、mesh、纹理、字体和截图都属于需要核对许可证的资产。

## 已知限制与注意事项

- Python 依赖当前使用范围约束，没有 lockfile；制作可重现发行包前应增加锁定策略。首次源码公开由 Python、Web 与 Firmware 三个 CI job 验证安装和核心行为。
- Web production bundle 当前约 573 kB，Vite 会提示 chunk 超过 500 kB。
- 真实模型原生 Tool Calling、共享配置和 transcript 核验代码已实现，并已用一个本机配置的 OpenAI-compatible Provider 完成显式验收；公开仓库仍不包含凭据，默认测试和 Acceptance 不调用外网。
- Mission、Fault、Semantic World 与 Replay 已完成正式 acceptance。Replay 只保存最近一次完成运行并由用户下载，不提供服务端历史库、跨进程恢复、视频或物理重演。
- Replay Mode 必须保持只读，不得调用 Task/Fault/Operator mutation API、publish MQTT、执行 AgentGraph 或修改 SimulationEngine。
- GO1 视觉 mesh 已进入 `web/public/assets/go1/`，来源、固定 commit、BSD-3-Clause 和无背书说明位于该目录的 `NOTICE.md`。不要把 mesh 当作 Python 碰撞、传感器或任务真值来源。

## 相关文档

- [README](../../README.md)
- [本地仿真使用指南](../user/local-simulation.md)
- [贡献规范](../../CONTRIBUTING.md)
- [第三方依赖声明](../../THIRD_PARTY_NOTICES.md)
- [GitHub 公开发布审核清单](../release/github-publication-checklist.md)
