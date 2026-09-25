// Two-push, one-pop queue. A clear empties it before the same cycle's pushes are stored.
module trace_fifo #(
    parameter int W = 8,
    parameter int DEPTH = 16
) (
    input  logic                     clk_i,
    input  logic                     rst_ni,
    input  logic                     clear_i,
    input  logic                     push0_i,
    input  logic                     push1_i,
    input  logic [W-1:0]             data0_i,
    input  logic [W-1:0]             data1_i,
    input  logic                     pop_i,
    output logic [W-1:0]             head_o,
    output logic [$clog2(DEPTH):0]   count_o
);
    localparam int AW = $clog2(DEPTH);

    logic [W-1:0]  mem [DEPTH];
    logic [AW-1:0] rptr, wptr, wbase;
    logic [AW:0]   count, cbase;

    assign wbase = clear_i ? '0 : wptr;
    assign cbase = clear_i ? '0 : count;

    always_ff @(posedge clk_i) begin
        if (push0_i) mem[wbase] <= data0_i;
        if (push1_i) mem[wbase + AW'(1)] <= data1_i;
    end

    always_ff @(posedge clk_i) begin
        if (!rst_ni) begin
            rptr  <= '0;
            wptr  <= '0;
            count <= '0;
        end else begin
            wptr  <= wbase + AW'(push0_i) + AW'(push1_i);
            rptr  <= (clear_i ? '0 : rptr) + AW'(pop_i);
            count <= cbase + (AW+1)'(push0_i) + (AW+1)'(push1_i) - (AW+1)'(pop_i);
        end
    end

    assign head_o  = mem[rptr];
    assign count_o = count;

`ifdef FORMAL
    // Environment: the ingress pushes only what fits and the writer never pops an empty or clearing queue.
    logic started = 1'b0;
    always_ff @(posedge clk_i) started <= 1'b1;
    always_comb begin
        if (!started) assume (!rst_ni);
        assume (!(push1_i && !push0_i));
        assume (!(pop_i && (count == '0 || clear_i)));
        assume (32'(cbase) + 32'(push0_i) + 32'(push1_i) <= DEPTH);
        if (started && rst_ni) begin
            assert (32'(count) <= DEPTH);
            assert (AW'(wptr - rptr) == AW'(count));
        end
    end

    // Order: an arbitrarily chosen pushed entry reaches the head after exactly the entries queued ahead of it.
    (* anyseq *) logic pick, second;
    logic          tracked = 1'b0;
    logic [AW:0]   ahead;
    logic [W-1:0]  tag;
    always_ff @(posedge clk_i) begin
        if (!rst_ni) begin
            tracked <= 1'b0;
        end else if (tracked && !clear_i) begin
            if (pop_i && ahead == '0) tracked <= 1'b0;
            else if (pop_i) ahead <= ahead - 1'b1;
        end else begin
            tracked <= pick && (second ? push1_i : push0_i);
            tag     <= second ? data1_i : data0_i;
            ahead   <= cbase + (AW+1)'(second) - (AW+1)'(pop_i);
        end
    end
    always_comb begin
        if (started && rst_ni && tracked) begin
            assert (ahead < count);
            assert (mem[rptr + AW'(ahead)] == tag);
            if (ahead == '0) assert (head_o == tag);
        end
    end
`else
`ifndef SYNTHESIS
    always_ff @(posedge clk_i) begin
        if (rst_ni) begin
            assert (!(push1_i && !push0_i)) else $error("second push without first");
            assert (!(pop_i && (count == 0 || clear_i))) else $error("pop from empty or clearing queue");
            assert (32'(cbase) + 32'(push0_i) + 32'(push1_i) <= DEPTH) else $error("queue overflow");
        end
    end
`endif
`endif
endmodule
