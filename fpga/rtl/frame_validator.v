//
// frame_validator.v — batch datagram validator for the data diode pump.
//
// This is the FPGA's entire understanding of the payload: find the frame,
// check it is well-formed, check CRC. It never interprets records — the
// diode stays protocol-agnostic by design (see WIRE_FORMAT.md).
//
// Wire format (v2 batch):
//   off  size  field
//    0    2    SYNC       0xAA55            (not covered by CRC)
//    2    1    VERSION    0x02
//    3    1    FLAGS      bit0 KEYFRAME, bit1 HEARTBEAT
//    4    4    SEQ        uint32
//    8    4    TIMESTAMP  uint32
//   12    2    COUNT      uint16
//   14   7*N   RECORDS    tag(2) value(4) quality(1)
//  14+7N  4    CRC-32     over bytes [2 .. 14+7N)
//
// A frame is ACCEPTED only if all of:
//   * SYNC matched
//   * VERSION == 0x02
//   * COUNT <= MAX_RECORDS       (MTU-safe bound; also the crafted-COUNT guard)
//   * computed CRC == received CRC
// Anything else is dropped silently. There is no error correction and no
// reverse path on which to request a retransmit — a bad frame simply never
// reaches the output.
//
// Streaming interface: one byte per clock on s_valid/s_data. On completion
// frame_done pulses for one cycle with frame_accept indicating the verdict.
//
`timescale 1ns / 1ps
`default_nettype none

