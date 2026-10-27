//
// tag_policy.v — per-tag semantic validation for the diode pump.
//
// The frame validator proves a frame is well-FORMED. This module proves each
// record is well-BEHAVED: the tag is one we expect, and its value lies inside
// that tag's engineering limits.
//
// Why this exists: without it, the value field is 4 arbitrary bytes the
// hardware never questions. A compromised OT proxy could stuff any bit pattern
// into every record and each frame would still pass SYNC/COUNT/CRC. Bounding
// the value collapses most of that channel and rejects data that is physically
// impossible for the sensor it claims to come from.
//
// Three rejection causes, all enforced in gates at wire speed:
//   1. NaN / Infinity        — exponent all-ones; never a legitimate reading
//   2. Unknown tag_id        — WHITELIST: a tag not in the table is refused
//   3. Value outside [min,max] for that tag
//
// HONEST LIMIT: range checking REDUCES the covert channel, it does not close
// it. A tag bounded to [0,1000] still has many representable float32 values.
// Eliminating the channel entirely requires quantising values at the source
// (e.g. fixed-point to 0.1 precision) and validating that quantisation here.
//
// Float comparison: IEEE-754 float32 ordering is recovered with a monotonic
// map to unsigned. For positives, set the MSB; for negatives, invert all bits.
// Unsigned compare on the result then matches float compare exactly — no FPU
// required, just XOR and a comparator.
//
`timescale 1ns / 1ps
`default_nettype none

module tag_policy #(
    parameter N_TAGS = 16
) (
    input  wire        clk,
    input  wire        rst,
    input  wire        clear,      // pulse at frame start
    input  wire        check_en,   // pulse when a record is fully assembled
    input  wire [15:0] tag_id,
    input  wire [31:0] value,      // raw IEEE-754 float32 bits
    output reg         violation   // sticky for the current frame
);

    // Table of {tag, min, max}, generated from the same tag map the software
    // uses so hardware and software limits cannot drift apart.
    reg [15:0] tbl_tag [0:N_TAGS-1];
    reg [31:0] tbl_min [0:N_TAGS-1];
    reg [31:0] tbl_max [0:N_TAGS-1];

    `include "tag_policy_table.vh"

    // IEEE-754 -> unsigned key preserving order.
    function [31:0] float_key;
        input [31:0] f;
        begin
            if (f[31])
                float_key = ~f;                 // negative: invert
            else
                float_key = {1'b1, f[30:0]};    // positive: set MSB
        end
    endfunction

    // Exponent all ones => NaN or +/-Inf. Never a valid sensor reading.
    wire is_nan_inf = (value[30:23] == 8'hFF);

    // Parallel associative match. Fine for the tens-of-tags case; a deployment
    // with thousands would need a hashed or sorted lookup instead.
    integer i;
    reg        found;
    reg [31:0] lo, hi;
    always @* begin
        found = 1'b0;
        lo    = 32'h0;
        hi    = 32'h0;
        for (i = 0; i < N_TAGS; i = i + 1) begin
            if (tbl_tag[i] == tag_id) begin
                found = 1'b1;
                lo    = tbl_min[i];
                hi    = tbl_max[i];
            end
        end
    end

    wire [31:0] vkey     = float_key(value);
    wire        in_range = (vkey >= float_key(lo)) && (vkey <= float_key(hi));

    always @(posedge clk) begin
        if (rst || clear)
            violation <= 1'b0;
        else if (check_en) begin
            if (is_nan_inf || !found || !in_range)
                violation <= 1'b1;
        end
    end

endmodule

`default_nettype wire
