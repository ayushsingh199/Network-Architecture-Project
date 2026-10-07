PY      ?= .venv/bin/python
PORT    ?= 9000

.PHONY: setup fmt lint typecheck test check serve demo conform hexdump

setup:            ## create .venv with the dev tools
	python3 -m venv .venv && .venv/bin/pip install -q -e '.[dev]'

fmt:              ## format the code
	$(PY) -m ruff format .

lint:             ## lint the code
	$(PY) -m ruff check .

typecheck:        ## strict static typing
	$(PY) -m mypy

test:             ## run the test suite
	$(PY) -m pytest -q

check: lint typecheck test

serve:            ## run the server on ./www
	./bserve ./www $(PORT)

demo:             ## fetch two files over ONE connection, hexdumping every frame
	./bcurl -v localhost:$(PORT)/index.html /about.html > /dev/null

conform:          ## run the interop checker against a running server
	./bconform localhost:$(PORT)/index.html

hexdump:          ## regenerate docs/capture.txt from a running server
	./bcurl -vv localhost:$(PORT)/hello.txt 2> docs/capture.txt
