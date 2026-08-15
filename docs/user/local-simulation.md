---
doc_type: user-guide
slug: local-simulation
component: quadruped-simulation-platform
status: draft
summary: 在本机启动四足机器人仿真、提交任务并理解运行结果的操作指南。
tags: [simulation, mqtt, mission-console, replay, quickstart]
last_reviewed: 2026-08-16
---

# 本地仿真使用指南

## 功能简介

本地仿真把自然语言任务、Agent Tool Call、本机 MQTT、Python 连续世界和 Three.js 控制台连成一个可观察闭环。你可以提交移动或语义任务，查看规划、MQTT publish、Observation、障碍阻挡、重新规划和最终状态，也可以运行固定 Fault、导出最近一次证据并在浏览器离线回放。

![Mission Console](../../reports/mission-control-desktop-scene.png)

当前机器人外观使用本地 Unitree GO1 视觉资产，页面状态栏会显示 `GO1 / READY`、`LOADING` 或 `FAILED`。网格只负责显示，Python 世界仍是位姿、碰撞、footprint、传感器和任务结果的唯一权威源。

## 前置条件

- 已按 README 完成 Python 安装和 `web` 生产构建；
- 本机 Mosquitto 或兼容 Broker 正在监听 `127.0.0.1:1883`；
- `8001` 端口未被其他进程占用；
- 使用支持 WebGL 和 WebSocket 的现代浏览器。

## 如何使用

### 1. 启动 Agent + MQTT bridge

在项目根目录运行：

```powershell
.venv\Scripts\python.exe -m embodied_agent.cli simulation `
  --bridge --host 127.0.0.1 --port 8001 `
  --mqtt-host 127.0.0.1 --mqtt-port 1883 --device-id dog01
```

终端保持运行，然后打开 <http://127.0.0.1:8001>。

### 2. 提交任务

在 Mission Console 中：

1. 在目标输入框填写任务，例如“前进 2 米，如果遇到障碍就从更宽的一侧绕开”。
2. Planner 选择 `Fake`，最大步骤保持 `8`。
3. 点击 Run mission。
4. 观察时间线中的 Goal、Planning、Action / MQTT Publish、Observation、Replanning 和 Final。

同一时刻只运行一个任务。任务运行中重复提交会被拒绝，不会静默覆盖当前任务。

### 3. 理解结果

| 状态 | 含义 |
| --- | --- |
| `success` | 动作或任务按权威仿真结果完成 |
| `blocked` | 机器人在障碍或世界边界前安全停止 |
| `rejected` | Schema、Safety、幂等、队列或状态条件拒绝执行 |
| `timeout` | deadline 或动作预算耗尽 |
| `emergency_stop` | 急停生效，活动/排队运动被取消 |
| `step_limit` | Agent 达到最大步骤数并安全退出 |

默认 challenge world 会让第一次前进在障碍前 `blocked`。FakePlanner 随后读取三向距离，只尝试一次有界 L 形绕行；它不是 A*、SLAM 或通用导航规划器。

### 4. 使用操作员控制

- Pause：冻结仿真逻辑时间、位姿和动作进度；
- Resume：继续当前仿真；
- Reset：完成或取消挂起回调后恢复初始世界；
- Emergency stop：在 bridge 模式中优先取消当前任务和运动。

解除急停不暴露给模型。需要恢复时使用操作员 Reset。

### 5. 使用 preview 模式

不需要 MQTT 任务，只查看确定性动作和渲染时运行：

```powershell
.venv\Scripts\python.exe -m embodied_agent.cli simulation `
  --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。preview 使用内置 Demo Program，不代表 Agent 或 MQTT 正在工作。

### 6. 使用真实模型

配置 `.env` 后运行 `provider-check`。Bridge runtime 启动时冻结该配置，修改后需要重启。Mission Console 只有在 runtime 检测到完整 provider 配置时才允许选择 Model。真实模型必须返回原生 Tool Call；正文 JSON、代码块、多个调用或空响应不会被当作可执行动作。

### 7. 导出和回放

完成 Mission 或 Fault Run 后，在 Replay & Evidence 工作台选择 Export current 保存最近一次 Replay Bundle。也可以选择 Import JSON 导入本地 Bundle，然后使用 play/pause、seek、step 和 0.5x/1x/2x 控件查看 GO1、pose、sensor 与事件/证据时间线。

Replay Mode 只读：Mission、Fault、Pause/Resume/Reset/Emergency Stop 等 mutation 控件会被禁用，播放不会 publish MQTT 或推进 Python Engine。选择 Return live 后，页面重新读取权威 world、simulation snapshot、Mission current 和 Fault current。

要生成固定验收包，确保本机 Broker 正常后运行：

```powershell
.venv\Scripts\python.exe -m embodied_agent.cli simulation-acceptance `
  --mqtt-host 127.0.0.1 --mqtt-port 1883 `
  --device-id dog-accept-local `
  --output reports\acceptance-pack-local
```

输出包含各场景 Bundle、SHA-256 manifest 和 JSON/Markdown 真实性报告。默认不调用外部 provider：已配置时标记 `skipped: provider_opt_in_required`，未配置时标记 `skipped: provider_unconfigured`，不会使用 FakePlanner 冒充通过。

确认费用、隐私和网络副作用后，可显式运行固定真实模型场景：

```powershell
.venv\Scripts\python.exe -m embodied_agent.cli simulation-acceptance `
  --mqtt-host 127.0.0.1 --mqtt-port 1883 `
  --device-id dog-accept-model `
  --include-real-model `
  --output reports\acceptance-pack-model
```

通过条件不是“配置成功”，而是 Replay 含匹配的原生 `tool_call → published → observation → final` 源事件链，并显示 `native_tool_calls_verified`。

## 常见问题

Q: 页面显示 offline 或一直 reconnecting？

A: 确认 runtime 终端仍在运行、访问端口与启动参数一致，并检查浏览器是否能请求 `/api/simulation/world`。前端采用有限次数退避重连，超过预算后会明确进入 offline。

Q: bridge 启动失败或任务没有 Observation？

A: 检查 `127.0.0.1:1883` 是否有真实 Broker。项目不会在 Broker 不可用时假装成功。

Q: 页面启动时报找不到前端构建？

A: 在 `web/` 下执行 `npm ci` 和 `npm run build`。`web/dist` 是生成物，不提交到 Git。

Q: 为什么 Model 选项不可用？

A: 当前没有完整的 `EMBODIED_AGENT_API_KEY`、`EMBODIED_AGENT_MODEL` 和 `EMBODIED_AGENT_BASE_URL`。FakePlanner 仍可用于确定性演示。

Q: GO1 模型加载失败怎么办？

A: 检查 `web/public/assets/go1/` 是否包含五组 STL，并确认 `NOTICE.md` 和 `LICENSE` 没有被移走。失败时页面会保留世界和控制台，不会静默显示盒体假模型；刷新页面即可重新尝试。

Q: 这些距离是不是实际传感器数据？

A: 不是。它们来自 Python World Config 和几何计算，是 simulation ground truth。

Q: 回放会让机器人再次移动吗？

A: 不会。回放只在浏览器内按记录帧更新显示，不调用动作 API、不发送 MQTT、不执行 AgentGraph，也不修改 live pose。

## 相关功能

- [项目 README](../../README.md)
- [贡献者开发指南](../dev/contributing.md)
- [安全策略](../../SECURITY.md)
