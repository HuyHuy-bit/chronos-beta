// Simulation SoC: upstream Ibex ("small" configuration, RVFI) with one RAM, a halt register, and Chronos on the data bus.
// Map: RAM at 0 (reset vector 0x80), Chronos registers at 0x1000_0000, halt/result at 0x2000_0000.
// The host port reaches the Chronos registers directly (frozen readout); it is used only while firmware is halted.
module chronos_soc #(
    parameter int    RAM_BYTES  = 65536,
    parameter int    PAGE_BYTES = 1024,
    parameter string FIRMWARE   = "firmware.hex"
) (
    input  logic        clk_i,
    input  logic        rst_ni,
    input  logic        host_req_i,
    input  logic        host_we_i,
    input  logic [8:0]  host_addr_i,
    input  logic [31:0] host_wdata_i,
    output logic [31:0] host_rdata_o,
    output logic        halted_o,
    output logic [31:0] result_o,
    output logic        retire_o,
    output logic [31:0] retire_pc_o,
    output logic [31:0] retire_next_pc_o
);
    localparam int AW = $clog2(RAM_BYTES / 4);

    logic              instr_req, instr_rvalid, instr_err, data_req, data_we, data_rvalid, data_err, chronos_q;
    logic              ram_sel, chronos_sel, halt_sel, pending, pending_we;
    logic [31:0]       instr_addr, instr_rdata, data_addr, data_wdata, data_rdata, ram_q, chronos_rdata, txn, pending_txn;
    logic [3:0]        data_be;
    logic [31:0]       ram [RAM_BYTES / 4];
    logic              rvfi_valid, rvfi_trap, rvfi_intr;
    logic [31:0]       rvfi_insn, rvfi_pc_rdata, rvfi_pc_wdata;
    logic [63:0]       boundary, epoch;
    logic [7:0]        obs_valid;
    logic [7:0][2:0]   obs_kind;
    logic [7:0][1:0]   obs_flags;
    logic [7:0][159:0] obs_payload;

    initial $readmemh(FIRMWARE, ram);

    ibex_top #(
        .RV32M  (ibex_pkg::RV32MFast),
        .RV32B  (ibex_pkg::RV32BNone),
        .RV32ZC (ibex_pkg::RV32Zca)
    ) cpu (
        .clk_i, .rst_ni,
        .test_en_i              (1'b0),
        .ram_cfg_icache_tag_i   ('0),
        .ram_cfg_icache_data_i  ('0),
        .cheriot_enable_i       (ibex_pkg::IbexMuBiOff),
        .hart_id_i              (32'd0),
        .boot_addr_i            (32'd0),
        .trvk_heap_base_addr_i  (32'd0),
        .instr_req_o            (instr_req),
        .instr_gnt_i            (instr_req),
        .instr_rvalid_i         (instr_rvalid),
        .instr_addr_o           (instr_addr),
        .instr_rdata_i          (instr_rdata),
        .instr_rdata_intg_i     (7'd0),
        .instr_err_i            (instr_err),
        .data_req_o             (data_req),
        .data_gnt_i             (data_req),
        .data_rvalid_i          (data_rvalid),
        .data_we_o              (data_we),
        .data_be_o              (data_be),
        .data_addr_o            (data_addr),
        .data_wdata_o           (data_wdata),
        .data_rdata_i           (data_rdata),
        .data_rdata_intg_i      (7'd0),
        .data_tag_i             (1'b0),
        .data_err_i             (data_err),
        .trvk_revbm_gnt_i       (1'b0),
        .trvk_revbm_rvalid_i    (1'b0),
        .trvk_revbm_rdata_i     (32'd0),
        .trvk_revbm_rdata_intg_i(7'd0),
        .trvk_revbm_err_i       (1'b0),
        .irq_software_i         (1'b0),
        .irq_timer_i            (1'b0),
        .irq_external_i         (1'b0),
        .irq_fast_i             (15'd0),
        .irq_nm_i               (1'b0),
        .scramble_key_valid_i   (1'b0),
        .scramble_key_i         ('0),
        .scramble_nonce_i       ('0),
        .debug_req_i            (1'b0),
        .rvfi_valid,
        .rvfi_insn,
        .rvfi_trap,
        .rvfi_intr,
        .rvfi_pc_rdata,
        .rvfi_pc_wdata,
        .fetch_enable_i         (ibex_pkg::IbexMuBiOn),
        .mcounteren_writable_i  (ibex_pkg::IbexMuBiOff),
        .scan_rst_ni            (1'b1)
    );

    // Memory: grant at once, respond the next cycle; fetches outside RAM and unmapped data addresses respond with an error.
    assign ram_sel     = data_addr < RAM_BYTES;
    assign chronos_sel = data_addr[31:12] == 20'h10000;
    assign halt_sel    = data_addr == 32'h2000_0000;
    assign data_rdata  = chronos_q ? chronos_rdata : ram_q;

    always_ff @(posedge clk_i) begin
        instr_rdata <= ram[instr_addr[AW+1:2]];
        ram_q       <= ram[data_addr[AW+1:2]];
        if (data_req && data_we && ram_sel)
            for (int b = 0; b < 4; b++) if (data_be[b]) ram[data_addr[AW+1:2]][8*b +: 8] <= data_wdata[8*b +: 8];
        if (!rst_ni) begin
            instr_rvalid <= 1'b0;
            instr_err    <= 1'b0;
            data_rvalid  <= 1'b0;
            halted_o     <= 1'b0;
        end else begin
            instr_rvalid <= instr_req;
            instr_err    <= instr_req && instr_addr >= RAM_BYTES;
            data_rvalid  <= data_req;
            chronos_q    <= chronos_sel;
            data_err     <= data_req && !(ram_sel || chronos_sel || halt_sel);
            if (data_req && data_we && halt_sel) begin
                halted_o <= 1'b1;
                result_o <= data_wdata;
            end
        end
    end

    // Observation adapter. A retirement is an RVFI record without a trap; the first instruction of every trap handler
    // (rvfi_intr) opens a new execution-boundary epoch. Data-bus requests are observed at grant and completions at rvalid.
    assign epoch = boundary + 64'(rvfi_valid && rvfi_intr);

    always_ff @(posedge clk_i) begin
        if (!rst_ni) begin
            boundary <= 64'd0;
            txn      <= 32'd0;
            pending  <= 1'b0;
        end else begin
            if (rvfi_valid) boundary <= epoch;
            if (data_req) begin
                txn         <= txn + 32'd1;
                pending_txn <= txn;
                pending_we  <= data_we;
            end
            pending <= data_req || (pending && !data_rvalid);
        end
    end

    always_comb begin
        obs_valid      = '0;
        obs_kind       = '0;
        obs_flags      = '0;
        obs_payload    = '0;
        obs_valid[0]   = rvfi_valid && !rvfi_trap;
        obs_kind[0]    = 3'd1;
        obs_payload[0] = {epoch, 32'd4, rvfi_pc_wdata, rvfi_pc_rdata};
        obs_valid[2]   = data_rvalid;
        obs_kind[2]    = 3'd6;
        obs_flags[2]   = {1'b0, !pending_we};
        obs_payload[2] = {64'd0, 31'd0, data_err, pending_we ? 32'd0 : data_rdata, pending_txn};
        obs_valid[3]   = data_req;
        obs_kind[3]    = 3'd5;
        obs_payload[3] = {32'd0, 16'd0, 4'd0, data_be, 7'd0, data_we, data_we ? data_wdata : 32'd0, data_addr, txn};
    end

    chronos_capture #(.PAGE_BYTES(PAGE_BYTES)) chronos (
        .clk_i, .rst_ni,
        .sink_ready_i (1'b1),
        .obs_valid_i  (obs_valid),
        .obs_kind_i   (obs_kind),
        .obs_flags_i  (obs_flags),
        .obs_payload_i(obs_payload),
        .reg_req_i    (host_req_i || (data_req && chronos_sel)),
        .reg_we_i     (host_req_i ? host_we_i : data_we),
        .reg_addr_i   (host_req_i ? host_addr_i : data_addr[8:0]),
        .reg_wdata_i  (host_req_i ? host_wdata_i : data_wdata),
        .reg_rdata_o  (chronos_rdata)
    );

    assign host_rdata_o     = chronos_rdata;
    assign retire_o         = obs_valid[0];
    assign retire_pc_o      = rvfi_pc_rdata;
    assign retire_next_pc_o = rvfi_pc_wdata;

`ifndef SYNTHESIS
    always_ff @(posedge clk_i) begin
        if (rst_ni) begin
            assert (!(rvfi_valid && rvfi_insn[1:0] != 2'b11)) else $error("16-bit instruction outside the RV32 profile");
            assert (!(data_req && pending && !data_rvalid)) else $error("second outstanding data request");
            assert (!(host_req_i && data_req && chronos_sel)) else $error("host and firmware Chronos access collide");
            assert (!(data_req && chronos_sel && data_be != 4'hF)) else $error("partial Chronos register access");
        end
    end
`endif
endmodule
