from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from embodied_agent.schemas import ObservationMessage, ObservationStatus


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIRMWARE_DIR = PROJECT_ROOT / "firmware_sim"
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


def test_c_protocol_source_contains_required_end_side_contracts():
    header = (FIRMWARE_DIR / "robot_protocol.h").read_text(encoding="utf-8")
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            FIRMWARE_DIR / "robot_protocol.c",
            FIRMWARE_DIR / "robot_protocol_json.c",
        )
    )
    for token in (
        "robot_ring_buffer_t",
        "robot_command_queue_t",
        "robot_idempotency_entry_t",
        "robot_ingest_result_t",
        "robot_observation_t",
        "task_id",
        "seq",
        "ROBOT_INGEST_REPLAY",
        "ROBOT_STATUS_BLOCKED",
    ):
        assert token in header + source
    for tool in (
        "move_robot",
        "turn_robot",
        "get_robot_state",
        "scan_obstacles",
        "emergency_stop",
    ):
        assert tool in source
    assert "cJSON_Parse" in source
    assert "exec(" not in source
    assert "eval(" not in source


@pytest.mark.skipif(not ARMCLANG.exists(), reason="Keil ARM Compiler is not installed")
@pytest.mark.skipif(not (CJSON_DIR / "cJSON.h").exists(), reason="local cJSON source is unavailable")
def test_c_protocol_cross_compiles_for_cortex_m4(tmp_path):
    for source_name in ("robot_protocol.c", "robot_protocol_json.c"):
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
                f"-I{FIRMWARE_DIR}",
                f"-I{CJSON_DIR}",
                "-c",
                str(FIRMWARE_DIR / source_name),
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
def test_c_protocol_host_behavior(tmp_path):
    executable = tmp_path / "robot_protocol_test.exe"
    subprocess.run(
        [
            *HOST_COMPILER,
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            f"-I{FIRMWARE_DIR}",
            f"-I{CJSON_DIR}",
            str(FIRMWARE_DIR / "robot_protocol.c"),
            str(FIRMWARE_DIR / "robot_protocol_json.c"),
            str(FIRMWARE_DIR / "test_robot_protocol.c"),
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
    observation = ObservationMessage.model_validate_json(completed.stdout.strip().splitlines()[-1])
    assert observation.status is ObservationStatus.BLOCKED
    assert observation.error_code == "front_obstacle"
