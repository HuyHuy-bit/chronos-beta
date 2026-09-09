module toolchain_smoke (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       enable_i,
    output logic [7:0] count_o
);
    always_ff @(posedge clk_i) begin
        if (!rst_ni)
            count_o <= '0;
        else if (enable_i)
            count_o <= count_o + 8'd1;
    end
endmodule