module frame_validator #(
    parameter [15:0] MAX_RECORDS = 16'd200,
    parameter        N_TAGS      = 16
) (
    input  wire        clk,
    input  wire        rst,

    // Byte stream in (from the MAC / PHY side)
    input  wire        s_valid,
    input  wire [7:0]  s_data,

    // Result strobe
    output reg         frame_done,    // 1-cycle pulse when a frame resolves
    output reg         frame_accept,  // qualified by frame_done
    output reg [31:0]  frame_seq,
    output reg [15:0]  frame_count,
    output reg [7:0]   frame_flags
);

    localparam [7:0]  SYNC_HI      = 8'hAA;
    localparam [7:0]  SYNC_LO      = 8'h55;
    localparam [7:0]  BATCH_VERSION = 8'h02;
    localparam [3:0]  HDR_REMAIN   = 4'd12;   // header bytes after SYNC
    localparam [2:0]  RECORD_SIZE  = 3'd7;

    localparam [2:0] S_SYNC1   = 3'd0;
    localparam [2:0] S_SYNC2   = 3'd1;
    localparam [2:0] S_HEADER  = 3'd2;
    localparam [2:0] S_RECORDS = 3'd3;
    localparam [2:0] S_CRC     = 3'd4;

    reg [2:0]  state = S_SYNC1;
    reg [3:0]  hdr_idx;
    reg [15:0] rec_remain;
    reg [2:0]  crc_idx;

    // Record assembly, so each tag/value pair can be policy-checked.
    reg [2:0]  rec_byte;      // byte position within the current 7-byte record
    reg [15:0] rec_tag;
    reg [31:0] rec_val;

    reg [7:0]  version_r;
    reg [7:0]  flags_r;
    reg [31:0] seq_r;
    reg [15:0] count_r;
    reg [31:0] crc_rx;

    // ── CRC unit ────────────────────────────────────────────────
    // Seeded on the SYNC_LO byte (a cycle where no payload is fed), then
    // fed every byte from VERSION through the last record byte. The CRC
    // field itself is excluded.
    wire        crc_init  = s_valid && (state == S_SYNC2) && (s_data == SYNC_LO);
    wire        crc_feed  = s_valid && ((state == S_HEADER) || (state == S_RECORDS));
    wire [31:0] crc_calc;

    crc32 u_crc (
        .clk        (clk),
        .init       (crc_init),
        .data_valid (crc_feed),
        .data_in    (s_data),
        .crc_out    (crc_calc)
    );

    // ── Per-tag semantic policy ─────────────────────────────────
    // A record is complete on its 7th byte; rec_tag/rec_val are already
    // assembled by then, so the check fires combinationally in that cycle.
    wire policy_check = s_valid && (state == S_RECORDS) && (rec_byte == 3'd6);
    wire policy_violation;

    tag_policy #(.N_TAGS(N_TAGS)) u_policy (
        .clk       (clk),
        .rst       (rst),
        .clear     (crc_init),          // same pulse that starts a new frame
        .check_en  (policy_check),
        .tag_id    (rec_tag),
        .value     (rec_val),
        .violation (policy_violation)
    );

    // Header field capture is positional; hdr_idx counts bytes after SYNC.
    wire hdr_last = (hdr_idx == (HDR_REMAIN - 1));

    always @(posedge clk) begin
        frame_done   <= 1'b0;
        frame_accept <= 1'b0;

        if (rst) begin
            state <= S_SYNC1;
        end else if (s_valid) begin
            case (state)

                // ── Hunt for SYNC ───────────────────────────────
                S_SYNC1: begin
                    if (s_data == SYNC_HI)
                        state <= S_SYNC2;
                end

                S_SYNC2: begin
                    if (s_data == SYNC_LO) begin
                        state   <= S_HEADER;
                        hdr_idx <= 4'd0;
                    end else if (s_data == SYNC_HI) begin
                        // 0xAA 0xAA — the second may still open a frame.
                        state <= S_SYNC2;
                    end else begin
                        state <= S_SYNC1;
                    end
                end

                // ── 12 header bytes after SYNC ──────────────────
                S_HEADER: begin
                    case (hdr_idx)
                        4'd0:  version_r     <= s_data;
                        4'd1:  flags_r       <= s_data;
                        4'd2:  seq_r[31:24]  <= s_data;
                        4'd3:  seq_r[23:16]  <= s_data;
                        4'd4:  seq_r[15:8]   <= s_data;
                        4'd5:  seq_r[7:0]    <= s_data;
                        // bytes 6..9 are TIMESTAMP — covered by CRC,
                        // not needed by the pump.
                        4'd10: count_r[15:8] <= s_data;
                        4'd11: count_r[7:0]  <= s_data;
                        default: ;
                    endcase

                    if (hdr_last) begin
                        // COUNT completes here; low byte is still in s_data.
                        if ((version_r != BATCH_VERSION) ||
                            ({count_r[15:8], s_data} > MAX_RECORDS)) begin
                            // Malformed or crafted COUNT — drop without
                            // ever sizing a buffer from the bad value.
                            frame_done   <= 1'b1;
                            frame_accept <= 1'b0;
                            frame_seq    <= seq_r;
                            frame_count  <= {count_r[15:8], s_data};
                            frame_flags  <= flags_r;
                            state        <= S_SYNC1;
                        end else if ({count_r[15:8], s_data} == 16'd0) begin
                            rec_remain <= 16'd0;
                            crc_idx    <= 3'd0;
                            state      <= S_CRC;
                        end else begin
                            rec_remain <= {count_r[15:8], s_data} * RECORD_SIZE;
                            rec_byte   <= 3'd0;
                            state      <= S_RECORDS;
                        end
                    end else begin
                        hdr_idx <= hdr_idx + 4'd1;
                    end
                end

                // ── Stream 7*COUNT record bytes through the CRC ─
                // Bytes are also assembled into tag/value so tag_policy can
                // bounds-check each reading as it goes past.
                S_RECORDS: begin
                    case (rec_byte)
                        3'd0: rec_tag[15:8]  <= s_data;
                        3'd1: rec_tag[7:0]   <= s_data;
                        3'd2: rec_val[31:24] <= s_data;
                        3'd3: rec_val[23:16] <= s_data;
                        3'd4: rec_val[15:8]  <= s_data;
                        3'd5: rec_val[7:0]   <= s_data;
                        default: ;   // byte 6 is quality; record ends here
                    endcase

                    if (rec_byte == 3'd6)
                        rec_byte <= 3'd0;
                    else
                        rec_byte <= rec_byte + 3'd1;

                    if (rec_remain == 16'd1) begin
                        crc_idx <= 3'd0;
                        state   <= S_CRC;
                    end
                    rec_remain <= rec_remain - 16'd1;
                end

                // ── 4 CRC bytes, then verdict ───────────────────
                S_CRC: begin
                    crc_rx <= {crc_rx[23:0], s_data};
                    if (crc_idx == 3'd3) begin
                        // Accept requires BOTH: frame well-formed (CRC) and
                        // every record well-behaved (tag_policy).
                        frame_done   <= 1'b1;
                        frame_accept <= ({crc_rx[23:0], s_data} == crc_calc)
                                        && !policy_violation;
                        frame_seq    <= seq_r;
                        frame_count  <= count_r;
                        frame_flags  <= flags_r;
                        state        <= S_SYNC1;
                    end else begin
                        crc_idx <= crc_idx + 3'd1;
                    end
                end

                default: state <= S_SYNC1;
            endcase
        end
    end

endmodule

`default_nettype wire
