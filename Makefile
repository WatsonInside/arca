# Arca — 常用命令入口
# 完整说明见 AGENTS.md；实现细节见 docs/ENGINEERING-NOTES.md

ARCA_BUILD_DIR ?= $(CURDIR)/build
export ARCA_BUILD_DIR
PY := $(ARCA_BUILD_DIR)/venv/bin/python -S -E

.PHONY: setup thirdparty bundle package publish test test-fast verify audit shots brief stats

# 给接手的 AI Agent 用：一条命令自述家门 + 健康检查
brief:
	@echo "================ AGENTS.md（项目规则与捷径）================"
	@cat AGENTS.md
	@echo "================ 当前状态 ================"
	@git log --oneline -3
	@echo "未提交变更：$$(git status --porcelain | wc -l | tr -d ' ') 项"
	@git diff --stat 2>/dev/null | tail -5
	@echo "跨 Agent 工作记忆（请先读最新一篇）：.workbuddy/memory/ 下按日期命名的日志"
	@echo "已安装版本：$$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' /Applications/Arca.app/Contents/Info.plist 2>/dev/null || echo 未安装)"
	@echo "================ 快速门禁 ================"
	@node --check src/arca/web/app.js && echo "app.js 语法 OK"
	@$(PY) scripts/design_audit.py | tail -1

setup:
	python3 -m venv $(ARCA_BUILD_DIR)/venv
	$(ARCA_BUILD_DIR)/venv/bin/pip install pyobjc-framework-Cocoa pyobjc-framework-WebKit

thirdparty:
	$(PY) scripts/build_thirdparty.py

bundle:
	ARCA_PYTHON=$(ARCA_BUILD_DIR)/python-runtime ARCA_OUT=$$PWD/release/Arca.app $(PY) scripts/bundle.py

package:
	$(PY) scripts/package.py

publish:
	$(PY) scripts/publish.py

test:
	$(PY) scripts/all_test.py

test-fast:
	node --check src/arca/web/app.js
	$(PY) scripts/design_audit.py
	HOME=$$(mktemp -d) $(PY) scripts/selftest.py
	HOME=$$(mktemp -d) $(PY) scripts/progress_test.py

audit:
	$(PY) scripts/design_audit.py

# Local pre-flight gate: static checks + the fast, dependency-light suites.
# `make test` remains the authoritative full signal; this only shortens the
# loop while editing.  No CI claim is made from this target.
verify:
	node --check src/arca/web/app.js
	$(PY) scripts/design_audit.py
	$(PY) scripts/jobwait_test.py
	$(PY) scripts/menu_cleanup_test.py
	$(PY) scripts/partial_extract_test.py
	$(PY) scripts/fuzz_soak_test.py
	$(PY) scripts/zip_inspect_test.py
	$(PY) scripts/extract_list_test.py

shots:
	$(PY) scripts/shots.py

# 匿名使用统计：拉 COS 访问日志 → 聚合 → 上传 stats.json 数据接口
# （接口契约见 docs/STATS-API.md；凭据在 $(ARCA_BUILD_DIR)/cos.json，--sample 可离线验证）
stats:
	$(PY) scripts/stats.py
