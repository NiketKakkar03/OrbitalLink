# Custom device is built into the pinned host QEMU, never the Mac host kernel.
define ORBIT_QEMU_PATCH
	python3 $(BR2_EXTERNAL_ORBIT_PATH)/../qemu/apply.py $(@D)
endef
HOST_QEMU_POST_PATCH_HOOKS += ORBIT_QEMU_PATCH
