from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_FILE = PROJECT_ROOT / "firmware_keil" / "robot_firmware_runtime.uvprojx"
TARGET_NAME = "robot_runtime_cortex_m4"
BUILD_LOG = PROJECT_ROOT / "reports" / "firmware-build" / "keil-build.txt"
LIBRARY = PROJECT_ROOT / "firmware_keil" / "Objects" / "robot_firmware_runtime.lib"
DEFAULT_UV4 = Path(r"D:\keil\UV4\UV4.exe")
DEFAULT_ARMCLANG = Path(r"D:\keil\ARM\ARMCLANG\bin\armclang.exe")

SOURCES = (
    "firmware_sim/robot_protocol.c",
    "firmware_sim/robot_protocol_json.c",
    "firmware_runtime/robot_runtime.c",
    "firmware_runtime/robot_freertos_runtime.c",
    ".deps/cJSON/cJSON.c",
    ".deps/FreeRTOS-Kernel/list.c",
    ".deps/FreeRTOS-Kernel/queue.c",
    ".deps/FreeRTOS-Kernel/tasks.c",
    ".deps/FreeRTOS-Kernel/portable/GCC/ARM_CM4F/port.c",
)


def _tool_path(environment_name: str, default: Path) -> Path:
    configured = os.environ.get(environment_name)
    return Path(configured) if configured else default


def _dependency_report() -> list[dict[str, object]]:
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "firmware_deps.py"),
            "verify",
            "--json",
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _read_keil_log() -> str:
    try:
        return BUILD_LOG.read_text(encoding="mbcs", errors="replace")
    except LookupError:
        return BUILD_LOG.read_text(encoding="utf-8", errors="replace")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def build_report() -> dict[str, object]:
    uv4 = _tool_path("KEIL_UV4", DEFAULT_UV4)
    armclang = _tool_path("ARMCLANG", DEFAULT_ARMCLANG)
    for tool in (uv4, armclang):
        if not tool.is_file():
            raise RuntimeError(f"required tool is unavailable: {tool}")
    if not PROJECT_FILE.is_file():
        raise RuntimeError(f"Keil project is unavailable: {PROJECT_FILE}")

    dependencies = _dependency_report()
    BUILD_LOG.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            str(uv4),
            "-r",
            str(PROJECT_FILE),
            "-t",
            TARGET_NAME,
            "-o",
            str(BUILD_LOG),
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if not BUILD_LOG.is_file():
        raise RuntimeError("UV4 returned without creating its requested build log")
    build_log = _read_keil_log()
    summary = re.search(r"(\d+) Error\(s\), (\d+) Warning\(s\)", build_log)
    compiler = re.search(r"Using Compiler '([^']+)'", build_log)
    errors = int(summary.group(1)) if summary else -1
    warnings = int(summary.group(2)) if summary else -1
    if completed.returncode != 0 or errors != 0 or warnings != 0 or not LIBRARY.is_file():
        raise RuntimeError(
            "UV4 batch build failed; inspect "
            f"{BUILD_LOG} (exit={completed.returncode}, errors={errors}, warnings={warnings})"
        )

    version = subprocess.run(
        [str(armclang), "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {
        "verified_at": date.today().isoformat(),
        "result": "passed",
        "scope": "Cortex-M4F static library compile only",
        "project": _relative(PROJECT_FILE),
        "target": TARGET_NAME,
        "uv4": {
            "path": str(uv4),
            "exit_code": completed.returncode,
            "build_log": _relative(BUILD_LOG),
        },
        "compiler": {
            "reported_by_uv4": compiler.group(1) if compiler else "unknown",
            "path": str(armclang),
            "version": version.splitlines(),
            "language": "C11",
            "warning_policy": "-Wall -Wextra -Werror",
        },
        "dependencies": [
            {
                "name": dependency["name"],
                "tag": dependency["tag"],
                "tag_object": dependency["tag_object"],
                "commit": dependency["commit"],
            }
            for dependency in dependencies
        ],
        "sources": list(SOURCES),
        "artifact": {
            "path": _relative(LIBRARY),
            "kind": "static_library",
            "size_bytes": LIBRARY.stat().st_size,
            "sha256": _sha256(LIBRARY),
            "is_flashable_image": False,
        },
        "limitations": [
            "no STM32 startup file, scatter file, HAL, board selection, flash image or flashing",
            "no FreeRTOS scheduler or task execution was started on hardware",
            "no UART/DMA IRQ, Wi-Fi, MQTT, MPU6050, PID, PWM, servo or motor integration",
            "the generic FreeRTOS clock, NVIC priority and FPU/ABI settings require board-specific review",
            "this result is compile evidence and must not be described as STM32 hardware integration",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify pinned dependencies and batch-build the Keil Cortex-M4 runtime library."
    )
    parser.add_argument("--output", type=Path, help="optional UTF-8 JSON report path")
    arguments = parser.parse_args()
    try:
        report = build_report()
    except (OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"firmware build failed: {exc}", file=sys.stderr)
        return 1

    rendered = json.dumps(report, ensure_ascii=True, indent=2)
    if arguments.output is not None:
        output = arguments.output
        if not output.is_absolute():
            output = PROJECT_ROOT / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
