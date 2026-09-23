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
    localparam int PAGES = SRAM_BYTES / PAGE_BYTES;
    localparam int WORDS = SRAM_BYTES / 8;
    localparam logic [8:0] CFG_MODE = 9'h024, COMMAND = 9'h040, ACCT_SELECT = 9'h120, READ_SESSION_LO = 9'h140,
                           READ_SESSION_HI = 9'h144, READ_OFFSET = 9'h148, READ_DATA = 9'h150;
    localparam logic [2:0] ACCEPTED = 3'd1, IGNORED = 3'd2, REJECTED = 3'd3, SUPERSEDED = 3'd4;
    localparam logic [2:0] DISABLED = chronos_pkg::STATE_DISABLED, ARMED = chronos_pkg::STATE_ARMED,
                           DRAINING = chronos_pkg::STATE_DRAINING, FROZEN = chronos_pkg::STATE_FROZEN;

    logic [2:0]                   state;
    logic [3:0]                   stop_reason;
    logic                         configured, cfg_codec, storage_full, done, write, read, command, idle, blocked;
    logic                         clear_ok, configure_ok, arm_ok, stop_ok, open, valid, data_ok, data_q, half_q;
    logic [6:0]                   keep_kinds, cfg_keep, acct_select;
    logic [63:0]                  tick, session, config_tag, read_session, acct;
    logic [31:0]                  read_offset, length, value, rdata_q;
    logic [2:0]                   outcomes [7];
    logic [20:0]                  outcome;
    logic [6:0]                   cmd;
    chronos_pkg::entry_t [3:0]    heads;
    logic [3:0]                   empty, pops;
    logic [$clog2(FIFO_DEPTH):0]  counts [4];
    logic [63:0]                  observed [4], filtered [4], admitted [4], dropped [4];
    logic [$clog2(PAGES):0]       committed;
    logic                         we;
    logic [$clog2(WORDS)-1:0]     waddr;
    logic [63:0]                  wdata, rdata;
    logic [7:0]                   wbe;

    assign write   = reg_req_i && reg_we_i;
    assign read    = reg_req_i && !reg_we_i;
    assign command = write && reg_addr_i == COMMAND;
    assign cmd     = command ? reg_wdata_i[6:0] : 7'd0;
    assign idle    = state == DISABLED || state == FROZEN;

    // Same-cycle priority: trace_reset > clear > configure > arm > stop > reset_source > software_trigger.
    // This build has no software trace reset, source reset, or trigger slots, so those commands are rejected.
    always_comb begin
        for (int i = 0; i < 7; i++) outcomes[i] = 3'd0;
        blocked = 1'b0;
        if (cmd[0]) outcomes[0] = REJECTED;
        if (cmd[1]) begin
            outcomes[1] = idle ? ACCEPTED : REJECTED;
            blocked     = idle;
        end
        if (cmd[2]) outcomes[2] = blocked ? SUPERSEDED : idle && !cfg_codec ? ACCEPTED : REJECTED;
        if (cmd[3]) begin
            outcomes[3] = blocked ? SUPERSEDED : state == DISABLED && (configured || outcomes[2] == ACCEPTED) ? ACCEPTED : REJECTED;
            blocked     = blocked || outcomes[3] == ACCEPTED;
        end
        if (cmd[4]) begin
            outcomes[4] = blocked ? SUPERSEDED : state == ARMED ? ACCEPTED : state == DRAINING ? IGNORED : REJECTED;
            blocked     = blocked || outcomes[4] == ACCEPTED;
        end
        if (cmd[5]) outcomes[5] = blocked ? SUPERSEDED : REJECTED;
        if (cmd[6]) outcomes[6] = blocked ? SUPERSEDED : REJECTED;
    end

    assign clear_ok     = outcomes[1] == ACCEPTED;
    assign configure_ok = outcomes[2] == ACCEPTED;
    assign arm_ok       = outcomes[3] == ACCEPTED;
    assign stop_ok      = outcomes[4] == ACCEPTED;
    assign open         = state == ARMED && !stop_ok && !storage_full;
    assign valid        = state == FROZEN && read_session == session;
    assign length       = valid ? 32'(committed) * PAGE_BYTES : 32'd0;
    assign data_ok      = valid && read_offset < length;

    always_ff @(posedge clk_i) begin
        if (!rst_ni) begin
            state        <= DISABLED;
            stop_reason  <= 4'd0;
            configured   <= 1'b0;
            session      <= 64'd0;
            config_tag   <= 64'd0;
            keep_kinds   <= '1;
            cfg_keep     <= '1;
            cfg_codec    <= 1'b0;
            acct_select  <= 7'd0;
            read_session <= 64'd0;
            read_offset  <= 32'd0;
            outcome      <= 21'd0;
            tick         <= 64'd0;
        end else begin
            if (write) begin
                case (reg_addr_i)
                    CFG_MODE:        {cfg_keep, cfg_codec} <= {reg_wdata_i[14:8], reg_wdata_i[0]};
                    ACCT_SELECT:     acct_select <= reg_wdata_i[6:0];
                    READ_SESSION_LO: read_session[31:0] <= reg_wdata_i;
                    READ_SESSION_HI: read_session[63:32] <= reg_wdata_i;
                    READ_OFFSET:     read_offset <= reg_wdata_i;
                    default: ;
                endcase
            end
            if (command) outcome <= {outcomes[6], outcomes[5], outcomes[4], outcomes[3], outcomes[2], outcomes[1], outcomes[0]};
            if (read && reg_addr_i == READ_DATA && data_ok) read_offset <= read_offset + 32'd4;
            if (configure_ok) begin
                keep_kinds <= cfg_keep;
                config_tag <= config_tag + 64'd1;
                configured <= 1'b1;
            end
            if (clear_ok) begin
                state       <= DISABLED;
                stop_reason <= 4'd0;
            end else if (arm_ok) begin
                state       <= ARMED;
                stop_reason <= 4'd0;
                tick        <= 64'd0;
                session     <= session + 64'd1;
            end else if (state == ARMED) begin
                tick <= tick + 64'd1;
                if (stop_ok || storage_full) begin
                    state       <= DRAINING;
                    stop_reason <= stop_ok ? 4'd1 : 4'd8;
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
            .tick_i(tick),
            .keep_kinds_i(keep_kinds),
            .valid_i(obs_valid_i[2*s +: 2]),
            .obs0_i({obs_kind_i[2*s], obs_flags_i[2*s], obs_payload_i[2*s]}),
            .obs1_i({obs_kind_i[2*s+1], obs_flags_i[2*s+1], obs_payload_i[2*s+1]}),
            .pop_i(pops[s]),
            .head_o(heads[s]),
            .count_o(counts[s]),
            .observed_o(observed[s]),
            .filtered_o(filtered[s]),
            .admitted_o(admitted[s]),
            .dropped_o(dropped[s])
        );
        assign empty[s] = counts[s] == '0;
    end

    trace_page_writer #(.PAGE_BYTES(PAGE_BYTES), .PAGES(PAGES)) writer (
        .clk_i, .rst_ni,
        .clear_i(clear_ok || arm_ok),
        .drain_i(state == DRAINING),
        .sink_ready_i,
        .session_i(session),
        .config_tag_i(config_tag),
        .head_i(heads),
        .empty_i(empty),
        .pop_o(pops),
        .we_o(we),
        .waddr_o(waddr),
        .wdata_o(wdata),
        .wbe_o(wbe),
        .done_o(done),
        .storage_full_o(storage_full),
        .committed_o(committed)
    );

    trace_sram #(.WORDS(WORDS)) sram (
        .clk_i,
        .we_i(we),
        .waddr_i(waddr),
        .wdata_i(wdata),
        .wbe_i(wbe),
        .re_i(read && reg_addr_i == READ_DATA && data_ok),
        .raddr_i(read_offset[$clog2(WORDS)+2:3]),
        .rdata_o(rdata)
    );

    always_comb begin
        case (acct_select[6:4])
            3'd0:       acct = observed[acct_select[1:0]];
            3'd1:       acct = filtered[acct_select[1:0]];
            3'd2:       acct = admitted[acct_select[1:0]];
            3'd3, 3'd5: acct = dropped[acct_select[1:0]];
            default:    acct = 64'd0;
        endcase
        case (reg_addr_i)
            9'h000:  value = 32'h4E52_4843;
            9'h004:  value = 32'h0000_0101;
            9'h008:  value = 32'd4 | 32'($clog2(FIFO_DEPTH)) << 6 | 32'($clog2(PAGE_BYTES)) << 10 | 32'd1 << 14
                             | 32'd128 << 16 | 32'd8 << 24;
            9'h00C:  value = SRAM_BYTES;
            9'h024:  value = {17'd0, cfg_keep, 7'd0, cfg_codec};
            9'h050:  value = 32'(state) | 32'(stop_reason) << 3 | 32'(storage_full) << 7 | 32'(configured) << 12;
            9'h054:  value = 32'(outcome);
            9'h058:  value = session[31:0];
            9'h05C:  value = session[63:32];
            9'h060:  value = config_tag[31:0];
            9'h064:  value = config_tag[63:32];
            9'h120:  value = 32'(acct_select);
            9'h124:  value = acct[31:0];
            9'h128:  value = acct[63:32];
            9'h140:  value = read_session[31:0];
            9'h144:  value = read_session[63:32];
            9'h148:  value = read_offset;
            9'h14C:  value = length;
            9'h154:  value = {30'd0, !valid, valid};
            default: value = 32'd0;
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
            assert (!we || state == ARMED || state == DRAINING) else $error("storage write outside capture");
            assert (!(read && reg_addr_i == READ_DATA && data_ok) || state == FROZEN) else $error("readout while capture storage is live");
        end
    end
`endif
endmodule
