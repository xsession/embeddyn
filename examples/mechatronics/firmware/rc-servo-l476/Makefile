# Builds ../rc-servo-l476.elf with arm-none-eabi-gcc (no SDK needed).
CC = arm-none-eabi-gcc
CFLAGS = -mcpu=cortex-m4 -mthumb -mfpu=fpv4-sp-d16 -mfloat-abi=hard -O2 -g -Wall -Wextra -ffreestanding -ffunction-sections
LDFLAGS = -nostdlib -T link.ld -Wl,--gc-sections -Wl,-Map=build/rc-servo-l476.map
OUT = ../rc-servo-l476.elf

# Some distro packages (e.g. Ubuntu 24.04 binutils-arm-none-eabi) lack the
# <prefix>/arm-none-eabi/bin/{as,ld} links gcc searches; provide them via -B.
TOOLBIN = build/toolbin

$(OUT): main.c startup.c link.ld $(TOOLBIN)/as
	$(CC) -B$(TOOLBIN)/ $(CFLAGS) $(LDFLAGS) -o $@ main.c startup.c -lgcc

$(TOOLBIN)/as:
	mkdir -p $(TOOLBIN)
	ln -sf "$$(command -v arm-none-eabi-as)" $(TOOLBIN)/as
	ln -sf "$$(command -v arm-none-eabi-ld)" $(TOOLBIN)/ld

clean:
	rm -f $(OUT) build/rc-servo-l476.map $(TOOLBIN)/as $(TOOLBIN)/ld
.PHONY: clean
