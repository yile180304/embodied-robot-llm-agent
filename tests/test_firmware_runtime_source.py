from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from embodied_agent.schemas import ObservationMessage


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_DIR = PROJECT_ROOT / "firmware_sim"
RUNTIME_DIR = PROJECT_ROOT / "firmware_runtime"
FREERTOS_DIR = PROJECT_ROOT / ".deps" / "FreeRTOS-Kernel"
ARMCLANG = Path(os.environ.get("ARMCLANG", r"D:\keil\ARM\ARMCLANG\bin\armclang.exe"))
CJSON_DIR = PROJECT_ROOT / ".deps" / "cJSON"


def _host_compiler() -> list[str] | None:
    cc = shutil.which("cc")
    if cc is not None:
        return [cc]
    zig = Path(sys.executable).resolve().parents[1] / "Lib" / "site-packages" / "ziglang" / "zig.exe"
    if zig.exists():
        return [str(zig), "cc"]
    return None


HOST_COMPILER = _host_compiler()


def test_runtime_source_has_bounded_uart_and_port_contracts():
    header = (RUNTIME_DIR / "robot_runtime.h").read_text(encoding="utf-8")
    source = (RUNTIME_DIR / "robot_runtime.c").read_text(encoding="utf-8")
    combined = header + source
    for token in (
        "robot_uart_spsc_ingress_t",
        "ROBOT_UART_RX_CAPACITY",
        "ROBOT_JSON_FRAME_CAPACITY",
        "robot_runtime_queue_send_fn",
        "robot_runtime_observation_sink_fn",
        "uart_overflow",
        "frame_too_long",
        "queue_full",
    ):
        assert token in combined
    for forbidden in ("HAL_UART", "HAL_DMA", "MPU6050", "set_pwm", "exec(", "eval("):
        assert forbidden not in combined


def test_freertos_binding_uses_static_official_apis_without_starting_scheduler():
    header = (RUNTIME_DIR / "robot_freertos_runtime.h").read_text(encoding="utf-8")
    source = (RUNTIME_DIR / "robot_freertos_runtime.c").read_text(encoding="utf-8")
    combined = header + source
    for token in (
        "StaticQueue_t",
        "StaticTask_t",
        "xQueueCreateStatic",
        "xQueueSend",
        "xQueueReceive",
        "xTaskCreateStatic",
        "vTaskNotifyGiveFromISR",
        "ulTaskNotifyTake",
        "portYIELD_FROM_ISR",
    ):
        assert token in combined
    for forbidden in (
        "vTaskStartScheduler",
        "HAL_UART",
        "HAL_DMA",
        "MPU6050",
        "set_pwm",
    ):
        assert forbidden not in combined


@pytest.mark.skipif(not ARMCLANG.exists(), reason="Keil ARM Compiler is not installed")
def test_runtime_core_cross_compiles_for_cortex_m4(tmp_path):
    for source_name in ("robot_runtime.c", "robot_runtime_host.c"):
        output = tmp_path / f"{Path(source_name).stem}.o"
        subprocess.run(
            [
                str(ARMCLANG),
                "-std=c11",
                "--target=arm-arm-none-eabi",
                "-mcpu=cortex-m4",
                "-mthumb",
                "-Wall",
                "-Wextra",
                "-Werror",
                f"-I{PROTOCOL_DIR}",
                f"-I{RUNTIME_DIR}",
                "-c",
                str(RUNTIME_DIR / source_name),
                "-o",
                str(output),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert output.stat().st_size > 0


@pytest.mark.skipif(not ARMCLANG.exists(), reason="Keil ARM Compiler is not installed")
@pytest.mark.skipif(
    not (FREERTOS_DIR / "include" / "FreeRTOS.h").exists(),
    reason="pinned FreeRTOS headers are unavailable",
)
def test_freertos_binding_cross_compiles_for_cortex_m4(tmp_path):
    output = tmp_path / "robot_freertos_runtime.o"
    subprocess.run(
        [
            str(ARMCLANG),
            "-std=c11",
            "--target=arm-arm-none-eabi",
            "-mcpu=cortex-m4",
            "-mthumb",
            "-Wall",
            "-Wextra",
            "-Werror",
            f"-I{PROTOCOL_DIR}",
            f"-I{RUNTIME_DIR}",
            f"-I{FREERTOS_DIR / 'include'}",
            f"-I{FREERTOS_DIR / 'portable' / 'GCC' / 'ARM_CM4F'}",
            "-c",
            str(RUNTIME_DIR / "robot_freertos_runtime.c"),
            "-o",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert output.stat().st_size > 0


@pytest.mark.skipif(HOST_COMPILER is None, reason="no host C compiler is installed")
@pytest.mark.skipif(not (CJSON_DIR / "cJSON.c").exists(), reason="local cJSON source is unavailable")
def test_runtime_host_executes_framing_queue_and_replay_scenarios(tmp_path):
    executable = tmp_path / "robot_runtime_test.exe"
    subprocess.run(
        [
            *HOST_COMPILER,
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            f"-I{PROTOCOL_DIR}",
            f"-I{RUNTIME_DIR}",
            f"-I{CJSON_DIR}",
            str(PROTOCOL_DIR / "robot_protocol.c"),
            str(PROTOCOL_DIR / "robot_protocol_json.c"),
            str(RUNTIME_DIR / "robot_runtime.c"),
            str(RUNTIME_DIR / "robot_runtime_host.c"),
            str(RUNTIME_DIR / "test_robot_runtime.c"),
            str(CJSON_DIR / "cJSON.c"),
            "-lm",
            "-o",
            str(executable),
        ],
        check=True,
    )
    completed = subprocess.run(
        [str(executable)],
        check=True,
        capture_output=True,
        text=True,
    )
    observations = [
        ObservationMessage.model_validate_json(line)
        for line in completed.stdout.splitlines()
        if line.strip()
    ]
    assert len(observations) >= 10
    assert any(item.error_code == "uart_overflow" for item in observations)
    assert any(item.error_code == "frame_too_long" for item in observations)
    assert any(item.error_code == "queue_full" for item in observations)
    assert any(item.status.value == "emergency_stop" for item in observations)
