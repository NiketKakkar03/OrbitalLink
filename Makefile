CXX ?= c++
CXXFLAGS ?= -std=c++20 -Wall -Wextra -Werror
PYTHON ?= $(shell if [ -x .venv/bin/python ]; then printf '.venv/bin/python'; else printf 'python3'; fi)

.PHONY: check test-python test-cpp test-native-e2e test-recovery test-sanitizers test-tsan clean

check: test-python test-cpp test-native-e2e test-recovery

test-python:
	$(PYTHON) -m json.tool protocol/fixtures/telemetry_sample_v1.json >/dev/null
	$(PYTHON) -m json.tool protocol/fixtures/telemetry_ack_v1.json >/dev/null
	$(PYTHON) -m pytest ground/test_temperature.py ground/test_protocol.py ground/test_storage.py

test-native-e2e:
	$(PYTHON) -m pytest ground/test_native_e2e.py

test-recovery:
	$(PYTHON) -m pytest ground/test_m3_e2e.py

test-cpp:
	$(CXX) $(CXXFLAGS) flight/tests/test_temperature.cpp -o /tmp/orbit_temperature_test
	/tmp/orbit_temperature_test
	$(CXX) $(CXXFLAGS) flight/tests/test_protocol.cpp -o /tmp/orbit_protocol_test
	/tmp/orbit_protocol_test
	$(CXX) $(CXXFLAGS) -pthread flight/tests/test_m3_policy.cpp -o /tmp/orbit_m3_policy_test
	/tmp/orbit_m3_policy_test
	$(CXX) $(CXXFLAGS) flight/send_sample.cpp -o /tmp/orbit_send_sample
	$(CXX) $(CXXFLAGS) -pthread flight/flight_service.cpp -o /tmp/orbit_flight_service

test-sanitizers:
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=address,undefined \
		flight/tests/test_m3_policy.cpp -o /tmp/orbit_m3_policy_sanitized
	/tmp/orbit_m3_policy_sanitized
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=address,undefined \
		flight/flight_service.cpp -o /tmp/orbit_flight_service_sanitized
	/tmp/orbit_flight_service_sanitized 127.0.0.1 1 --count 10000 \
		--interval-ms 0 --capacity 32 --connect-timeout-ms 10 --io-timeout-ms 10 --drain-ms 25

test-tsan:
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=thread \
		flight/tests/test_m3_policy.cpp -o /tmp/orbit_m3_policy_tsan
	/tmp/orbit_m3_policy_tsan
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=thread \
		flight/flight_service.cpp -o /tmp/orbit_flight_service_tsan
	/tmp/orbit_flight_service_tsan 127.0.0.1 1 --count 10000 \
		--interval-ms 0 --capacity 32 --connect-timeout-ms 10 --io-timeout-ms 10 --drain-ms 25

clean:
	rm -f /tmp/orbit_temperature_test /tmp/orbit_protocol_test /tmp/orbit_m3_policy_test
	rm -f /tmp/orbit_send_sample /tmp/orbit_flight_service /tmp/orbit_m3_policy_sanitized
	rm -f /tmp/orbit_m3_policy_tsan /tmp/orbit_flight_service_sanitized
	rm -f /tmp/orbit_flight_service_tsan
