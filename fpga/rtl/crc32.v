//
// crc32.v — CRC-32/ISO-HDLC unit for the data diode frame validator.
//
// Bit-exact with the Python reference model (frame.py, which uses
// zlib.crc32). Parameters are pinned in phase1_env/docs/WIRE_FORMAT.md:
//
//   polynomial      0x04C11DB7   (reflected form 0xEDB88320)
//   init            0xFFFFFFFF
//   refin / refout  true / true
//   final XOR       0xFFFFFFFF
//   check("123456789") = 0xCBF43926
//
// Because refin/refout are both true, the reflected (right-shifting) form is
// used directly — no bit-reversal of input bytes or of the result is needed.
//
// Throughput: one byte per clock. The eight bit-steps are unrolled
// combinationally, which synthesises to a small XOR tree.
//
// Usage (per frame):
//   1. Pulse `init` high for one cycle before the first byte.
//   2. Drive `data_valid` with each payload byte on `data_in`.
//   3. `crc_out` is continuously valid; sample it after the last byte.
//
// For the diode the CRC region is bytes [2 .. 14+7*COUNT) of the datagram:
// VERSION through the last record. SYNC and the CRC field are excluded.
//
`timescale 1ns / 1ps
`default_nettype none

module crc32 (
    input  wire        clk,
    input  wire        init,        // sync: reload seed for a new frame
    input  wire        data_valid,  // data_in holds a valid byte this cycle
    input  wire [7:0]  data_in,
    output wire [31:0] crc_out      // running CRC, final XOR applied
);

    localparam [31:0] CRC_POLY   = 32'hEDB88320;  // reflected 0x04C11DB7
    localparam [31:0] CRC_INIT   = 32'hFFFFFFFF;
    localparam [31:0] CRC_XOROUT = 32'hFFFFFFFF;

    reg [31:0] crc_reg = CRC_INIT;

    // One byte folded into the register: XOR the byte into the low bits,
    // then eight conditional shift/XOR steps.
    function [31:0] crc32_byte;
        input [31:0] crc_in;
        input [7:0]  data;
        reg   [31:0] c;
        integer      i;
        begin
            c = crc_in ^ {24'b0, data};
            for (i = 0; i < 8; i = i + 1) begin
                if (c[0])
                    c = {1'b0, c[31:1]} ^ CRC_POLY;
                else
                    c = {1'b0, c[31:1]};
            end
            crc32_byte = c;
        end
    endfunction

    always @(posedge clk) begin
        if (init)
            crc_reg <= CRC_INIT;
        else if (data_valid)
            crc_reg <= crc32_byte(crc_reg, data_in);
    end

    assign crc_out = crc_reg ^ CRC_XOROUT;

endmodule

`default_nettype wire
