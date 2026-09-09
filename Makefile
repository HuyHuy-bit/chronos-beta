PYTHON ?= python3

.PHONY: help doctor config-check smoke synth-smoke foundation-check model-check raw-check check

help:
	@printf '%s\n' 'doctor            Inventory required and optional local tools' 'config-check      Validate the provisional configuration and negative cases' 'smoke             Build/run the HDL probe and compile/check a freestanding RV32 ELF' 'synth-smoke       Synthesize the HDL probe with an available Yosys' 'foundation-check  Run all required P0 checks offline' 'model-check       Check P1a semantics, admission, and abstract reserve budgets' 'raw-check         Check P1b wire format and generate a raw model fragment' 'check             Run foundation, model, and raw-format checks'

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

model-check:
	$(PYTHON) -m scripts.model_check

raw-check:
	$(PYTHON) -m scripts.raw_check

check: foundation-check model-check raw-check
