from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPS_ROOT = PROJECT_ROOT / ".deps"


@dataclass(frozen=True)
class FirmwareDependency:
    name: str
    repository: str
    tag: str
    tag_object: str
    commit: str
    required_files: tuple[str, ...]


DEPENDENCIES = (
    FirmwareDependency(
        name="cJSON",
        repository="https://github.com/DaveGamble/cJSON.git",
        tag="v1.7.19",
        tag_object="c859b25da02955fef659d658b8f324b5cde87be3",
        commit="c859b25da02955fef659d658b8f324b5cde87be3",
        required_files=("cJSON.c", "cJSON.h", "LICENSE"),
    ),
    FirmwareDependency(
        name="FreeRTOS-Kernel",
        repository="https://github.com/FreeRTOS/FreeRTOS-Kernel.git",
        tag="V11.2.0",
        tag_object="88971102bf8aa34b4fcc24f176db620dd9c854f7",
        commit="0adc196d4bd52a2d91102b525b0aafc1e14a2386",
        required_files=(
            "include/FreeRTOS.h",
            "portable/GCC/ARM_CM4F/port.c",
            "portable/GCC/ARM_CM4F/portmacro.h",
            "LICENSE.md",
        ),
    ),
)


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _clone_missing(dependency: FirmwareDependency) -> None:
    target = DEPS_ROOT / dependency.name
    if target.exists():
        return
    DEPS_ROOT.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            dependency.tag,
            "--single-branch",
            dependency.repository,
            str(target),
        ],
        check=True,
    )


def _verify(dependency: FirmwareDependency) -> dict[str, object]:
    target = DEPS_ROOT / dependency.name
    errors: list[str] = []
    if not (target / ".git").is_dir():
        errors.append("missing Git checkout")
        return {**asdict(dependency), "path": str(target), "ok": False, "errors": errors}

    values: dict[str, str] = {}
    commands = {
        "origin": ("remote", "get-url", "origin"),
        "head": ("rev-parse", "HEAD"),
        "resolved_tag_object": ("rev-parse", dependency.tag),
        "resolved_tag_commit": ("rev-parse", f"{dependency.tag}^{{commit}}"),
        "dirty": ("status", "--porcelain"),
    }
    for key, arguments in commands.items():
        try:
            values[key] = _git(target, *arguments)
        except subprocess.CalledProcessError as exc:
            errors.append(f"git {key} failed with exit code {exc.returncode}")

    if values.get("origin") != dependency.repository:
        errors.append(f"origin mismatch: {values.get('origin', '<unavailable>')}")
    if values.get("head") != dependency.commit:
        errors.append(f"HEAD mismatch: {values.get('head', '<unavailable>')}")
    if values.get("resolved_tag_object") != dependency.tag_object:
        errors.append(
            f"tag object mismatch: {values.get('resolved_tag_object', '<unavailable>')}"
        )
    if values.get("resolved_tag_commit") != dependency.commit:
        errors.append(
            f"tag commit mismatch: {values.get('resolved_tag_commit', '<unavailable>')}"
        )
    if values.get("dirty"):
        errors.append("dependency checkout has local modifications")
    for relative_path in dependency.required_files:
        if not (target / relative_path).is_file():
            errors.append(f"missing required file: {relative_path}")

    return {
        **asdict(dependency),
        "path": str(target),
        **values,
        "ok": not errors,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch or verify the pinned cJSON and FreeRTOS firmware dependencies."
    )
    parser.add_argument("action", choices=("fetch", "verify"), nargs="?", default="verify")
    parser.add_argument("--json", action="store_true", dest="as_json")
    arguments = parser.parse_args()

    try:
        if arguments.action == "fetch":
            for dependency in DEPENDENCIES:
                _clone_missing(dependency)
        results = [_verify(dependency) for dependency in DEPENDENCIES]
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"firmware dependency operation failed: {exc}", file=sys.stderr)
        return 2

    if arguments.as_json:
        print(json.dumps(results, ensure_ascii=True, indent=2))
    else:
        for result in results:
            state = "ok" if result["ok"] else "failed"
            print(f"{result['name']}: {state} ({result['commit']})")
            for error in result["errors"]:
                print(f"  - {error}")
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
