PYTHON ?= python3

.PHONY: help doctor config-check smoke synth-smoke foundation-check model-check raw-check snapshot-check compact-check control-check retention-check registers-check rtl-check check

help:
	@printf '%s\n' 'doctor            Inventory required and optional local tools' 'config-check      Validate the provisional configuration and negative cases' 'smoke             Build/run the HDL probe and compile/check a freestanding RV32 ELF' 'synth-smoke       Synthesize the HDL probe with an available Yosys' 'foundation-check  Run all required P0 checks offline' 'model-check       Check P1a semantics, admission, and abstract reserve budgets' 'raw-check         Check P1b wire format and generate a raw model fragment' 'check             Run foundation and all implemented model checks'
	@printf '%s\n' 'snapshot-check    Check P1c retention and authoritative model snapshots' 'compact-check     Check P1d exact-time codecs and framed size comparisons' 'control-check     Check P1e controller command races and streaming run flush' 'retention-check   Check P1f compressed retention and measured capacity' 'registers-check   Check P1g register map, trigger slots, and register-driven readout' 'rtl-check         Lint, simulate, and score the P2a raw RTL capture slice'

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

snapshot-check:
	$(PYTHON) -m scripts.snapshot_check

compact-check:
	$(PYTHON) -m scripts.compact_check

control-check:
	$(PYTHON) -m scripts.control_check

retention-check:
	$(PYTHON) -m scripts.retention_check

registers-check:
	$(PYTHON) -m scripts.registers_check

rtl-check:
	$(PYTHON) -m scripts.rtl_check

check: foundation-check model-check raw-check snapshot-check compact-check control-check retention-check registers-check rtl-check
