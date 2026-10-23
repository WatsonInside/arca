# Arca — 常用命令入口
# 完整说明见 AGENTS.md；实现细节见 docs/ENGINEERING-NOTES.md

PY := build/venv/bin/python -S -E

.PHONY: setup thirdparty bundle package publish test test-fast audit shots clean-help

setup:
	python3 -m venv build/venv
	build/venv/bin/pip install pyobjc-framework-Cocoa pyobjc-framework-WebKit

thirdparty:
	$(PY) scripts/build_thirdparty.py

bundle:
	ARCA_PYTHON=$$PWD/build/python-runtime ARCA_OUT=$$PWD/release/Arca.app $(PY) scripts/bundle.py

package:
	$(PY) scripts/package.py

publish:
	$(PY) scripts/publish.py

test:
	$(PY) scripts/all_test.py

test-fast:
	node --check src/arca/web/app.js
	$(PY) scripts/design_audit.py
	$(PY) scripts/selftest.py
	$(PY) scripts/progress_test.py

audit:
	$(PY) scripts/design_audit.py

shots:
	$(PY) scripts/shots.py
