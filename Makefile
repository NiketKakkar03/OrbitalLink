CXX ?= c++
CXXFLAGS ?= -std=c++20 -Wall -Wextra -Werror
OPENSSL_PREFIX ?= $(shell if [ -d /opt/homebrew/opt/openssl@3 ]; then printf /opt/homebrew/opt/openssl@3; elif [ -d /usr/local/opt/openssl@3 ]; then printf /usr/local/opt/openssl@3; fi)
TLS_CXXFLAGS = $(if $(OPENSSL_PREFIX),-I$(OPENSSL_PREFIX)/include)
TLS_LDLIBS = $(if $(OPENSSL_PREFIX),-L$(OPENSSL_PREFIX)/lib) -lssl -lcrypto -lsqlite3
PYTHON ?= $(shell if [ -x .venv/bin/python ]; then printf '.venv/bin/python'; else printf 'python3'; fi)

.PHONY: check test-python test-cpp test-native-e2e test-recovery test-security test-sanitizers test-tsan clean

check: test-python test-cpp test-native-e2e test-recovery test-durable test-security test-harness test-foundations

.PHONY: test-durable
test-durable:
	$(PYTHON) -m pytest ground/test_durable_e2e.py

test-security:
	$(PYTHON) -m pytest ground/test_security_e2e.py ground/test_command_e2e.py ground/test_load_e2e.py

.PHONY: test-security-sanitizers
test-security-sanitizers:
	ORBIT_TEST_SANITIZERS=address,undefined $(PYTHON) -m pytest ground/test_security_e2e.py ground/test_command_e2e.py ground/test_load_e2e.py

test-python:
	$(PYTHON) -m json.tool protocol/fixtures/telemetry_sample_v1.json >/dev/null
	$(PYTHON) -m json.tool protocol/fixtures/telemetry_ack_v1.json >/dev/null
	$(PYTHON) -m pytest ground/test_temperature.py ground/test_protocol.py ground/test_storage.py ground/test_command_broker.py ground/test_command_cli.py ground/test_observability.py ground/test_sensor_telemetry.py

test-native-e2e:
	$(PYTHON) -m pytest ground/test_native_e2e.py

test-recovery:
	$(PYTHON) -m pytest ground/test_m3_e2e.py ground/test_window_e2e.py

test-cpp:
	$(CXX) $(CXXFLAGS) -pthread  flight/tests/test_sensor_source.cpp -o /tmp/orbit_sensor_source_test
	/tmp/orbit_sensor_source_test
	$(CXX) $(CXXFLAGS) flight/tests/test_scenario.cpp -o /tmp/orbit_scenario_test
	/tmp/orbit_scenario_test
	$(CXX) $(CXXFLAGS) flight/tests/test_sensor_protocol.cpp -o /tmp/orbit_sensor_protocol_test
	/tmp/orbit_sensor_protocol_test
	$(CXX) $(CXXFLAGS) -pthread  flight/tests/test_device_source.cpp -o /tmp/orbit_device_source_test
	/tmp/orbit_device_source_test
	$(CXX) $(CXXFLAGS) -pthread flight/durable_outbox_tests.cpp flight/durable_outbox.cpp -o /tmp/orbit_durable_outbox_test -lsqlite3
	/tmp/orbit_durable_outbox_test
	$(CXX) $(CXXFLAGS) -pthread flight/tests/test_command_policy.cpp -o /tmp/orbit_command_policy_test
	/tmp/orbit_command_policy_test
	$(CXX) $(CXXFLAGS) flight/tests/test_temperature.cpp -o /tmp/orbit_temperature_test
	/tmp/orbit_temperature_test
	$(CXX) $(CXXFLAGS) flight/tests/test_protocol.cpp -o /tmp/orbit_protocol_test
	/tmp/orbit_protocol_test
	$(CXX) $(CXXFLAGS) -pthread flight/tests/test_m3_policy.cpp -o /tmp/orbit_m3_policy_test
	/tmp/orbit_m3_policy_test
	$(CXX) $(CXXFLAGS) flight/send_sample.cpp -o /tmp/orbit_send_sample
	$(CXX) $(CXXFLAGS) $(TLS_CXXFLAGS) -pthread flight/flight_service.cpp flight/durable_outbox.cpp -o /tmp/orbit_flight_service $(TLS_LDLIBS)

