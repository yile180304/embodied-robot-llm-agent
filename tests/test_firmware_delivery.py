from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_FILE = PROJECT_ROOT / "firmware_keil" / "robot_firmware_runtime.uvprojx"
UV4 = Path(os.environ.get("KEIL_UV4", r"D:\keil\UV4\UV4.exe"))
FIRMWARE_DEPENDENCIES_AVAILABLE = all(
    path.is_file()
    for path in (
        PROJECT_ROOT / ".deps" / "cJSON" / "cJSON.c",
        PROJECT_ROOT / ".deps" / "cJSON" / "cJSON.h",
        PROJECT_ROOT / ".deps" / "FreeRTOS-Kernel" / "include" / "FreeRTOS.h",
        PROJECT_ROOT
        / ".deps"
        / "FreeRTOS-Kernel"
        / "portable"
        / "GCC"
        / "ARM_CM4F"
        / "port.c",
    )
)


def test_dependency_manifest_and_ignored_outputs_are_pinned():
    dependency_script = (PROJECT_ROOT / "scripts" / "firmware_deps.py").read_text(
        encoding="utf-8"
    )
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for token in (
        "c859b25da02955fef659d658b8f324b5cde87be3",
        "88971102bf8aa34b4fcc24f176db620dd9c854f7",
        "0adc196d4bd52a2d91102b525b0aafc1e14a2386",
    ):
        assert token in dependency_script
    assert ".deps/" in gitignore
    assert "firmware_keil/Objects/" in gitignore
    assert 'ziglang==0.15.2' in pyproject


def test_keil_project_is_armclang_static_library_without_board_sources():
    tree = ET.parse(PROJECT_FILE)
    root = tree.getroot()
    target = root.find("./Targets/Target")
    assert target is not None
    assert target.findtext("TargetName") == "robot_runtime_cortex_m4"
    assert target.findtext("pArmCC") == r"6070000::V6.7::.\ARMCLANG"
    common = target.find("./TargetOption/TargetCommonOption")
    assert common is not None
    assert common.findtext("CreateExecutable") == "0"
    assert common.findtext("CreateLib") == "1"
    assert common.findtext("CreateHexFile") == "0"
    project_text = PROJECT_FILE.read_text(encoding="utf-8")
    for token in (
        "robot_freertos_runtime.c",
        "FreeRTOS-Kernel\\portable\\GCC\\ARM_CM4F\\port.c",
        "-std=c11 -Wall -Wextra -Werror",
    ):
        assert token in project_text
    for forbidden in ("startup_", "HAL_UART", "HAL_DMA", "MPU6050", "set_pwm"):
        assert forbidden not in project_text


@pytest.mark.skipif(not UV4.exists(), reason="Keil uVision is not installed")
@pytest.mark.skipif(
    not FIRMWARE_DEPENDENCIES_AVAILABLE,
    reason="run scripts/firmware_deps.py fetch before the optional Keil build test",
)
def test_uv4_batch_build_creates_only_static_library():
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "build_firmware.py"),
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert '"result": "passed"' in completed.stdout
    library = PROJECT_ROOT / "firmware_keil" / "Objects" / "robot_firmware_runtime.lib"
    assert library.stat().st_size > 0
    assert not list((PROJECT_ROOT / "firmware_keil" / "Objects").glob("*.hex"))
    assert not list((PROJECT_ROOT / "firmware_keil" / "Objects").glob("*.axf"))
