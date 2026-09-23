CXX ?= c++
CXXFLAGS ?= -std=c++20 -Wall -Wextra -Werror
PYTHON ?= $(shell if [ -x .venv/bin/python ]; then printf '.venv/bin/python'; else printf 'uv run --with pytest==8.3.4 python'; fi)

OPENSSL_PREFIX ?= /opt/homebrew
OPENSSL_CFLAGS ?= -I$(OPENSSL_PREFIX)/include
OPENSSL_LIBS ?= -L$(OPENSSL_PREFIX)/lib -lssl -lcrypto

.PHONY: check test-python test-cpp test-native-e2e test-recovery test-security test-commands-e2e test-sanitizers test-tsan certs clean

check: test-python test-cpp test-security test-native-e2e test-recovery test-commands-e2e

certs:
	bash tools/make_dev_certs.sh .local/certs

test-python:
	$(PYTHON) -m json.tool protocol/fixtures/telemetry_sample_v1.json >/dev/null
	$(PYTHON) -m json.tool protocol/fixtures/telemetry_ack_v1.json >/dev/null
	$(PYTHON) -m json.tool protocol/fixtures/command_v1.json >/dev/null
	$(PYTHON) -m pytest ground/test_temperature.py ground/test_protocol.py ground/test_storage.py ground/test_control.py

test-security:
	$(PYTHON) -m pytest ground/test_m4_security.py

test-commands-e2e:
	$(PYTHON) -m pytest ground/test_m4_commands_e2e.py

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
	$(CXX) $(CXXFLAGS) -pthread flight/tests/test_command_control.cpp -o /tmp/orbit_command_control_test
	/tmp/orbit_command_control_test
	$(CXX) $(CXXFLAGS) $(OPENSSL_CFLAGS) flight/send_sample.cpp \
		-o /tmp/orbit_send_sample $(OPENSSL_LIBS)
	$(CXX) $(CXXFLAGS) $(OPENSSL_CFLAGS) -pthread flight/flight_service.cpp \
		-o /tmp/orbit_flight_service $(OPENSSL_LIBS)

test-sanitizers:
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=address,undefined \
		flight/tests/test_m3_policy.cpp -o /tmp/orbit_m3_policy_sanitized
	/tmp/orbit_m3_policy_sanitized
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=address,undefined \
		flight/tests/test_command_control.cpp -o /tmp/orbit_command_control_sanitized
	/tmp/orbit_command_control_sanitized
	$(CXX) $(CXXFLAGS) $(OPENSSL_CFLAGS) -pthread -fsanitize=address,undefined \
		flight/flight_service.cpp -o /tmp/orbit_flight_service_sanitized \
		$(OPENSSL_LIBS)
	bash tools/make_dev_certs.sh /tmp/orbit-certs >/dev/null
	/tmp/orbit_flight_service_sanitized 127.0.0.1 1 --count 10000 \
		--interval-ms 0 --capacity 32 --connect-timeout-ms 10 --io-timeout-ms 10 --drain-ms 25 \
		--ca-file /tmp/orbit-certs/ca.cert.pem --client-cert /tmp/orbit-certs/client.cert.pem \
		--client-key /tmp/orbit-certs/client.key.pem --server-name orbital-ground

test-tsan:
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=thread \
		flight/tests/test_m3_policy.cpp -o /tmp/orbit_m3_policy_tsan
	/tmp/orbit_m3_policy_tsan
	$(CXX) $(CXXFLAGS) -pthread -fsanitize=thread \
		flight/tests/test_command_control.cpp -o /tmp/orbit_command_control_tsan
	/tmp/orbit_command_control_tsan
	$(CXX) $(CXXFLAGS) $(OPENSSL_CFLAGS) -pthread -fsanitize=thread \
		flight/flight_service.cpp -o /tmp/orbit_flight_service_tsan \
		$(OPENSSL_LIBS)
	bash tools/make_dev_certs.sh /tmp/orbit-certs >/dev/null
	/tmp/orbit_flight_service_tsan 127.0.0.1 1 --count 10000 \
		--interval-ms 0 --capacity 32 --connect-timeout-ms 10 --io-timeout-ms 10 --drain-ms 25 \
		--ca-file /tmp/orbit-certs/ca.cert.pem --client-cert /tmp/orbit-certs/client.cert.pem \
		--client-key /tmp/orbit-certs/client.key.pem --server-name orbital-ground

clean:
	rm -f /tmp/orbit_temperature_test /tmp/orbit_protocol_test /tmp/orbit_m3_policy_test
	rm -f /tmp/orbit_command_control_test
	rm -f /tmp/orbit_command_control_sanitized /tmp/orbit_command_control_tsan
	rm -f /tmp/orbit_send_sample /tmp/orbit_flight_service /tmp/orbit_m3_policy_sanitized
	rm -f /tmp/orbit_m3_policy_tsan /tmp/orbit_flight_service_sanitized
	rm -f /tmp/orbit_flight_service_tsan
