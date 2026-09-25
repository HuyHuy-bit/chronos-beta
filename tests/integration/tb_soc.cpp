// Runs the demo SoC until firmware halts, logging every retirement ("t cycle pc next_pc"), then replays register lines
// ("c ready op addr data", scripts/rtl_check.readout) on the host port and echoes reads ("r addr value").
#include "Vchronos_soc.h"
#include "verilated.h"
#include <cinttypes>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <string>

int main(int argc, char** argv) {
    if (argc != 4) {
        std::fprintf(stderr, "usage: %s script dump max_cycles\n", argv[0]);
        return 64;
    }
    std::ifstream input(argv[1]);
    std::FILE* dump = std::fopen(argv[2], "w");
    if (!input || !dump) return 66;
    const uint64_t limit = std::strtoull(argv[3], nullptr, 10);
    Verilated::randReset(2);

    Vchronos_soc soc;
    auto clock = [&]() {
        soc.clk_i = 0;
        soc.eval();
        soc.clk_i = 1;
        soc.eval();
    };
    soc.host_req_i = 0;
    soc.rst_ni = 0;
    clock();
    clock();
    soc.rst_ni = 1;

    uint64_t cycle = 0;
    for (; !soc.halted_o && cycle < limit; ++cycle) {
        soc.clk_i = 0;
        soc.eval();
        if (soc.retire_o) std::fprintf(dump, "t %" PRIu64 " %u %u\n", cycle, soc.retire_pc_o, soc.retire_next_pc_o);
        soc.clk_i = 1;
        soc.eval();
    }
    if (!soc.halted_o) return 2;
    std::fprintf(dump, "h %u %" PRIu64 "\n", soc.result_o, cycle);

    std::string line, tag, op;
    while (std::getline(input, line)) {
        std::istringstream fields(line);
        unsigned ready, addr = 0, data = 0;
        fields >> tag >> ready >> op >> std::hex >> addr >> data;
        if (tag != "c") return 65;
        soc.host_req_i = 1;
        soc.host_we_i = op == "w";
        soc.host_addr_i = addr;
        soc.host_wdata_i = data;
        clock();
        soc.host_req_i = 0;
        if (op == "r") std::fprintf(dump, "r %u %u\n", addr, soc.host_rdata_o);
    }
    std::fclose(dump);
    soc.final();
    return 0;
}
