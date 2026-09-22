module chronos_capture #(
    parameter int PAGE_BYTES = 1024,
    parameter int SRAM_BYTES = 32768,
    parameter int FIFO_DEPTH = 16
) (
    input  logic                                     clk_i,
    input  logic                                     rst_ni,
    input  logic                                     arm_i,
    input  logic                                     stop_i,
    input  logic [6:0]                               keep_kinds_i,
    input  logic [63:0]                              session_i,
    input  logic [63:0]                              config_tag_i,
    input  logic                                     sink_ready_i,
    input  logic [7:0]                               obs_valid_i,
    input  logic [7:0][2:0]                          obs_kind_i,
    input  logic [7:0][1:0]                          obs_flags_i,
    input  logic [7:0][159:0]                        obs_payload_i,
    output logic [2:0]                               state_o,
    output logic                                     storage_full_o,
    output logic [63:0]                              tick_o,
    input  logic [1:0]                               acct_source_i,
    input  logic [1:0]                               acct_counter_i,
    output logic [63:0]                              acct_value_o,
    output logic [$clog2(FIFO_DEPTH):0]              fifo_count_o,
    input  logic [$clog2(SRAM_BYTES/PAGE_BYTES)-1:0] dir_slot_i,
    output logic                                     dir_valid_o,
    output logic [63:0]                              dir_generation_o,
    output logic [$clog2(SRAM_BYTES/PAGE_BYTES):0]   committed_o,
    input  logic                                     rd_en_i,
    input  logic [$clog2(SRAM_BYTES/8)-1:0]          rd_addr_i,
    output logic [63:0]                              rd_data_o
);
    localparam int PAGES = SRAM_BYTES / PAGE_BYTES;
    localparam int WORDS = SRAM_BYTES / 8;

    logic [2:0]                   state;
    logic                         clear, open, done, storage_full;
    logic [63:0]                  tick, session, config_tag;
    logic [6:0]                   keep_kinds;
    chronos_pkg::entry_t [3:0]                 heads;
    logic [3:0]                   empty, pops;
    logic [$clog2(FIFO_DEPTH):0]  counts [4];
    logic [63:0]                  observed [4], filtered [4], admitted [4], dropped [4];
    logic                         we;
    logic [$clog2(WORDS)-1:0]     waddr;
    logic [63:0]                  wdata;
    logic [7:0]                   wbe;

    assign clear = arm_i && (state == chronos_pkg::STATE_DISABLED || state == chronos_pkg::STATE_FROZEN);
    assign open  = state == chronos_pkg::STATE_ARMED && !stop_i && !storage_full;

    always_ff @(posedge clk_i) begin
        if (!rst_ni) begin
            state <= chronos_pkg::STATE_DISABLED;
            tick  <= 64'd0;
        end else if (clear) begin
            state      <= chronos_pkg::STATE_ARMED;
            tick       <= 64'd0;
            session    <= session_i;
            config_tag <= config_tag_i;
            keep_kinds <= keep_kinds_i;
        end else begin
            case (state)
                chronos_pkg::STATE_ARMED: begin
                    tick <= tick + 64'd1;
                    if (stop_i || storage_full) state <= chronos_pkg::STATE_DRAINING;
                end
                chronos_pkg::STATE_DRAINING: if (done) state <= chronos_pkg::STATE_FROZEN;
                default: ;
            endcase
        end
    end

    for (genvar s = 0; s < 4; s++) begin : g_source
        trace_ingress #(.DEPTH(FIFO_DEPTH)) ingress (
            .clk_i, .rst_ni,
            .clear_i(clear),
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
        .clear_i(clear),
        .drain_i(state == chronos_pkg::STATE_DRAINING),
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
        .dir_slot_i,
        .dir_valid_o,
        .dir_generation_o,
        .committed_o
    );

    trace_sram #(.WORDS(WORDS)) sram (
        .clk_i,
        .we_i(we),
        .waddr_i(waddr),
        .wdata_i(wdata),
        .wbe_i(wbe),
        .re_i(rd_en_i),
        .raddr_i(rd_addr_i),
        .rdata_o(rd_data_o)
    );

    always_comb begin
        case (acct_counter_i)
            2'd0:    acct_value_o = observed[acct_source_i];
            2'd1:    acct_value_o = filtered[acct_source_i];
            2'd2:    acct_value_o = admitted[acct_source_i];
            default: acct_value_o = dropped[acct_source_i];
        endcase
    end

    assign state_o        = state;
    assign storage_full_o = storage_full;
    assign tick_o         = tick;
    assign fifo_count_o   = counts[acct_source_i];

`ifndef SYNTHESIS
    always_ff @(posedge clk_i) begin
        if (rst_ni) begin
            assert (!rd_en_i || state == chronos_pkg::STATE_FROZEN) else $error("readout while capture storage is live");
            assert (!we || state == chronos_pkg::STATE_ARMED || state == chronos_pkg::STATE_DRAINING) else $error("storage write outside capture");
        end
    end
`endif
endmodule
