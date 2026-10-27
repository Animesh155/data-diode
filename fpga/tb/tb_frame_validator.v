//
// tb_frame_validator.v — drives full batch datagrams through the validator
// and checks the accept/drop verdict against the Python reference model.
//
// Vectors (tb/frame_vectors.vh, from gen_vectors.py) cover both directions:
//   * accept — keyframe, delta, heartbeat, empty, max-size
//   * drop   — corrupt payload, corrupt CRC, bad VERSION, crafted COUNT
//   * stream — truncated frame yields NO verdict, resync after garbage,
//              two frames back-to-back
//
// Run:  make validator
//
`timescale 1ns / 1ps
`default_nettype none

module tb_frame_validator;

    reg        clk = 1'b0;
    reg        rst = 1'b1;
    reg        s_valid = 1'b0;
    reg  [7:0] s_data = 8'h00;

    wire        frame_done;
    wire        frame_accept;
    wire [31:0] frame_seq;
    wire [15:0] frame_count;
    wire [7:0]  frame_flags;

    frame_validator #(.N_TAGS(16)) dut (
        .clk          (clk),
        .rst          (rst),
        .s_valid      (s_valid),
        .s_data       (s_data),
        .frame_done   (frame_done),
        .frame_accept (frame_accept),
        .frame_seq    (frame_seq),
        .frame_count  (frame_count),
        .frame_flags  (frame_flags)
    );

    always #5 clk = ~clk;

    `include "frame_vectors.vh"

    integer v, i;
    integer errors = 0;
    integer seen_done, seen_accept;
    reg [31:0] last_accept_seq;

    // Count verdicts as they stream out of the DUT.
    always @(posedge clk) begin
        if (frame_done) begin
            seen_done = seen_done + 1;
            if (frame_accept) begin
                seen_accept = seen_accept + 1;
                last_accept_seq = frame_seq;
            end
        end
    end

    initial begin
        $display("frame_validator vs Python reference model");
        $display("-----------------------------------------");
        #1;

        for (v = 0; v < NFRM; v = v + 1) begin
            // Reset per vector so state never leaks between cases.
            @(negedge clk);
            rst = 1'b1;
            s_valid = 1'b0;
            seen_done = 0;
            seen_accept = 0;
            last_accept_seq = 32'hFFFFFFFF;
            @(negedge clk);
            rst = 1'b0;

            for (i = 0; i < frm_len[v]; i = i + 1) begin
                s_data = frm_bytes[frm_start[v] + i];
                s_valid = 1'b1;
                @(negedge clk);
            end
            s_valid = 1'b0;
            @(negedge clk);
            @(negedge clk);

            if (seen_done !== frm_done[v]) begin
                $display("  FAIL  [%0d] %0s: done=%0d want %0d",
                         v, frm_label[v], seen_done, frm_done[v]);
                errors = errors + 1;
            end else if (seen_accept !== frm_accept[v]) begin
                $display("  FAIL  [%0d] %0s: accept=%0d want %0d",
                         v, frm_label[v], seen_accept, frm_accept[v]);
                errors = errors + 1;
            end else if ((frm_accept[v] > 0) && (last_accept_seq !== frm_seq[v])) begin
                $display("  FAIL  [%0d] %0s: seq=%0d want %0d",
                         v, frm_label[v], last_accept_seq, frm_seq[v]);
                errors = errors + 1;
            end else begin
                $display("  ok    [%0d] %0s  (done=%0d accept=%0d)",
                         v, frm_label[v], seen_done, seen_accept);
            end
        end

        $display("-----------------------------------------");
        if (errors == 0)
            $display("RESULT: ALL PASS (%0d vectors)", NFRM);
        else
            $display("RESULT: %0d FAILURE(S) of %0d", errors, NFRM);
        $finish;
    end

endmodule

`default_nettype wire
