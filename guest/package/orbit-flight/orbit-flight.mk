ORBIT_FLIGHT_VERSION = local
ORBIT_FLIGHT_SITE = $(BR2_EXTERNAL_ORBIT_PATH)/../flight
ORBIT_FLIGHT_SITE_METHOD = local
ORBIT_FLIGHT_LICENSE = MIT
ORBIT_FLIGHT_LICENSE_FILES = LICENSE
ORBIT_FLIGHT_DEPENDENCIES = openssl sqlite

define ORBIT_FLIGHT_BUILD_CMDS
	$(TARGET_MAKE_ENV) $(TARGET_CXX) $(TARGET_CPPFLAGS) $(TARGET_CXXFLAGS) \
		-std=c++20 -Wall -Wextra -Werror -pthread -I$(ORBIT_FLIGHT_SITE)/../driver/include -I$(ORBIT_FLIGHT_SITE)/../simulation \
		$(@D)/flight_service.cpp $(@D)/durable_outbox.cpp $(TARGET_LDFLAGS) -lssl -lcrypto -lsqlite3 \
		-o $(@D)/orbit-flight
endef

define ORBIT_FLIGHT_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/orbit-flight $(TARGET_DIR)/usr/bin/orbit-flight
endef

$(eval $(generic-package))
