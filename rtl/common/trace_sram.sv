// Simple dual-port synchronous RAM: one byte-enabled write port, one registered read port.
module trace_sram #(
    parameter int WORDS = 4096
) (
    input  logic                     clk_i,
    input  logic                     we_i,
    input  logic [$clog2(WORDS)-1:0] waddr_i,
    input  logic [63:0]              wdata_i,
    input  logic [7:0]               wbe_i,
    input  logic                     re_i,
    input  logic [$clog2(WORDS)-1:0] raddr_i,
    output logic [63:0]              rdata_o
);
    logic [63:0] mem [WORDS];

    always_ff @(posedge clk_i) begin
        for (int b = 0; b < 8; b++) begin
            if (we_i && wbe_i[b]) mem[waddr_i][8*b +: 8] <= wdata_i[8*b +: 8];
        end
        if (re_i) rdata_o <= mem[raddr_i];
    end
endmodule
