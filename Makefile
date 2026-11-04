.PHONY: check run
check:
	.venv/bin/ruff check .
	.venv/bin/xenon --max-absolute B --max-modules A --max-average A --exclude '.venv/*,*/migrations/*' .
	.venv/bin/pytest -q
run:
	.venv/bin/python manage.py migrate && .venv/bin/python manage.py runserver
