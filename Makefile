CXX ?= c++
CXXFLAGS ?= -std=c++20 -Wall -Wextra -Werror
PYTHON ?= $(shell if [ -x .venv/bin/python ]; then printf '.venv/bin/python'; else printf 'python3'; fi)

.PHONY: check test-python test-cpp clean

check: test-python test-cpp

test-python:
	$(PYTHON) -m json.tool protocol/fixtures/telemetry_sample_v1.json >/dev/null
	$(PYTHON) -m pytest ground/test_temperature.py ground/test_protocol.py

test-cpp:
	$(CXX) $(CXXFLAGS) flight/tests/test_temperature.cpp -o /tmp/orbit_temperature_test
	/tmp/orbit_temperature_test
	$(CXX) $(CXXFLAGS) flight/tests/test_protocol.cpp -o /tmp/orbit_protocol_test
	/tmp/orbit_protocol_test

clean:
	rm -f /tmp/orbit_temperature_test /tmp/orbit_protocol_test
