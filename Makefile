PYTHON ?= python3

.PHONY: help doctor config-check smoke synth-smoke foundation-check check

help:
	@printf '%s\n' 'doctor            Inventory required and optional local tools' 'config-check      Validate the provisional configuration and negative cases' 'smoke             Build/run the HDL probe and compile/check a freestanding RV32 ELF' 'synth-smoke       Synthesize the HDL probe with an available Yosys' 'foundation-check  Run all required P0 checks offline' 'check             Alias for foundation-check'

doctor:
	$(PYTHON) scripts/doctor.py

config-check:
	$(PYTHON) scripts/config.py configs/baseline.json
	$(PYTHON) -m unittest discover -s tests/config -v

smoke:
	$(PYTHON) scripts/smoke.py

synth-smoke:
	$(PYTHON) scripts/smoke.py --synthesis

foundation-check:
	$(PYTHON) scripts/foundation.py

check: foundation-check
