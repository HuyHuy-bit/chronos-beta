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
    logic [AW-1:0] rptr, wptr;
    logic [AW:0]   count;

    always_ff @(posedge clk_i) begin
        if (push0_i) mem[wptr] <= data0_i;
        if (push1_i) mem[wptr + AW'(1)] <= data1_i;
    end

    always_ff @(posedge clk_i) begin
        if (!rst_ni || clear_i) begin
            rptr  <= '0;
            wptr  <= '0;
            count <= '0;
        end else begin
            wptr  <= wptr + AW'(push0_i) + AW'(push1_i);
            rptr  <= rptr + AW'(pop_i);
            count <= count + (AW+1)'(push0_i) + (AW+1)'(push1_i) - (AW+1)'(pop_i);
        end
    end

    assign head_o  = mem[rptr];
    assign count_o = count;

`ifndef SYNTHESIS
    always_ff @(posedge clk_i) begin
        if (rst_ni && !clear_i) begin
            assert (!(push1_i && !push0_i)) else $error("second push without first");
            assert (!(pop_i && count == 0)) else $error("pop from empty queue");
            assert (32'(count) + 32'(push0_i) + 32'(push1_i) <= DEPTH) else $error("queue overflow");
        end
    end
`endif
endmodule
