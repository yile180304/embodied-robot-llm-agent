# Contributing

感谢你关注 Embodied Robot LLM Agent。项目目前处于仿真优先的 MVP 阶段，贡献应优先保持安全边界、行为可验证和公开描述真实。

## 开始之前

1. 阅读 [README](README.md) 和 [贡献者开发指南](docs/dev/contributing.md)。
2. 不要提交 `.env`、API Key、虚拟环境、`node_modules`、`.deps` 或 Keil 中间产物。
3. 新行为应带有与风险相称的测试；涉及 MQTT、Safety、幂等或任务结果时必须覆盖失败路径。
4. 不要把仿真、localhost MQTT、静态库编译或 FakePlanner 描述成真实机器人、真实模型或板级验收。

## 变更原则

- 模型可见能力固定通过 Tool Registry 管理；不要绕过 Schema 和 Safety。
- 模型输出不得进入 `eval`、`exec`、Shell 或任意代码执行路径。
- Python Simulation Core 是位姿、碰撞、传感器和任务结果的权威源；Three.js 只负责显示。
- MQTT QoS 1 必须继续使用 `(task_id, seq)` 做业务层幂等和冲突检测。
- 操作员急停优先于 Agent；暂停、继续和重置不应暴露成模型 Tool。
- C/FreeRTOS 代码保持平台无关，板级 HAL、启动文件和驱动应作为独立移植层处理。

## 本地验证

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pip check
Push-Location web
npm test
npm run build
Pop-Location
```

需要验证真实 MQTT 集成测试时，先在 `127.0.0.1:1883` 启动本地 Broker。没有 Broker 的环境会跳过相关用例，不应使用直接函数调用伪装成网络验收。

## Pull Request

PR 应说明：

- 解决的问题和用户可见变化；
- 修改的权威边界或协议契约；
- 运行过的测试和未覆盖的风险；
- 是否引入第三方代码、模型、图片或其他资产及其许可证；
- 是否改变 README 中的真实性边界。

详细环境和目录说明见 [docs/dev/contributing.md](docs/dev/contributing.md)。
