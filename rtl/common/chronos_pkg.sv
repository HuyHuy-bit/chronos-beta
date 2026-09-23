package chronos_pkg;
    typedef struct packed {
        logic [2:0]   kind;
        logic [1:0]   flags;
        logic [159:0] payload;
    } obs_t;

    typedef struct packed {
        logic [2:0]   kind;
        logic [1:0]   flags;
        logic         lane;
        logic [63:0]  seq;
        logic [63:0]  tick;
        logic [159:0] payload;
    } entry_t;

    function automatic logic [5:0] record_bytes(input logic [2:0] kind);
        case (kind)
            3'd1, 3'd2, 3'd3: record_bytes = 6'd52;
            3'd4:             record_bytes = 6'd40;
            3'd5:             record_bytes = 6'd48;
            3'd6:             record_bytes = 6'd44;
            default:          record_bytes = 6'd36;
        endcase
    endfunction

    // CRC-32/ISO-HDLC register step over 4 (wide = 0) or 8 little-endian bytes; init and final inversion are external.
    function automatic logic [31:0] crc32_step(input logic [31:0] crc, input logic [63:0] data, input logic wide);
        logic [31:0] value;
        value = crc;
        for (int i = 0; i < 64; i++) begin
            if (i < 32 || wide)
                value = {1'b0, value[31:1]} ^ ((value[0] ^ data[i]) ? 32'hEDB88320 : 32'h0);
        end
        crc32_step = value;
    endfunction
endpackage