test-sanitizers:
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=address,undefined flight/tests/test_scenario.cpp -o /tmp/orbit_scenario_sanitized
	/tmp/orbit_scenario_sanitized
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=address,undefined flight/tests/test_sensor_protocol.cpp -o /tmp/orbit_sensor_protocol_sanitized
	/tmp/orbit_sensor_protocol_sanitized
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=address,undefined flight/tests/test_sensor_source.cpp -o /tmp/orbit_sensor_source_sanitized
	/tmp/orbit_sensor_source_sanitized
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=address,undefined flight/tests/test_device_source.cpp -o /tmp/orbit_device_source_sanitized
	/tmp/orbit_device_source_sanitized
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=address,undefined flight/durable_outbox_tests.cpp flight/durable_outbox.cpp -o /tmp/orbit_durable_outbox_sanitized -lsqlite3
	/tmp/orbit_durable_outbox_sanitized
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=address,undefined \
		flight/tests/test_command_policy.cpp -o /tmp/orbit_command_policy_sanitized
	/tmp/orbit_command_policy_sanitized
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=address,undefined \
		flight/tests/test_m3_policy.cpp -o /tmp/orbit_m3_policy_sanitized
	/tmp/orbit_m3_policy_sanitized
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=address,undefined \
		-DORBIT_TEST_PLAINTEXT flight/flight_service.cpp flight/durable_outbox.cpp -o /tmp/orbit_flight_service_sanitized -lsqlite3
	/tmp/orbit_flight_service_sanitized 127.0.0.1 1 --count 10000 \
		--interval-ms 0 --capacity 32 --connect-timeout-ms 10 --io-timeout-ms 10 --drain-ms 25

test-tsan:
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=thread flight/tests/test_scenario.cpp -o /tmp/orbit_scenario_tsan
	/tmp/orbit_scenario_tsan
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=thread flight/tests/test_sensor_protocol.cpp -o /tmp/orbit_sensor_protocol_tsan
	/tmp/orbit_sensor_protocol_tsan
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=thread flight/tests/test_sensor_source.cpp -o /tmp/orbit_sensor_source_tsan
	/tmp/orbit_sensor_source_tsan
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=thread flight/tests/test_device_source.cpp -o /tmp/orbit_device_source_tsan
	/tmp/orbit_device_source_tsan
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=thread flight/durable_outbox_tests.cpp flight/durable_outbox.cpp -o /tmp/orbit_durable_outbox_tsan -lsqlite3
	/tmp/orbit_durable_outbox_tsan
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=thread \
		flight/tests/test_command_policy.cpp -o /tmp/orbit_command_policy_tsan
	/tmp/orbit_command_policy_tsan
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=thread \
		flight/tests/test_m3_policy.cpp -o /tmp/orbit_m3_policy_tsan
	/tmp/orbit_m3_policy_tsan
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=thread \
		-DORBIT_TEST_PLAINTEXT flight/flight_service.cpp flight/durable_outbox.cpp -o /tmp/orbit_flight_service_tsan -lsqlite3
	/tmp/orbit_flight_service_tsan 127.0.0.1 1 --count 10000 \
		--interval-ms 0 --capacity 32 --connect-timeout-ms 10 --io-timeout-ms 10 --drain-ms 25

clean:
	rm -f /tmp/orbit_command_policy_test /tmp/orbit_command_policy_sanitized /tmp/orbit_command_policy_tsan
	rm -f /tmp/orbit_temperature_test /tmp/orbit_protocol_test /tmp/orbit_m3_policy_test
	rm -f /tmp/orbit_send_sample /tmp/orbit_flight_service /tmp/orbit_m3_policy_sanitized
	rm -f /tmp/orbit_m3_policy_tsan /tmp/orbit_flight_service_sanitized
	rm -f /tmp/orbit_flight_service_tsan

.PHONY: doctor demo-native test-harness
doctor:
	$(PYTHON) scripts/doctor.py

demo-native: test-cpp
	$(PYTHON) integration/demo_native.py --flight /tmp/orbit_flight_service

test-harness:
	$(PYTHON) -m pytest integration/test_run_checks.py

.PHONY: test-foundations
test-foundations:
	$(PYTHON) -m pytest guest/tests/test_recipe.py
	$(MAKE) -C driver host-check
