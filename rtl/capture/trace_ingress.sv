module trace_ingress #(
    parameter int DEPTH = 16
) (
    input  logic                   clk_i,
    input  logic                   rst_ni,
    input  logic                   clear_i,
    input  logic                   open_i,
    input  logic                   admit_i,
    input  logic                   cap_block_i,
    input  logic                   reset_i,
    input  logic [63:0]            tick_i,
    input  logic [6:0]             keep_kinds_i,
    input  logic [1:0]             valid_i,
    input  chronos_pkg::obs_t      obs0_i,
    input  chronos_pkg::obs_t      obs1_i,
    input  logic                   pop_i,
    output chronos_pkg::entry_t    head_o,
    output logic [$clog2(DEPTH):0] count_o,
    output logic [1:0]             eligible_o,
    output logic                   fits_o,
    output logic [63:0]            observed_o,
    output logic [63:0]            filtered_o,
    output logic [63:0]            admitted_o,
    output logic [63:0]            fifo_dropped_o,
    output logic [63:0]            capacity_dropped_o,
    output logic [63:0]            reset_discarded_o,
    output logic [63:0]            epoch_o
);
    logic [63:0]         seq, base;
    logic                a_valid, b_valid, a_keep, b_keep;
    chronos_pkg::obs_t   a;
    logic [1:0]          observed;
    chronos_pkg::entry_t entry_a, entry_b;

    assign a_valid    = valid_i[0] | valid_i[1];
    assign b_valid    = valid_i[0] & valid_i[1];
    assign a          = valid_i[0] ? obs0_i : obs1_i;
    assign a_keep     = a_valid && keep_kinds_i[a.kind - 3'd1];
    assign b_keep     = b_valid && keep_kinds_i[obs1_i.kind - 3'd1];
    assign observed   = {1'b0, a_valid} + {1'b0, b_valid};
    assign eligible_o = {1'b0, a_keep} + {1'b0, b_keep};
    // A source reset discards the queue and restarts sequences before this cycle's observations are admitted.
    assign base       = reset_i ? '0 : seq;
    assign fits_o     = 32'(eligible_o) + (reset_i ? 32'd0 : 32'(count_o)) <= DEPTH;

    always_comb begin
        entry_a         = '0;
        entry_a.kind    = a.kind;
        entry_a.flags   = a.flags;
        entry_a.seq     = base;
        entry_a.tick    = tick_i;
        entry_a.payload = a.payload;
        entry_b         = '0;
        entry_b.kind    = obs1_i.kind;
        entry_b.flags   = obs1_i.flags;
        entry_b.lane    = 1'b1;
        entry_b.seq     = base + 64'd1;
        entry_b.tick    = tick_i;
        entry_b.payload = obs1_i.payload;
    end

    trace_fifo #(.W($bits(entry_a)), .DEPTH(DEPTH)) queue (
        .clk_i, .rst_ni,
        .clear_i(clear_i || reset_i),
        .push0_i(admit_i),
        .push1_i(admit_i && a_keep && b_keep),
        .data0_i(a_keep ? entry_a : entry_b),
        .data1_i(entry_b),
        .pop_i,
        .head_o,
        .count_o
    );

    always_ff @(posedge clk_i) begin
        if (!rst_ni || clear_i) begin
            seq                <= '0;
            observed_o         <= '0;
            filtered_o         <= '0;
            admitted_o         <= '0;
            fifo_dropped_o     <= '0;
            capacity_dropped_o <= '0;
            reset_discarded_o  <= '0;
            epoch_o            <= '0;
        end else begin
            if (reset_i) begin
                epoch_o           <= epoch_o + 64'd1;
                reset_discarded_o <= reset_discarded_o + 64'(count_o);
                seq               <= '0;
            end
            if (open_i) begin
            seq        <= base + 64'(observed);
            observed_o <= observed_o + 64'(observed);
            filtered_o <= filtered_o + 64'(observed - eligible_o);
            if (admit_i)
                admitted_o <= admitted_o + 64'(eligible_o);
            else if (cap_block_i)
                capacity_dropped_o <= capacity_dropped_o + 64'(eligible_o);
            else
                fifo_dropped_o <= fifo_dropped_o + 64'(eligible_o);
            end
        end
    end

`ifndef SYNTHESIS
    always_ff @(posedge clk_i) begin
        if (rst_ni && open_i) begin
            assert (!(valid_i[0] && obs0_i.kind == 3'd0) && !(valid_i[1] && obs1_i.kind == 3'd0))
                else $error("valid observation without a kind");
        end
    end
`endif
endmodule
