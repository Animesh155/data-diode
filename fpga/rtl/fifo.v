//
// fifo.v — synchronous FIFO for the data diode pump.
//
// Sits between the frame validator (write side) and the one-way output
// driver (read side). Models the FPGA's internal block-RAM buffer.
//
// Diode-specific behaviour — READ CAREFULLY:
//
//   * TAIL-DROP ON OVERFLOW, NEVER BACKPRESSURE.
//     A data diode has no reverse channel. There is physically no way to
//     tell the sender "slow down". So when the FIFO is full, a new write is
//     DROPPED (and counted) rather than stalling. Asserting a "full → wait"
//     handshake would be a lie: nothing upstream can act on it. This mirrors
//     the Phase-1 Python fpga_sim, which also tail-drops.
//
//   * The drop is silent to the sender and observable only via overflow_cnt,
//     which the pump can surface as a health metric (analogous to the IT
//     proxy's loss counters). Lost frames self-heal via the OT proxy's
//     keyframe refresh — exactly the same recovery story as wire loss.
//
// This is a first-word-fall-through style interface: when empty is low,
// rd_data already holds the head element; assert rd_en for one cycle to pop.
//
`timescale 1ns / 1ps
`default_nettype none

module fifo #(
    parameter WIDTH = 8,
    parameter DEPTH = 512,                    // must be a power of two
    parameter AW    = $clog2(DEPTH)           // derived — do not override
) (
    input  wire              clk,
    input  wire              rst,

    // Write side (from validator)
    input  wire              wr_en,
    input  wire [WIDTH-1:0]  wr_data,

    // Read side (to output driver)
    input  wire              rd_en,
    output wire [WIDTH-1:0]  rd_data,

    // Status
    output wire              empty,
    output wire              full,
    output reg  [31:0]       overflow_cnt      // frames/bytes tail-dropped
);

    reg [WIDTH-1:0] mem [0:DEPTH-1];
    reg [AW:0]      wr_ptr = 0;                // extra MSB distinguishes
    reg [AW:0]      rd_ptr = 0;                // full from empty

    wire do_write = wr_en && !full;
    wire do_read  = rd_en && !empty;

    assign empty = (wr_ptr == rd_ptr);
    assign full  = (wr_ptr[AW-1:0] == rd_ptr[AW-1:0]) &&
                   (wr_ptr[AW]     != rd_ptr[AW]);

    // First-word-fall-through: head is continuously presented.
    assign rd_data = mem[rd_ptr[AW-1:0]];

    always @(posedge clk) begin
        if (rst) begin
            wr_ptr       <= 0;
            rd_ptr       <= 0;
            overflow_cnt <= 0;
        end else begin
            if (do_write) begin
                mem[wr_ptr[AW-1:0]] <= wr_data;
                wr_ptr <= wr_ptr + 1'b1;
            end else if (wr_en && full) begin
                // Tail-drop: no room, no backpressure. Count it and move on.
                overflow_cnt <= overflow_cnt + 1'b1;
            end

            if (do_read)
                rd_ptr <= rd_ptr + 1'b1;
        end
    end

endmodule

`default_nettype wire
