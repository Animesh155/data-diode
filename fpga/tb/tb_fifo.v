//
// tb_fifo.v — exercises the diode buffer's semantics.
//
// The important test here is TAIL-DROP. A normal FIFO asserts backpressure
// when full and the writer waits. A diode FIFO must not: there is no reverse
// channel on which to signal "slow down", so an overflowing write is dropped
// and counted. This bench proves the drop happens, is counted, and — the part
// that actually matters — does NOT corrupt the frames already buffered.
//
// Runs with a small DEPTH so the full/overflow/wrap-around cases are reachable
// in a few hundred cycles. The pointer arithmetic under test is identical at
// the production depth.
//
// Run:  make fifo
//
`timescale 1ns / 1ps
`default_nettype none

module tb_fifo;

    localparam WIDTH = 8;
    localparam DEPTH = 8;

    reg              clk = 1'b0;
    reg              rst = 1'b1;
    reg              wr_en = 1'b0;
    reg  [WIDTH-1:0] wr_data = {WIDTH{1'b0}};
    reg              rd_en = 1'b0;
    wire [WIDTH-1:0] rd_data;
    wire             empty;
    wire             full;
    wire [31:0]      overflow_cnt;

    fifo #(.WIDTH(WIDTH), .DEPTH(DEPTH)) dut (
        .clk          (clk),
        .rst          (rst),
        .wr_en        (wr_en),
        .wr_data      (wr_data),
        .rd_en        (rd_en),
        .rd_data      (rd_data),
        .empty        (empty),
        .full         (full),
        .overflow_cnt (overflow_cnt)
    );

    always #5 clk = ~clk;

    integer errors = 0;
    integer i;
    reg [WIDTH-1:0] got;

    task check(input cond, input [511:0] msg);
        begin
            if (cond) begin
                $display("  ok    %0s", msg);
            end else begin
                $display("  FAIL  %0s", msg);
                errors = errors + 1;
            end
        end
    endtask

    // Same, for checks inside a loop. Avoids $sformatf, which iverilog will
    // not implicitly cast into a reg vector.
    task check_idx(input cond, input [511:0] msg, input integer idx);
        begin
            if (cond) begin
                $display("  ok    %0s %0d", msg, idx);
            end else begin
                $display("  FAIL  %0s %0d", msg, idx);
                errors = errors + 1;
            end
        end
    endtask

    // One write attempt. If the FIFO is full this is the tail-drop path.
    task push(input [WIDTH-1:0] d);
        begin
            wr_data = d;
            wr_en   = 1'b1;
            @(negedge clk);
            wr_en   = 1'b0;
        end
    endtask

    // First-word-fall-through: the head is already on rd_data, so capture
    // it before asserting rd_en to advance.
    task pop;
        begin
            got   = rd_data;
            rd_en = 1'b1;
            @(negedge clk);
            rd_en = 1'b0;
        end
    endtask

    initial begin
        $display("fifo - diode buffer semantics");
        $display("------------------------------");

        @(negedge clk);
        rst = 1'b1;
        @(negedge clk);
        rst = 1'b0;

        // ── Reset state ────────────────────────────────────────
        check(empty === 1'b1,        "reset: empty asserted");
        check(full  === 1'b0,        "reset: full deasserted");
        check(overflow_cnt === 32'd0, "reset: overflow_cnt zero");

        // ── Single element, first-word-fall-through ────────────
        push(8'hA5);
        check(empty === 1'b0,     "one write: not empty");
        check(rd_data === 8'hA5,  "FWFT: head visible without rd_en");
        pop;
        check(got === 8'hA5,      "read back 0xA5");
        check(empty === 1'b1,     "drained: empty again");

        // ── FIFO ordering ──────────────────────────────────────
        for (i = 0; i < 5; i = i + 1) push(8'h10 + i[7:0]);
        for (i = 0; i < 5; i = i + 1) begin
            pop;
            check_idx(got === (8'h10 + i[7:0]),
                      "ordering: in-order element", i);
        end
        check(empty === 1'b1, "ordering: drained");

        // ── Fill to capacity ───────────────────────────────────
        for (i = 0; i < DEPTH; i = i + 1) push(8'hC0 + i[7:0]);
        check(full  === 1'b1, "full asserted after DEPTH writes");
        check(empty === 1'b0, "full: not empty");

        // ── TAIL-DROP: the diode-critical behaviour ────────────
        push(8'hEE);
        push(8'hEE);
        push(8'hEE);
        check(overflow_cnt === 32'd3, "tail-drop: 3 overflows counted");
        check(full === 1'b1,          "tail-drop: still full, no stall");

        // The buffered frames must be untouched by the dropped writes.
        for (i = 0; i < DEPTH; i = i + 1) begin
            pop;
            check_idx(got === (8'hC0 + i[7:0]),
                      "tail-drop left buffered byte intact:", i);
        end
        check(empty === 1'b1, "tail-drop: drained cleanly");

        // ── Pointer wrap-around ────────────────────────────────
        // Pointers have now passed DEPTH; repeat a full cycle to prove the
        // extra MSB correctly distinguishes full from empty after wrapping.
        for (i = 0; i < DEPTH; i = i + 1) push(8'h50 + i[7:0]);
        check(full === 1'b1, "wrap: full asserted after wrap");
        for (i = 0; i < DEPTH; i = i + 1) begin
            pop;
            check_idx(got === (8'h50 + i[7:0]),
                      "wrap: byte intact across wrap:", i);
        end
        check(empty === 1'b1, "wrap: drained");

        // ── Simultaneous read and write ────────────────────────
        push(8'h11);
        push(8'h22);
        wr_data = 8'h33;
        wr_en   = 1'b1;
        got     = rd_data;
        rd_en   = 1'b1;
        @(negedge clk);
        wr_en = 1'b0;
        rd_en = 1'b0;
        check(got === 8'h11,     "simultaneous: popped 0x11");
        check(rd_data === 8'h22, "simultaneous: head advanced to 0x22");
        pop;
        check(got === 8'h22, "simultaneous: popped 0x22");
        pop;
        check(got === 8'h33, "simultaneous: popped 0x33 written that cycle");
        check(empty === 1'b1, "simultaneous: drained");

        // ── Read while empty is a no-op ────────────────────────
        rd_en = 1'b1;
        @(negedge clk);
        rd_en = 1'b0;
        check(empty === 1'b1, "read-when-empty: still empty");
        push(8'h77);
        pop;
        check(got === 8'h77, "read-when-empty: pointers uncorrupted");

        // ── Reset clears buffered data and counters ────────────
        for (i = 0; i < DEPTH + 2; i = i + 1) push(8'hAB);
        check(overflow_cnt !== 32'd0, "pre-reset: overflows accumulated");
        rst = 1'b1;
        @(negedge clk);
        rst = 1'b0;
        check(empty === 1'b1,         "post-reset: empty");
        check(full  === 1'b0,         "post-reset: not full");
        check(overflow_cnt === 32'd0, "post-reset: overflow_cnt cleared");

        $display("------------------------------");
        if (errors == 0)
            $display("RESULT: ALL PASS");
        else
            $display("RESULT: %0d FAILURE(S)", errors);
        $finish;
    end

endmodule

`default_nettype wire
