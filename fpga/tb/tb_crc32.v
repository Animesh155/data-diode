//
// tb_crc32.v — verifies the Verilog CRC unit against golden vectors
// generated from the Python reference model (frame.py).
//
// Vectors come from crc_vectors.vh, produced by gen_vectors.py. They include
// the canonical CRC-32/ISO-HDLC check value plus real batch datagrams
// (keyframe, delta, heartbeat, empty, edge values, max MTU-safe batch).
//
// Run:  make
//
`timescale 1ns / 1ps
`default_nettype none

module tb_crc32;

    reg         clk = 1'b0;
    reg         init = 1'b0;
    reg         data_valid = 1'b0;
    reg  [7:0]  data_in = 8'h00;
    wire [31:0] crc_out;

    crc32 dut (
        .clk        (clk),
        .init       (init),
        .data_valid (data_valid),
        .data_in    (data_in),
        .crc_out    (crc_out)
    );

    always #5 clk = ~clk;

    `include "crc_vectors.vh"

    integer v, i;
    integer errors = 0;

    initial begin
        $display("CRC-32/ISO-HDLC vs Python reference model");
        $display("----------------------------------------");
        #1;

        for (v = 0; v < NVEC; v = v + 1) begin
            // Start a new frame: reload the seed.
            @(negedge clk);
            init = 1'b1;
            data_valid = 1'b0;
            @(negedge clk);
            init = 1'b0;

            // Feed the CRC region one byte per clock.
            for (i = 0; i < vec_len[v]; i = i + 1) begin
                data_in = vec_bytes[vec_start[v] + i];
                data_valid = 1'b1;
                @(negedge clk);
            end
            data_valid = 1'b0;
            @(negedge clk);

            if (crc_out === vec_expected[v]) begin
                $display("  ok    vector %0d  %4d bytes  crc=%08h",
                         v, vec_len[v], crc_out);
            end else begin
                $display("  FAIL  vector %0d  %4d bytes  got=%08h want=%08h",
                         v, vec_len[v], crc_out, vec_expected[v]);
                errors = errors + 1;
            end
        end

        $display("----------------------------------------");
        if (errors == 0)
            $display("RESULT: ALL PASS (%0d vectors)", NVEC);
        else
            $display("RESULT: %0d FAILURE(S) of %0d", errors, NVEC);
        $finish;
    end

endmodule

`default_nettype wire
