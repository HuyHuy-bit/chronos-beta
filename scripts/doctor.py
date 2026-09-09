import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TOOLS = {
    "git": ("GIT", "git", ["--version"], True),
    "make": ("MAKE_TOOL", "make", ["--version"], True),
    "cxx": ("CXX", "g++", ["--version"], True),
    "verilator": ("VERILATOR", "verilator", ["--version"], True),
    "riscv_cc": ("RISCV_CC", "riscv64-unknown-elf-gcc", ["--version"], True),
    "riscv_objdump": ("RISCV_OBJDUMP", "riscv64-unknown-elf-objdump", ["--version"], True),
    "yosys": ("YOSYS", "yosys", ["-V"], False),
    "sby": ("SBY", "sby", ["--version"], False),
    "z3": ("Z3", "z3", ["--version"], False),
    "boolector": ("BOOLECTOR", "boolector", ["--version"], False),
    "vivado": ("VIVADO", "vivado", ["-version"], False),
}


def tool_path(key):
    variable, name, _, _ = TOOLS[key]
    override = os.environ.get(variable)
    if override:
        return shutil.which(override)
    found = shutil.which(name)
    if found:
        return found
    suite = os.environ.get("OSS_CAD_SUITE")
    if suite:
        return shutil.which(name, path=str(Path(suite) / "bin"))
    return None


def inventory():
    records = {}
    for key, (_, _, arguments, required) in TOOLS.items():
        path = tool_path(key)
        record = {"required": required, "path": path, "status": "unavailable"}
        if path:
            try:
                result = subprocess.run([path, *arguments], capture_output=True, text=True, timeout=30)
                lines = (result.stdout + result.stderr).strip().splitlines()
                record.update(status="available" if result.returncode == 0 else "failed",
                              version=lines[0] if lines else "no version output")
            except (OSError, subprocess.TimeoutExpired) as error:
                record.update(status="failed", version=str(error))
        records[key] = record
    records["python"] = {"required": True, "path": sys.executable,
                         "version": sys.version.splitlines()[0],
                         "status": "available" if sys.version_info >= (3, 10) else "failed"}
    return records


def main():
    records = inventory()
    destination = ROOT / "build/doctor.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(records, indent=2) + "\n")
    for key, record in records.items():
        requirement = "required" if record["required"] else "optional"
        print(f"{key}: {record['status']} ({requirement}) {record.get('version', '')}")
    return int(any(r["required"] and r["status"] != "available" for r in records.values()))


if __name__ == "__main__":
    raise SystemExit(main())
