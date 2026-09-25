// Ring page writer: prehistory slots rotate until pinned, then post slots fill in order; committed pinned pages are never rewritten.
// Compact pages (compact-v1) hold a retirement run in rec until an input cannot extend it, then write it before anything
// else. Its first event reserved raw space, so seals still happen only when a raw record does not fit.
module trace_page_writer #(
    parameter int PAGE_BYTES = 1024,
    parameter int PAGES = 32
) (
    input  logic                                   clk_i,
    input  logic                                   rst_ni,
    input  logic                                   clear_i,
    input  logic                                   drain_i,
    input  logic                                   pin_i,
    input  logic                                   compact_i,
    input  logic [$clog2(PAGES):0]                 pre_pages_i,
    input  logic                                   sink_ready_i,
    input  logic [63:0]                            session_i,
    input  logic [63:0]                            config_tag_i,
    input  chronos_pkg::entry_t [3:0]              head_i,
    input  logic [3:0][63:0]                       epoch_i,
    input  logic [3:0]                             empty_i,
    input  logic [1:0]                             reset_i,
    output logic [3:0]                             pop_o,
    output logic                                   we_o,
    output logic [$clog2(PAGES*PAGE_BYTES/8)-1:0]  waddr_o,
    output logic [63:0]                            wdata_o,
    output logic [7:0]                             wbe_o,
    output logic                                   done_o,
    output logic                                   storage_full_o,
    output logic [$clog2(PAGES):0]                 pre_ptr_o,
    output logic [$clog2(PAGES):0]                 post_used_o,
    output logic                                   wrapped_o
);
    localparam int PAYLOAD = PAGE_BYTES - 64;
    localparam int AW      = $clog2(PAGES * PAGE_BYTES / 8);
    localparam int OW      = $clog2(PAGE_BYTES);

    typedef enum logic [2:0] {PICK, RECORD, TAIL, HEADER, DONE} state_e;

    state_e                  state;
    logic [1:0]              rr, pick;
    logic                    any, fits, odd, pair, last, load, room, page_open, storage_full, wrapped;
    logic                    writing, emit, run, starts, rlink, rdelta, bdelta, ext, rvalid, bvalid, bwrite;
    logic [31:0]             rec [16];
    logic [3:0]              rec_len, rec_idx;
    logic [2:0]              hstep;
    logic [63:0]             min_tick, gen, rseq, rtick, rbound, bseq, btick, dt, bdt;
    logic [63:0]             hdr [8];
    logic [OW-1:0]           ofs, plen, ofs_next, start;
    logic [31:0]             records, events, pcrc, hcrc, rpc, baddr;
    logic [32:0]             dpc, daddr;
    logic [7:0]              members;
    logic [8:0]              stride;
    logic [$clog2(PAGES):0]  slot, pre_ptr, post_used;
    logic [5:0]              len;
    logic [AW-1:0]           page_base;
    chronos_pkg::entry_t     head;

    always_comb begin
        pick = rr;
        any  = 1'b0;
        for (int i = 0; i < 4; i++) begin
            if (!any && !empty_i[rr + 2'(i)]) begin
                pick = rr + 2'(i);
                any  = 1'b1;
            end
        end
    end

    // Codec context (compact_encode._single/_extends): the last retirement and bus request on this page. A bus
    // response needs no clear: it takes the next source-1 sequence number, so no later request links past it.
    assign dt     = head.tick - rtick;
    assign bdt    = head.tick - btick;
    assign dpc    = {1'b0, head.payload[31:0]} - {1'b0, rpc};
    assign daddr  = {1'b0, head.payload[63:32]} - {1'b0, baddr};
    assign starts = compact_i && head.kind == 3'd1 && {1'b0, head.payload[63:32]} == {1'b0, head.payload[31:0]} + 33'd4;
    assign rlink  = compact_i && rvalid && head.kind == 3'd1 && head.seq == rseq + 64'd1 &&
                    head.payload[159:96] == rbound && dt != 64'd0;
    assign rdelta = rlink && dt < 64'd65536 && (&dpc[32:15] || ~|dpc[32:15]);
    assign bdelta = compact_i && bvalid && head.kind == 3'd5 && head.seq == bseq + 64'd1 && head.payload[96] == bwrite &&
                    bdt < 64'd65536 && (&daddr[32:15] || ~|daddr[32:15]);
    assign ext    = run && rlink && starts && dpc == 33'd4 && members != 8'd255 && dt <= 64'd256 &&
                    (members == 8'd1 || dt[8:0] == stride) && 17'(members) * 17'(dt[8:0]) <= 17'd256;

    // A record may load in the last write cycle of the previous one, so back-to-back records cost only their writes.
    // A held run is written from its emit cycle on.
    assign head      = head_i[pick];
    assign len       = chronos_pkg::record_bytes(head.kind);
    assign emit      = state == PICK && run && (any ? !ext : drain_i);
    assign writing   = state == RECORD || emit;
    assign odd       = ofs[2];
    assign pair      = !odd && rec_idx + 4'd1 < rec_len;
    assign ofs_next  = ofs + OW'(pair ? 8 : 4);
    assign last      = writing && sink_ready_i && rec_idx + (pair ? 4'd2 : 4'd1) == rec_len;
    assign start     = writing ? ofs_next : page_open ? ofs : '0;
    assign fits      = 32'(start) + 32'(len) <= PAYLOAD;
    assign room      = !pin_i || 32'(pre_pages_i) + 32'(post_used) < PAGES;
    assign load      = any && (run ? ext : fits) && (state == PICK ? page_open || room : last);
    assign page_base = AW'(slot) * AW'(PAGE_BYTES / 8);

    always_comb begin
        hdr[0] = {16'd64, 8'd0, compact_i ? 8'd2 : 8'd1, 32'h5052_4843};
        hdr[1] = session_i;
        hdr[2] = gen;
        hdr[3] = config_tag_i;
        hdr[4] = {records, 32'(plen)};
        hdr[5] = min_tick;
        hdr[6] = {32'd0, ~pcrc};
        hdr[7] = compact_i ? 64'(events) : 64'd0;
        hcrc   = '1;
        for (int k = 0; k < 8; k++) hcrc = chronos_pkg::crc32_step(hcrc, hdr[k], 1'b1);
        hdr[6] = {~hcrc, ~pcrc};
    end

    always_comb begin
        we_o    = sink_ready_i && (writing || state == TAIL || state == HEADER);
        waddr_o = state == HEADER ? page_base + AW'(hstep) : page_base + AW'(8) + AW'(ofs >> 3);
        wdata_o = 64'd0;
        wbe_o   = odd ? 8'hF0 : 8'hFF;
        if (state == HEADER) begin
            wdata_o = hdr[hstep];
            wbe_o   = 8'hFF;
        end else if (writing) begin
            wdata_o = odd ? {rec[rec_idx], 32'd0} : {rec[rec_idx + 4'd1], rec[rec_idx]};
            if (!odd && !pair) wbe_o = 8'h0F;
        end
        pop_o = load ? 4'(1) << pick : 4'd0;
    end

    always_ff @(posedge clk_i) begin
        if (!rst_ni || clear_i) begin
            state        <= PICK;
            rr           <= 2'd0;
            gen          <= 64'd0;
            pre_ptr      <= '0;
            post_used    <= '0;
            wrapped      <= 1'b0;
            page_open    <= 1'b0;
            storage_full <= 1'b0;
            ofs          <= '0;
            run          <= 1'b0;
            rvalid       <= 1'b0;
            bvalid       <= 1'b0;
        end else begin
            if (load) begin
                if (!run) begin
                    rec[0] <= {10'd0, len, 6'd0, head.flags, 5'd0, head.kind};
                    rec[1] <= {16'd0, 7'd0, head.lane, 6'd0, pick};
                    rec[2] <= epoch_i[pick][31:0];
                    rec[3] <= epoch_i[pick][63:32];
                    rec[4] <= head.seq[31:0];
                    rec[5] <= head.seq[63:32];
                    rec[6] <= head.tick[31:0];
                    rec[7] <= head.tick[63:32];
                    for (int i = 0; i < 5; i++) rec[8 + i] <= head.payload[32*i +: 32];
                    if (rdelta) begin
                        rec[0] <= {16'd16, 16'h0011};
                        rec[2] <= {dpc[15:0], dt[15:0]};
                        rec[3] <= head.payload[63:32];
                    end
                    if (bdelta) begin
                        rec[0] <= {16'd24, 16'h0012};
                        rec[2] <= {daddr[15:0], bdt[15:0]};
                        rec[3] <= head.payload[31:0];
                        rec[4] <= head.payload[95:64];
                        rec[5] <= head.payload[127:96];
                    end
                    rec_len <= rdelta ? 4'd4 : bdelta ? 4'd6 : 4'(len >> 2);
                    rec_idx <= 4'd0;
                end else begin
                    // The second member turns the held single into a PC_RUN of its first member (the context).
                    rec[9] <= {7'd0, dt[8:0], 8'd0, members + 8'd1};
                    stride <= dt[8:0];
                    if (members == 8'd1) begin
                        rec[0]  <= {16'd48, 16'h0010};
                        rec[1]  <= 32'd0;
                        rec[2]  <= epoch_i[0][31:0];
                        rec[3]  <= epoch_i[0][63:32];
                        rec[4]  <= rseq[31:0];
                        rec[5]  <= rseq[63:32];
                        rec[6]  <= rtick[31:0];
                        rec[7]  <= rtick[63:32];
                        rec[8]  <= rpc;
                        rec[10] <= rbound[31:0];
                        rec[11] <= rbound[63:32];
                        rec_len <= 4'd12;
                    end
                end
                run      <= run || starts;
                members  <= run ? members + 8'd1 : 8'd1;
                state    <= run || starts ? PICK : RECORD;
                rr       <= pick + 2'd1;
                events   <= page_open ? events + 32'd1 : 32'd1;
                min_tick <= page_open && min_tick < head.tick ? min_tick : head.tick;
                if (head.kind == 3'd1) begin
                    rvalid <= 1'b1;
                    rseq   <= head.seq;
                    rtick  <= head.tick;
                    rpc    <= head.payload[31:0];
                    rbound <= head.payload[159:96];
                end
                if (head.kind == 3'd2 || head.kind == 3'd3) rvalid <= 1'b0;
                if (head.kind == 3'd5) begin
                    bvalid <= 1'b1;
                    bseq   <= head.seq;
                    btick  <= head.tick;
                    baddr  <= head.payload[63:32];
                    bwrite <= head.payload[96];
                end
                if (!page_open) begin
                    if (pin_i) begin
                        slot      <= pre_pages_i + post_used;
                        post_used <= post_used + 1'b1;
                    end else begin
                        slot    <= pre_ptr;
                        pre_ptr <= pre_ptr + 1'b1 == pre_pages_i ? '0 : pre_ptr + 1'b1;
                        wrapped <= wrapped || pre_ptr + 1'b1 == pre_pages_i;
                    end
                    page_open <= 1'b1;
                    ofs       <= '0;
                    records   <= 32'd0;
                    pcrc      <= '1;
                end
            end
            if (emit) begin
                run   <= 1'b0;
                state <= RECORD;
            end
            // A source reset changes that source's epoch, which ends its codec context.
            if (reset_i[0]) rvalid <= 1'b0;
            if (reset_i[1]) bvalid <= 1'b0;
            if (writing && sink_ready_i) begin
                ofs  <= ofs_next;
                pcrc <= chronos_pkg::crc32_step(pcrc, pair ? {rec[rec_idx + 4'd1], rec[rec_idx]} : {32'd0, rec[rec_idx]}, pair);
                if (!last) begin
                    rec_idx <= rec_idx + (pair ? 4'd2 : 4'd1);
                end else begin
                    records <= records + 32'd1;
                    if (!load && any) begin
                        plen  <= ofs_next;
                        hstep <= 3'd0;
                        state <= 32'(ofs_next) == PAYLOAD ? HEADER : TAIL;
                    end else if (!load) begin
                        state <= PICK;
                    end
                end
            end
            case (state)
                PICK: if (!load && !emit) begin
                    if (any && !page_open) begin
                        storage_full <= 1'b1;
                        state        <= DONE;
                    end else if (any || (drain_i && page_open)) begin
                        plen  <= ofs;
                        hstep <= 3'd0;
                        state <= 32'(ofs) == PAYLOAD ? HEADER : TAIL;
                    end else if (drain_i) begin
                        state <= DONE;
                    end
                end
                TAIL: if (sink_ready_i) begin
                    ofs <= ofs + OW'(odd ? 4 : 8);
                    if (32'(ofs) + (odd ? 4 : 8) == PAYLOAD) state <= HEADER;
                end
                HEADER: if (sink_ready_i) begin
                    hstep <= hstep + 3'd1;
                    if (hstep == 3'd7) begin
                        gen       <= gen + 64'd1;
                        page_open <= 1'b0;
                        rvalid    <= 1'b0;
                        bvalid    <= 1'b0;
                        state     <= PICK;
                    end
                end
                default: ;
            endcase
        end
    end

    assign done_o         = state == DONE;
    assign storage_full_o = storage_full;
    assign pre_ptr_o      = pre_ptr;
    assign post_used_o    = post_used;
    assign wrapped_o      = wrapped;

`ifndef SYNTHESIS
    always_ff @(posedge clk_i) begin
        if (rst_ni && !clear_i) begin
            assert (32'(ofs) <= PAYLOAD) else $error("payload offset beyond page");
            assert (!we_o || 32'(slot) < PAGES) else $error("write outside allocated pages");
            if (writing)
                assert (32'(ofs) + 4 * 32'(rec_len - rec_idx) <= PAYLOAD) else $error("record crosses page end");
            // Both codecs seal for overflow only when a raw record does not fit: the capacity budget's 51-byte tail.
            if (state == TAIL && !drain_i)
                assert (PAYLOAD - 32'(plen) < 52) else $error("sealed tail exceeds the raw bound");
        end
    end
`endif
endmodule
