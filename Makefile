.PHONY: check build run

check:
	python3 -m py_compile luvnotes_archive.py

build:
	docker compose build

run:
	docker compose run --rm luvnotes
