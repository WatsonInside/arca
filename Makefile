# Arca — 常用命令入口
# 完整说明见 AGENTS.md；实现细节见 docs/ENGINEERING-NOTES.md

PY := build/venv/bin/python -S -E

.PHONY: setup thirdparty bundle package publish test test-fast audit shots brief

# 给接手的 AI Agent 用：一条命令自述家门 + 健康检查
brief:
	@echo "================ AGENTS.md（项目规则与捷径）================"
	@cat AGENTS.md
	@echo "================ 当前状态 ================"
	@git log --oneline -3
	@echo "未提交变更：$$(git status --porcelain | wc -l | tr -d ' ') 项"
	@echo "已安装版本：$$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' /Applications/Arca.app/Contents/Info.plist 2>/dev/null || echo 未安装)"
	@echo "================ 快速门禁 ================"
	@node --check src/arca/web/app.js && echo "app.js 语法 OK"
	@$(PY) scripts/design_audit.py | tail -1

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
