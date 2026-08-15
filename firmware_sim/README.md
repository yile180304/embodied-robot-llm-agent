# PC/ARM C 端协议参考

这个目录是 HANDOFF 阶段 6 的独立协议证据层，用来把 Python 消息契约映射到典型 MCU 端的几项确定性职责：

```text
接收字节 → Ring Buffer → cJSON 校验 → task_id + seq 去重 → Command Queue → 状态机执行
```

当前实现包含：

- 固定容量 Ring Buffer；
- 固定容量 Command Queue（可替换为 FreeRTOS Queue）；
- cJSON 解析、版本/字段/数值范围校验；
- 五个动作/状态高层 Tool 的白名单映射；只读 `inspect_semantic_world` 由 Python 仿真层实现，不下沉到 C 协议核心；
- deadline、乱序 seq、重复 `(task_id, seq)` 的业务语义；
- 简化的移动、转向、障碍和急停状态机。

运行时编排位于相邻的 `firmware_runtime/`：它增加 UART SPSC/NDJSON 分帧、Prepared Command 的 Queue 提交边界、Observation Sink，以及独立的 FreeRTOS 静态 Queue/Task binding。两层共享 `robot_command_t` 与 `robot_observation_t`，但运行时层不把协议核心绑定到 RTOS。

它不包含 STM32 HAL、真实 UART/Wi-Fi、FreeRTOS scheduler 启动、MPU6050、PWM、舵机或 PID。因而它可以作为 PC/ARM 编译的协议参考，但不能描述成真实 STM32 联调。

## 编译验证

项目不把第三方 cJSON/FreeRTOS 源码纳入交付，而是在被忽略的 `.deps/` 中校验固定 tag/commit：

```powershell
.venv\Scripts\python.exe scripts\firmware_deps.py fetch
.venv\Scripts\python.exe scripts\firmware_deps.py verify
```

主机语义与 ArmClang 交叉编译已纳入 pytest：

```powershell
.venv\Scripts\python.exe -m pytest -q `
  tests\test_firmware_protocol_source.py `
  tests\test_firmware_runtime_source.py `
  tests\test_firmware_delivery.py
```

ARM 交叉编译只验证源码、头文件和目标架构兼容；没有链接 STM32 启动文件和板级工程，不产生“已烧录硬件”的结论。

完整静态库交付使用仓库内的 [robot_firmware_runtime.uvprojx](../firmware_keil/robot_firmware_runtime.uvprojx) 与 `scripts\build_firmware.py`；产物是 `.lib`，不是可烧录镜像。
