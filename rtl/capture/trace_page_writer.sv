module trace_page_writer #(
    parameter int PAGE_BYTES = 1024,
    parameter int PAGES = 32
) (
    input  logic                                   clk_i,
    input  logic                                   rst_ni,
    input  logic                                   clear_i,
    input  logic                                   drain_i,
    input  logic                                   sink_ready_i,
    input  logic [63:0]                            session_i,
    input  logic [63:0]                            config_tag_i,
    input  chronos_pkg::entry_t [3:0]              head_i,
    input  logic [3:0]                             empty_i,
    output logic [3:0]                             pop_o,
    output logic                                   we_o,
    output logic [$clog2(PAGES*PAGE_BYTES/8)-1:0]  waddr_o,
    output logic [63:0]                            wdata_o,
    output logic [7:0]                             wbe_o,
    output logic                                   done_o,
    output logic                                   storage_full_o,
    input  logic [$clog2(PAGES)-1:0]               dir_slot_i,
    output logic                                   dir_valid_o,
    output logic [63:0]                            dir_generation_o,
    output logic [$clog2(PAGES):0]                 committed_o
);
    localparam int PAGE_WORDS = PAGE_BYTES / 8;
    localparam int PAYLOAD    = PAGE_BYTES - 64;
    localparam int AW         = $clog2(PAGES * PAGE_WORDS);
    localparam int SW         = $clog2(PAGES);
    localparam int OW         = $clog2(PAGE_BYTES);

    typedef enum logic [2:0] {PICK, RECORD, TAIL, HEADER, COMMIT, DONE} state_e;

    state_e        state;
    logic [1:0]    rr, src, pick;
    logic          any, fits, odd, pair, page_open, storage_full;
    logic [31:0]   rec [16];
    logic [3:0]    rec_len, rec_idx, hstep;
    logic [63:0]   rec_tick, min_tick, generation, hword;
    logic [OW-1:0] ofs, plen;
    logic [31:0]   count, pcrc, hcrc;
    logic [SW:0]   slot;
    logic [5:0]    len;
    logic [AW-1:0] page_base;
    chronos_pkg::entry_t        head;
    logic          dir_valid [PAGES];
    logic [63:0]   dir_gen [PAGES];

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

    assign head      = head_i[pick];
    assign len       = chronos_pkg::record_bytes(head.kind);
    assign fits      = 32'(ofs) + 32'(len) <= PAYLOAD;
    assign odd       = ofs[2];
    assign pair      = !odd && rec_idx + 4'd1 < rec_len;
    assign page_base = AW'(slot[SW-1:0]) * AW'(PAGE_WORDS);

    always_comb begin
        case (hstep[2:0])
            3'd0:    hword = {16'd64, 8'd0, 8'd1, 32'h5052_4843};
            3'd1:    hword = session_i;
            3'd2:    hword = generation;
            3'd3:    hword = config_tag_i;
            3'd4:    hword = {count, 32'(plen)};
            3'd5:    hword = min_tick;
            3'd6:    hword = {32'd0, ~pcrc};
            default: hword = 64'd0;
        endcase
    end

    always_comb begin
        we_o    = 1'b0;
        waddr_o = page_base + AW'(8) + AW'(ofs >> 3);
        wdata_o = 64'd0;
        wbe_o   = 8'h00;
        case (state)
            RECORD: if (sink_ready_i) begin
                we_o = 1'b1;
                if (odd) begin
                    wdata_o = {rec[rec_idx], 32'd0};
                    wbe_o   = 8'hF0;
                end else if (pair) begin
                    wdata_o = {rec[rec_idx + 4'd1], rec[rec_idx]};
                    wbe_o   = 8'hFF;
                end else begin
                    wdata_o = {32'd0, rec[rec_idx]};
                    wbe_o   = 8'h0F;
                end
            end
            TAIL: if (sink_ready_i && 32'(ofs) != PAYLOAD) begin
                we_o  = 1'b1;
                wbe_o = odd ? 8'hF0 : 8'hFF;
            end
            HEADER: if (sink_ready_i && hstep != 4'd6) begin
                we_o    = 1'b1;
                wbe_o   = 8'hFF;
                waddr_o = page_base + AW'(hstep == 4'd8 ? 4'd6 : hstep);
                wdata_o = hstep == 4'd8 ? {~hcrc, ~pcrc} : hword;
            end
            default: ;
        endcase
    end

    always_comb begin
        pop_o = 4'b0;
        if (state == PICK && any && page_open && fits) pop_o[pick] = 1'b1;
    end

    always_ff @(posedge clk_i) begin
        if (!rst_ni || clear_i) begin
            state        <= PICK;
            rr           <= 2'd0;
            slot         <= '0;
            generation   <= 64'd0;
            page_open    <= 1'b0;
            storage_full <= 1'b0;
            ofs          <= '0;
            count        <= 32'd0;
            for (int i = 0; i < PAGES; i++) dir_valid[i] <= 1'b0;
        end else begin
            case (state)
                PICK: if (any) begin
                    if (!page_open) begin
                        if (32'(slot) == PAGES) begin
                            storage_full <= 1'b1;
                            state        <= DONE;
                        end else begin
                            page_open <= 1'b1;
                            ofs       <= '0;
                            count     <= 32'd0;
                            min_tick  <= '1;
                            pcrc      <= '1;
                        end
                    end else if (!fits) begin
                        plen  <= ofs;
                        state <= TAIL;
                    end else begin
                        rec[0] <= {10'd0, len, 6'd0, head.flags, 5'd0, head.kind};
                        rec[1] <= {16'd0, 7'd0, head.lane, 6'd0, pick};
                        rec[2] <= 32'd0;
                        rec[3] <= 32'd0;
                        rec[4] <= head.seq[31:0];
                        rec[5] <= head.seq[63:32];
                        rec[6] <= head.tick[31:0];
                        rec[7] <= head.tick[63:32];
                        for (int i = 0; i < 5; i++) rec[8 + i] <= head.payload[32*i +: 32];
                        rec_len  <= 4'(len >> 2);
                        rec_idx  <= 4'd0;
                        rec_tick <= head.tick;
                        src      <= pick;
                        state    <= RECORD;
                    end
                end else if (drain_i) begin
                    plen  <= ofs;
                    state <= page_open && count != 32'd0 ? TAIL : DONE;
                end
                RECORD: if (sink_ready_i) begin
                    ofs     <= ofs + OW'(pair ? 8 : 4);
                    pcrc    <= chronos_pkg::crc32_step(pcrc, pair ? {rec[rec_idx + 4'd1], rec[rec_idx]} : {32'd0, rec[rec_idx]}, pair);
                    rec_idx <= rec_idx + (pair ? 4'd2 : 4'd1);
                    if (rec_idx + (pair ? 4'd2 : 4'd1) == rec_len) begin
                        count <= count + 32'd1;
                        if (rec_tick < min_tick) min_tick <= rec_tick;
                        rr    <= src + 2'd1;
                        state <= PICK;
                    end
                end
                TAIL: if (32'(ofs) == PAYLOAD) begin
                    hstep <= 4'd0;
                    hcrc  <= '1;
                    state <= HEADER;
                end else if (sink_ready_i) begin
                    ofs <= ofs + OW'(odd ? 4 : 8);
                end
                HEADER: if (sink_ready_i) begin
                    if (hstep == 4'd8) state <= COMMIT;
                    else hcrc <= chronos_pkg::crc32_step(hcrc, hword, 1'b1);
                    hstep <= hstep + 4'd1;
                end
                COMMIT: begin
                    dir_valid[slot[SW-1:0]] <= 1'b1;
                    dir_gen[slot[SW-1:0]]   <= generation;
                    generation              <= generation + 64'd1;
                    slot                    <= slot + 1'b1;
                    page_open               <= 1'b0;
                    state                   <= PICK;
                end
                default: ;
            endcase
        end
    end

    assign done_o           = state == DONE;
    assign storage_full_o   = storage_full;
    assign dir_valid_o      = dir_valid[dir_slot_i];
    assign dir_generation_o = dir_gen[dir_slot_i];
    assign committed_o      = slot;

`ifndef SYNTHESIS
    always_ff @(posedge clk_i) begin
        if (rst_ni && !clear_i) begin
            assert (32'(ofs) <= PAYLOAD) else $error("payload offset beyond page");
            if (we_o)
                assert (32'(slot) < PAGES && !dir_valid[slot[SW-1:0]]) else $error("write to a committed or absent page");
            if (state == RECORD)
                assert (32'(ofs) + 4 * 32'(rec_len - rec_idx) <= PAYLOAD) else $error("record crosses page end");
        end
    end
`endif
endmodule
