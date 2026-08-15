# Firmware Runtime Adapter

这一层把平台无关的协议核心接到两个可替换端口：

```text
UART/DMA chunks
→ UART SPSC ingress
→ NDJSON frame assembler
→ protocol prepare
→ static host queue / FreeRTOS Queue
→ control task
→ deterministic execute
→ Observation Sink
```

## 已验证内容

- `robot_runtime.c`：固定容量 UART SPSC、任意分块 NDJSON、超长帧/溢出恢复、Queue 满不 commit、Observation JSON sink；
- `robot_runtime_host.c`：无 RTOS 的固定容量测试端口；
- `robot_freertos_runtime.c`：真实 `xQueueCreateStatic`、`xQueueSend`、`xQueueReceive`、`xTaskCreateStatic`、`vTaskNotifyGiveFromISR`、`ulTaskNotifyTake` 和 `portYIELD_FROM_ISR`；
- `FreeRTOSConfig.h`：通用 Cortex-M4 编译配置，未绑定具体 MCU/向量表；
- `firmware_keil/robot_firmware_runtime.uvprojx`：只生成静态库，不生成 `.hex`/`.axf`。

运行：

```powershell
.venv\Scripts\python.exe scripts\firmware_deps.py fetch
.venv\Scripts\python.exe scripts\build_firmware.py --output reports\firmware-build.json
```

## 真实性边界

UV4 日志证明的是源码和官方 FreeRTOS API binding 的 Cortex-M4F 编译，不是 STM32 实机运行。工程不含 HAL、startup/scatter、真实 UART/DMA IRQ、Wi-Fi、MPU6050、PID、PWM、舵机、电机或 `vTaskStartScheduler()` 调用。板级移植时必须重新审查时钟、NVIC 优先级、FPU ABI、向量表和内存布局。
