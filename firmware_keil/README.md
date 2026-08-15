# Keil Cortex-M4F 静态库工程

`robot_firmware_runtime.uvprojx` 使用本机 uVision/ARM Compiler 6.7 编译协议核心、Runtime Core、FreeRTOS Binding、cJSON 和所需 FreeRTOS Kernel 源码。

```powershell
.venv\Scripts\python.exe scripts\firmware_deps.py fetch
.venv\Scripts\python.exe scripts\build_firmware.py --output reports\firmware-build.json
```

成功时生成：

- `Objects/robot_firmware_runtime.lib`：静态库；
- `reports/firmware-build/keil-build.txt`：UV4 原始日志；
- `reports/firmware-build.json`：工具链、依赖 commit、产物哈希和限制说明。

目标明确设置 `CreateExecutable=0`、`CreateLib=1`、`CreateHexFile=0`。项目没有 startup、scatter、HAL 或具体 STM32 型号，因此 `.lib` 不是可烧录镜像，也不是板上 FreeRTOS 运行证据。
