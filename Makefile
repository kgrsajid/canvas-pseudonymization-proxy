.PHONY: selftest test dist clean

VENV := .venv
PY := $(VENV)/bin/python

$(VENV)/pyvenv.cfg:
	uv venv --python python3.13 $(VENV)
	uv pip install -e . --group dev -p $(PY)

deps: $(VENV)/pyvenv.cfg

test: deps
	$(PY) -m pytest tests/ -q

# Built-in self-test (§11.3): full suite incl. PII scanner + round-trip drills.
selftest: deps
	@$(PY) -m pytest tests/ -q; ret=$$?; \
	if [ $$ret -eq 0 ]; then echo "SELFTEST: PASS"; else echo "SELFTEST: FAIL"; fi; \
	exit $$ret

# Institution handoff tarball (§11): everything needed to self-host, nothing else.
dist: selftest
	tar --exclude='.venv' --exclude='./data' --exclude='.env' --exclude='__pycache__' \
	    --exclude='.pytest_cache' --exclude='*.pyc' \
	    -czf ../canvas-privacy-proxy-dist.tar.gz .
	@echo "Tarball: ../canvas-privacy-proxy-dist.tar.gz"

clean:
	rm -rf .venv data *.tar.gz .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
