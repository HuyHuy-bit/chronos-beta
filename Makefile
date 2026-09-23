PYTHON ?= python3

.PHONY: help doctor config-check smoke foundation-check model-check rtl-check check

help:
	@printf '%s\n' 'doctor            Inventory required and optional local tools' 'config-check      Validate the configuration and its negative cases' 'smoke             Compile and inspect a freestanding RV32 firmware ELF' 'foundation-check  Tools, configuration, private-plan exclusion, and the RV32 probe' 'model-check       Run every model test (format, codecs, retention, capacity, registers)' 'rtl-check         Lint, simulate, score, and synthesize the Chronos RTL' 'check             Run foundation, model, and RTL checks'

doctor:
	$(PYTHON) scripts/doctor.py

config-check:
	$(PYTHON) scripts/config.py configs/baseline.json
	$(PYTHON) -m unittest discover -s tests/config -v

smoke:
	$(PYTHON) scripts/smoke.py

foundation-check:
	$(PYTHON) scripts/foundation.py

model-check:
	$(PYTHON) -m scripts.model_check

rtl-check:
	$(PYTHON) -m scripts.rtl_check

check: foundation-check model-check rtl-check
