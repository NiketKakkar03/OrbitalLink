# External module requires the exact built Linux tree pinned by guest/versions.json.
ifeq ($(KERNELRELEASE),)
CC ?= cc
KDIR ?=
.PHONY: host-check module guest-tests clean
host-check:
	$(CC) -std=c11 -Wall -Wextra -Werror -I. tests/host_contract.c -o /tmp/orbit-host-contract
	/tmp/orbit-host-contract
module:
	@test -n "$(KDIR)" || { echo 'Set KDIR to the built pinned Linux tree'; exit 1; }
	$(MAKE) -C $(KDIR) M=$(CURDIR) ORBIT_TEST_BACKEND=1 modules
guest-tests:
	$(CC) -std=c11 -Wall -Wextra -Werror -I. tests/syscall_contract.c -o orbit-syscall-contract
clean:
	@if test -n "$(KDIR)"; then $(MAKE) -C $(KDIR) M=$(CURDIR) clean; fi
	$(RM) orbit-syscall-contract
else
ifeq ($(ORBIT_TEST_BACKEND),1)
obj-m += orbit_test.o
ifeq ($(ORBIT_KUNIT),1)
obj-m += orbit_queue_test.o
endif
else
$(error This module is test-only: build with ORBIT_TEST_BACKEND=1)
endif
endif
