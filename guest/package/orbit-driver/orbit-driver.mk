ORBIT_DRIVER_VERSION = local
ORBIT_DRIVER_SITE = $(BR2_EXTERNAL_ORBIT_PATH)/../driver
ORBIT_DRIVER_SITE_METHOD = local
ORBIT_DRIVER_LICENSE = GPL-2.0
ORBIT_DRIVER_LICENSE_FILES = LICENSE
ORBIT_DRIVER_MODULE_MAKE_OPTS = ORBIT_TEST_BACKEND=1

define ORBIT_DRIVER_BUILD_CMDS
	$(TARGET_MAKE_ENV) $(TARGET_CC) $(TARGET_CPPFLAGS) $(TARGET_CFLAGS) \
		-std=c11 -Wall -Wextra -Werror -I$(@D) \
		$(@D)/tests/syscall_contract.c -o $(@D)/orbit-syscall-contract
	$(TARGET_MAKE_ENV) $(TARGET_CC) $(TARGET_CPPFLAGS) $(TARGET_CFLAGS) \
		-std=c11 -Wall -Wextra -Werror -I$(@D) \
		$(@D)/tests/sensors_contract.c -o $(@D)/orbit-sensors-contract
endef

define ORBIT_DRIVER_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/orbit-syscall-contract \
		$(TARGET_DIR)/usr/bin/orbit-syscall-contract
	$(INSTALL) -D -m 0755 $(@D)/tests/run_guest_tests.sh \
		$(TARGET_DIR)/usr/bin/orbit-driver-tests
	$(INSTALL) -D -m 0755 $(@D)/orbit-sensors-contract \
		$(TARGET_DIR)/usr/bin/orbit-sensors-contract
endef

$(eval $(kernel-module))
$(eval $(generic-package))
