import json
from pathlib import Path
import re
import struct
import subprocess
import sys

from doctor import ROOT, tool_path

BUILD = ROOT / "build/toolchain"


def run(arguments, log):
    with log.open("w") as output:
        result = subprocess.run([str(a) for a in arguments], cwd=ROOT,
                                stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}); see {log.relative_to(ROOT)}")


def require(key):
    path = tool_path(key)
    if not path:
        raise RuntimeError(f"required for this check: {key}; configure its path, no download attempted")
    return path


def smoke():
    BUILD.mkdir(parents=True, exist_ok=True)
    result = {"status": "failed", "scope": "RV32 firmware toolchain probe; the RTL is checked by rtl-check"}
    receipt = BUILD / "smoke.json"
    receipt.write_text(json.dumps(result, indent=2) + "\n")
    elf = BUILD / "smoke.elf"
    run([require("riscv_cc"), "-march=rv32i", "-mabi=ilp32", "-O2", "-ffreestanding",
         "-fno-pic", "-fno-stack-protector", "-nostdlib", "-nostartfiles", "-mno-relax",
         "-Wall", "-Wextra", "-Werror", "-Wl,--no-relax", "-Wl,--build-id=none",
         "-T", "tests/toolchain/link.ld", "tests/toolchain/start.S",
         "tests/toolchain/firmware.c", "-o", elf], BUILD / "firmware-build.log")
    data = elf.read_bytes()
    if len(data) < 52 or data[:7] != b"\x7fELF\x01\x01\x01":
        raise RuntimeError("expected little-endian ELF32")
    kind, machine = struct.unpack_from("<HH", data, 16)
    entry = struct.unpack_from("<I", data, 24)[0]
    flags = struct.unpack_from("<I", data, 36)[0]
    if kind != 2 or machine != 243 or entry != 0x1000 or flags & 1:
        raise RuntimeError("ELF target, entry point, or compressed-instruction flag mismatch")
    run([require("riscv_objdump"), "-d", elf], BUILD / "firmware-disassembly.txt")
    instructions = re.findall(r"^\s*[0-9a-f]+:\s+([0-9a-f]+)\s", (BUILD / "firmware-disassembly.txt").read_text(), re.M)
    if not instructions or any(len(opcode) != 8 for opcode in instructions):
        raise RuntimeError("expected nonempty 32-bit-only instruction disassembly")
    result.update(status="passed", elf_class=32,
                  elf_machine="RISC-V", entry_point="0x1000",
                  instruction_count=len(instructions), firmware_executed=False)
    receipt.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    try:
        print(json.dumps(smoke(), indent=2))
    except (OSError, RuntimeError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
