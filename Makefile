.PHONY: test test-red test-blue test-green demo

test: test-red test-blue test-green

test-red:
	cd services/red && python3 -m pytest -q

test-blue:
	cd services/blue && GCP_PROJECT=local python3 -m pytest -q

test-green:
	cd services/green && python3 -m pytest -q

demo:
	cd services/red && python3 demo.py
