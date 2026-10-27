# Data diode FPGA — simulation build
#
# Verifies the Verilog CRC unit against golden vectors generated from the
# Python reference model. Board-independent: pure simulation, no toolchain
# beyond Icarus Verilog.
#
#   make vectors   regenerate golden vectors from frame.py
#   make           build + run the CRC testbench
#   make clean

IVERILOG := iverilog
VVP      := vvp
BUILD    := build

.PHONY: all crc validator fifo vectors clean

all: crc validator fifo

crc: $(BUILD)/tb_crc32
	@$(VVP) $(BUILD)/tb_crc32

validator: $(BUILD)/tb_frame_validator
	@$(VVP) $(BUILD)/tb_frame_validator

fifo: $(BUILD)/tb_fifo
	@$(VVP) $(BUILD)/tb_fifo

vectors:
	@python3 gen_vectors.py

$(BUILD)/tb_crc32: rtl/crc32.v tb/tb_crc32.v tb/crc_vectors.vh
	@mkdir -p $(BUILD)
	@$(IVERILOG) -g2012 -Wall -I tb -o $@ rtl/crc32.v tb/tb_crc32.v

$(BUILD)/tb_frame_validator: rtl/crc32.v rtl/tag_policy.v rtl/frame_validator.v tb/tb_frame_validator.v tb/frame_vectors.vh tb/tag_policy_table.vh
	@mkdir -p $(BUILD)
	@$(IVERILOG) -g2012 -Wall -I tb -o $@ rtl/crc32.v rtl/tag_policy.v rtl/frame_validator.v tb/tb_frame_validator.v

$(BUILD)/tb_fifo: rtl/fifo.v tb/tb_fifo.v
	@mkdir -p $(BUILD)
	@$(IVERILOG) -g2012 -Wall -I tb -o $@ rtl/fifo.v tb/tb_fifo.v

clean:
	@rm -rf $(BUILD)
