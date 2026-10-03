PY ?= python
install: ; $(PY) -m pip install -r requirements.txt
test: ; $(PY) -m pytest -q
e2e: ; PYTHONPATH=. $(PY) scripts/mcp_e2e.py
demo: ; $(PY) -m demo.app
mock: ; $(PY) -m mockstore.server
spec: ; $(PY) -m wooconnector spec > docs/mcp_tools.json
