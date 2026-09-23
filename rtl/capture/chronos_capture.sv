// Register offsets and fields follow spec/registers.json (map 1.1); unimplemented registers read zero.
module chronos_capture #(
    parameter int PAGE_BYTES = 1024,
    parameter int SRAM_BYTES = 32768,
    parameter int FIFO_DEPTH = 16
) (
    input  logic                  clk_i,
    input  logic                  rst_ni,
    input  logic                  sink_ready_i,
    input  logic [7:0]            obs_valid_i,
    input  logic [7:0][2:0]       obs_kind_i,
    input  logic [7:0][1:0]       obs_flags_i,
    input  logic [7:0][159:0]     obs_payload_i,
    input  logic                  reg_req_i,
    input  logic                  reg_we_i,
    input  logic [8:0]            reg_addr_i,
    input  logic [31:0]           reg_wdata_i,
    output logic [31:0]           reg_rdata_o
);
    localparam int PAGES  = SRAM_BYTES / PAGE_BYTES;
    localparam int WORDS  = SRAM_BYTES / 8;
    localparam int PW     = $clog2(PAGES);
    // Measured worst-case raw record and sealed-page tail (model capacity.measured_inventory).
    localparam int RECORD = 52, TAIL = 51;
    localparam logic [8:0] COMMAND = 9'h040, ACCT_SELECT = 9'h120, READ_SESSION_LO = 9'h140,
                           READ_SESSION_HI = 9'h144, READ_OFFSET = 9'h148, READ_DATA = 9'h150;
    localparam logic [2:0] ACCEPTED = 3'd1, IGNORED = 3'd2, REJECTED = 3'd3, SUPERSEDED = 3'd4;
    localparam logic [2:0] DISABLED = 3'd0, ARMED = 3'd1, POST_TRIGGER = 3'd2, DRAINING = 3'd3, FROZEN = 3'd4;

    logic [2:0]                   state, trig_primary, primary;
    logic [3:0]                   stop_reason, trig_slots, slots;
    logic [5:0]                   trig_lanes, lanes;
    logic                         configured, config_ok, cfg_hit, storage_full, done, write, read, command, idle;
    logic                         blocked, clear_ok, configure_ok, arm_ok, stop_ok, soft_ok, open, triggered;
    logic                         trig_software, trigger_now, capturing, valid, data_ok, data_q, half_q, wrapped;
    logic [4:0]                   cfg_index;
    logic [31:0]                  staged [24], active [24];
    logic [63:0]                  tick, trig_tick, session, config_tag, read_session, acct;
    logic [31:0]                  read_offset, length, value, rdata_q, payload, reserved, post_completed;
    logic [2:0]                   outcomes [7];
    logic [20:0]                  outcome;
    logic [6:0]                   cmd, acct_select;
    logic [PW:0]                  ring_pre, pre_ptr, post_used, n_pre, first, page, slot;
    chronos_pkg::entry_t [3:0]    heads;
    logic [3:0]                   pops, fits, admit, cap_block;
    logic [1:0]                   eligible [4];
    logic [$clog2(FIFO_DEPTH):0]  counts [4];
    logic [63:0]                  observed [4], filtered [4], admitted [4], fifo_dropped [4], capacity_dropped [4];
    logic                         we;
    logic [$clog2(WORDS)-1:0]     waddr;
    logic [63:0]                  wdata, rdata;
    logic [7:0]                   wbe;

    assign write     = reg_req_i && reg_we_i;
    assign read      = reg_req_i && !reg_we_i;
    assign command   = write && reg_addr_i == COMMAND;
    assign cmd       = command ? reg_wdata_i[6:0] : 7'd0;
    assign idle      = state == DISABLED || state == FROZEN;
    assign capturing = state == ARMED || state == POST_TRIGGER;

    // Staged words: CFG_SPLIT, CFG_MODE, CFG_POST_TICKS_LO/HI, then TRIGn CTRL/VALUE/MASK/BASE/LIMIT.
    always_comb begin
        cfg_hit   = reg_addr_i[1:0] == 2'd0 && ((reg_addr_i >= 9'h020 && reg_addr_i <= 9'h02C) ||
                                               (reg_addr_i[8:7] == 2'b01 && reg_addr_i[4:0] < 5'h14));
        cfg_index = reg_addr_i[8:7] == 2'b01 ? 5'd4 + 5'(reg_addr_i[6:5]) * 5'd5 + 5'(reg_addr_i[4:2])
                                             : 5'(reg_addr_i[3:2]);
        config_ok = staged[0][15:0] != 16'd0 && staged[0][31:16] != 16'd0 && !staged[1][0] &&
                    32'(staged[0][15:0]) + 32'(staged[0][31:16]) == PAGES &&
                    32'(4 * FIFO_DEPTH * RECORD) + 32'(staged[0][31:16]) * TAIL <= 32'(staged[0][31:16]) * (PAGE_BYTES - 64);
        for (int t = 0; t < 4; t++) begin
            if (staged[4 + 5*t][0] && (staged[4 + 5*t][14:8] == 7'd0 ||
                                       (staged[4 + 5*t][1] && staged[7 + 5*t] >= staged[8 + 5*t])))
                config_ok = 1'b0;
        end
    end

    // Same-cycle priority: trace_reset > clear > configure > arm > stop > reset_source > software_trigger.
    // This build has no software trace reset or source reset, so those commands are rejected.
    always_comb begin
        for (int i = 0; i < 7; i++) outcomes[i] = 3'd0;
        blocked = 1'b0;
        if (cmd[0]) outcomes[0] = REJECTED;
        if (cmd[1]) begin
            outcomes[1] = idle ? ACCEPTED : REJECTED;
            blocked     = idle;
        end
        if (cmd[2]) outcomes[2] = blocked ? SUPERSEDED : idle && config_ok ? ACCEPTED : REJECTED;
        if (cmd[3]) begin
            outcomes[3] = blocked ? SUPERSEDED : state == DISABLED && (configured || outcomes[2] == ACCEPTED) ? ACCEPTED : REJECTED;
            blocked     = blocked || outcomes[3] == ACCEPTED;
        end
        if (cmd[4]) begin
            outcomes[4] = blocked ? SUPERSEDED : capturing ? ACCEPTED : state == DRAINING ? IGNORED : REJECTED;
            blocked     = blocked || outcomes[4] == ACCEPTED;
        end
        if (cmd[5]) outcomes[5] = blocked ? SUPERSEDED : REJECTED;
        if (cmd[6]) outcomes[6] = blocked ? SUPERSEDED : state == ARMED ? ACCEPTED : state == POST_TRIGGER ? IGNORED : REJECTED;
    end

    assign clear_ok     = outcomes[1] == ACCEPTED;
    assign configure_ok = outcomes[2] == ACCEPTED;
    assign arm_ok       = outcomes[3] == ACCEPTED;
    assign stop_ok      = outcomes[4] == ACCEPTED;
    assign soft_ok      = outcomes[6] == ACCEPTED;
    assign open         = capturing && !stop_ok && !storage_full;

    // Trigger slots compare one key field per kind; the first matching cycle latches the descriptor.
    always_comb begin
        lanes   = 6'd0;
        slots   = 4'd0;
        primary = 3'd7;
        for (int p = 0; p < 8; p++) begin
            logic [31:0] key;
            logic [3:0]  hits;
            logic [2:0]  lane;
            key  = obs_kind_i[p] == 3'd1 || obs_kind_i[p] >= 3'd6 ? obs_payload_i[p][31:0] : obs_payload_i[p][63:32];
            lane = p < 2 ? 3'd0 : p < 4 ? 3'd1 : p < 6 ? 3'd3 : 3'd5;
            if (p % 2 == 1 && obs_valid_i[p - 1]) lane = lane + 3'd1;
            for (int t = 0; t < 4; t++) begin
                hits[t] = obs_valid_i[p] && obs_kind_i[p] != 3'd0 && active[4 + 5*t][0] &&
                          active[4 + 5*t][7 + 32'(obs_kind_i[p])] &&
                          (active[4 + 5*t][1] ? key >= active[7 + 5*t] && key < active[8 + 5*t]
                                              : (key & active[6 + 5*t]) == (active[5 + 5*t] & active[6 + 5*t]));
            end
            if (hits != 4'd0) begin
                lanes[lane] = 1'b1;
                if (primary == 3'd7) primary = lane;
            end
            slots = slots | hits;
        end
    end

    assign trigger_now = open && !triggered && (lanes != 6'd0 || soft_ok);

    // Post-trigger admission keeps the worst-case completion of all admitted work within the post pool.
    always_comb begin
        logic closed;
        payload  = 32'(active[0][31:16]) * (PAGE_BYTES - 64);
        reserved = 32'(active[0][31:16]) * TAIL + RECORD * (post_completed + 32'(counts[0]) + 32'(counts[1])
                                                            + 32'(counts[2]) + 32'(counts[3]));
        closed   = 1'b0;
        for (int s = 0; s < 4; s++) begin
            cap_block[s] = (triggered || trigger_now) && eligible[s] != 2'd0 &&
                           (closed || reserved + RECORD * 32'(eligible[s]) > payload);
            closed       = closed || cap_block[s];
            admit[s]     = open && eligible[s] != 2'd0 && !cap_block[s] && fits[s];
            if (admit[s]) reserved = reserved + RECORD * 32'(eligible[s]);
        end
    end

    always_ff @(posedge clk_i) begin
        if (!rst_ni) begin
            state        <= DISABLED;
            stop_reason  <= 4'd0;
            configured   <= 1'b0;
            session      <= 64'd0;
            config_tag   <= 64'd0;
            acct_select  <= 7'd0;
            read_session <= 64'd0;
            read_offset  <= 32'd0;
            outcome      <= 21'd0;
            tick         <= 64'd0;
            triggered    <= 1'b0;
            for (int i = 0; i < 24; i++) staged[i] <= i == 1 ? 32'h7F00 : 32'd0;
        end else begin
            if (write && cfg_hit) staged[cfg_index] <= reg_wdata_i;
            if (write && reg_addr_i == ACCT_SELECT) acct_select <= reg_wdata_i[6:0];
            if (write && reg_addr_i == READ_SESSION_LO) read_session[31:0] <= reg_wdata_i;
            if (write && reg_addr_i == READ_SESSION_HI) read_session[63:32] <= reg_wdata_i;
            if (write && reg_addr_i == READ_OFFSET) read_offset <= reg_wdata_i;
            if (read && reg_addr_i == READ_DATA && data_ok) read_offset <= read_offset + 32'd4;
            if (command) outcome <= {outcomes[6], outcomes[5], outcomes[4], outcomes[3], outcomes[2], outcomes[1], outcomes[0]};
            if (configure_ok) begin
                active     <= staged;
                config_tag <= config_tag + 64'd1;
                configured <= 1'b1;
            end
            if (clear_ok) begin
                state       <= DISABLED;
                stop_reason <= 4'd0;
            end else if (arm_ok) begin
                state          <= ARMED;
                stop_reason    <= 4'd0;
                tick           <= 64'd0;
                session        <= session + 64'd1;
                triggered      <= 1'b0;
                post_completed <= 32'd0;
                ring_pre       <= (PW+1)'(configure_ok ? staged[0][15:0] : active[0][15:0]);
            end else if (capturing) begin
                tick <= tick + 64'd1;
                if (pops != 4'd0 && (triggered || trigger_now)) post_completed <= post_completed + 32'd1;
                if (trigger_now) begin
                    state         <= POST_TRIGGER;
                    triggered     <= 1'b1;
                    trig_tick     <= tick;
                    trig_lanes    <= lanes;
                    trig_primary  <= primary;
                    trig_slots    <= slots;
                    trig_software <= soft_ok;
                end
                if (stop_ok || storage_full || cap_block != 4'd0 ||
                    ((triggered || trigger_now) && tick == (triggered ? trig_tick : tick) + {active[3], active[2]})) begin
                    state       <= DRAINING;
                    stop_reason <= stop_ok ? 4'd1 : storage_full ? 4'd8 : cap_block != 4'd0 ? 4'd3 : 4'd2;
                end
            end else if (state == DRAINING && done) begin
                state <= FROZEN;
            end
        end
    end

    for (genvar s = 0; s < 4; s++) begin : g_source
        trace_ingress #(.DEPTH(FIFO_DEPTH)) ingress (
            .clk_i, .rst_ni,
            .clear_i(clear_ok || arm_ok),
            .open_i(open),
            .admit_i(admit[s]),
            .cap_block_i(cap_block[s]),
            .tick_i(tick),
            .keep_kinds_i(active[1][14:8]),
            .valid_i(obs_valid_i[2*s +: 2]),
            .obs0_i({obs_kind_i[2*s], obs_flags_i[2*s], obs_payload_i[2*s]}),
            .obs1_i({obs_kind_i[2*s+1], obs_flags_i[2*s+1], obs_payload_i[2*s+1]}),
            .pop_i(pops[s]),
            .head_o(heads[s]),
            .count_o(counts[s]),
            .eligible_o(eligible[s]),
            .fits_o(fits[s]),
            .observed_o(observed[s]),
            .filtered_o(filtered[s]),
            .admitted_o(admitted[s]),
            .fifo_dropped_o(fifo_dropped[s]),
            .capacity_dropped_o(capacity_dropped[s])
        );
    end

    trace_page_writer #(.PAGE_BYTES(PAGE_BYTES), .PAGES(PAGES)) writer (
        .clk_i, .rst_ni,
        .clear_i(clear_ok || arm_ok),
        .drain_i(state == DRAINING),
        .pin_i(state == POST_TRIGGER || state == DRAINING || trigger_now || stop_ok),
        .pre_pages_i(ring_pre),
        .sink_ready_i,
        .session_i(session),
        .config_tag_i(config_tag),
        .head_i(heads),
        .empty_i({counts[3] == '0, counts[2] == '0, counts[1] == '0, counts[0] == '0}),
        .pop_o(pops),
        .we_o(we),
        .waddr_o(waddr),
        .wdata_o(wdata),
        .wbe_o(wbe),
        .done_o(done),
        .storage_full_o(storage_full),
        .pre_ptr_o(pre_ptr),
        .post_used_o(post_used),
        .wrapped_o(wrapped)
    );

    // Readout image: committed pages in generation order, the oldest prehistory slot first, then post slots.
    assign n_pre   = wrapped ? ring_pre : pre_ptr;
    assign first   = wrapped ? pre_ptr : '0;
    assign page    = (PW+1)'(read_offset >> $clog2(PAGE_BYTES));
    assign slot    = page < n_pre ? (first + page >= ring_pre ? first + page - ring_pre : first + page)
                                  : ring_pre + page - n_pre;
    assign valid   = state == FROZEN && read_session == session;
    assign length  = valid ? 32'(n_pre + post_used) * PAGE_BYTES : 32'd0;
    assign data_ok = valid && read_offset < length;

    trace_sram #(.WORDS(WORDS)) sram (
        .clk_i,
        .we_i(we),
        .waddr_i(waddr),
        .wdata_i(wdata),
        .wbe_i(wbe),
        .re_i(read && reg_addr_i == READ_DATA && data_ok),
        .raddr_i($clog2(WORDS)'(slot) * $clog2(WORDS)'(PAGE_BYTES / 8) + $clog2(WORDS)'(read_offset[$clog2(PAGE_BYTES)-1:3])),
        .rdata_o(rdata)
    );

    always_comb begin
        case (acct_select[6:4])
            3'd0:    acct = observed[acct_select[1:0]];
            3'd1:    acct = filtered[acct_select[1:0]];
            3'd2:    acct = admitted[acct_select[1:0]];
            3'd3:    acct = fifo_dropped[acct_select[1:0]] + capacity_dropped[acct_select[1:0]];
            3'd5:    acct = fifo_dropped[acct_select[1:0]];
            3'd6:    acct = capacity_dropped[acct_select[1:0]];
            default: acct = 64'd0;
        endcase
        case (reg_addr_i)
            9'h000:  value = 32'h4E52_4843;
            9'h004:  value = 32'h0000_0101;
            9'h008:  value = 32'd4 | 32'd4 << 3 | 32'($clog2(FIFO_DEPTH)) << 6 | 32'($clog2(PAGE_BYTES)) << 10
                             | 32'd1 << 14 | 32'd128 << 16 | 32'd8 << 24;
            9'h00C:  value = SRAM_BYTES;
            9'h050:  value = 32'(state) | 32'(stop_reason) << 3 | 32'(storage_full) << 7 | 32'(triggered) << 10
                             | 32'(triggered && trig_software) << 11 | 32'(configured) << 12;
            9'h054:  value = 32'(outcome);
            9'h058:  value = session[31:0];
            9'h05C:  value = session[63:32];
            9'h060:  value = config_tag[31:0];
            9'h064:  value = config_tag[63:32];
            9'h100:  value = triggered ? trig_tick[31:0] : 32'd0;
            9'h104:  value = triggered ? trig_tick[63:32] : 32'd0;
            9'h108:  value = triggered ? {16'd0, trig_slots, trig_software, trig_primary, 2'd0, trig_lanes} : 32'h700;
            9'h120:  value = 32'(acct_select);
            9'h124:  value = acct[31:0];
            9'h128:  value = acct[63:32];
            9'h140:  value = read_session[31:0];
            9'h144:  value = read_session[63:32];
            9'h148:  value = read_offset;
            9'h14C:  value = length;
            9'h154:  value = {30'd0, !valid, valid};
            default: value = cfg_hit ? staged[cfg_index] : 32'd0;
        endcase
    end

    always_ff @(posedge clk_i) begin
        if (read) begin
            rdata_q <= value;
            data_q  <= reg_addr_i == READ_DATA && data_ok;
            half_q  <= read_offset[2];
        end
    end

    assign reg_rdata_o = data_q ? (half_q ? rdata[63:32] : rdata[31:0]) : rdata_q;

`ifndef SYNTHESIS
    always_ff @(posedge clk_i) begin
        if (rst_ni) begin
            assert (!we || capturing || state == DRAINING) else $error("storage write outside capture");
            assert (!(read && reg_addr_i == READ_DATA && data_ok) || state == FROZEN) else $error("readout while capture storage is live");
            assert (!(capturing && triggered) || reserved <= payload) else $error("post reserve exceeded");
            for (int s = 1; s < 4; s++)
                assert (!(cap_block[s - 1] && eligible[s] != 2'd0) || cap_block[s]) else $error("capacity closure must cover later sources");
        end
    end
`endif
endmodule
