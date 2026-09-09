from datetime import datetime, timezone
import hashlib
import json
import subprocess
import sys

from config import read_json, validate
from doctor import ROOT, inventory, tool_path
from smoke import smoke


def git(*arguments):
    return subprocess.check_output(["git", *arguments], cwd=ROOT, text=True).strip()


def source_files():
    paths = [ROOT / name for name in ("Makefile", ".gitignore")]
    for name in ("scripts", "configs", "spec", "tests", "third_party"):
        paths.extend(p for p in (ROOT / name).rglob("*") if p.is_file()
                     and "__pycache__" not in p.parts and p.suffix != ".pyc"
                     and "releases" not in p.parts)
    return sorted(set(paths))


def main():
    output = ROOT / "build/foundation.json"
    output.parent.mkdir(exist_ok=True)
    report = {"status": "failed", "phase": "P0 foundation",
              "timestamp_utc": datetime.now(timezone.utc).isoformat()}
    output.write_text(json.dumps(report, indent=2) + "\n")
    try:
        tools = inventory()
        report["tools"] = tools
        missing = [name for name, item in tools.items() if item["required"] and item["status"] != "available"]
        if missing:
            raise RuntimeError(f"missing or failed required tools: {', '.join(missing)}")
        validate(read_json(ROOT / "configs/baseline.json"))
        subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests/config", "-v"], cwd=ROOT, check=True)
        excluded = git("check-ignore", "--no-index", "docs_plan/README.md")
        if excluded != "docs_plan/README.md" or git("ls-files", "--", "docs_plan"):
            raise RuntimeError("private plan is not excluded or is already tracked")
        paths = source_files()
        report["smoke"] = smoke()
        report["synthesis"] = smoke(True) if tool_path("yosys") else {"status": "unavailable", "required_for_foundation": False}
        report["source_sha256"] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        report.update(status="passed", config_validation="structure only; P1 capacity model pending",
                      core_qualification="pending acquisition and integration spike",
                      board_qualification="pending; no physical board available",
                      formal_proofs="not run; no Chronos RTL exists")
        print("PASS: P0 foundation; core and physical-board qualification remain pending")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        report["error"] = str(error)
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    finally:
        output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
