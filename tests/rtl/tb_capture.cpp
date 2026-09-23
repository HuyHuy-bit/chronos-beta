// Replays a line-based script: "c ready op addr data" cycles with "o slot kind flags p0..p4" observations,
// "u state status_addr" waits (random sink readiness) until STATUS.state matches; reads are echoed to the dump.
#include "Vchronos_capture.h"
#include "verilated.h"
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <sstream>
#include <string>

int main(int argc, char** argv) {
    if (argc != 3) {
        std::fprintf(stderr, "usage: %s script dump\n", argv[0]);
        return 64;
    }
    std::ifstream input(argv[1]);
    std::FILE* dump = std::fopen(argv[2], "w");
    if (!input || !dump) return 66;
    unsigned ready_percent = 100;
    uint64_t lcg = 1;
    std::string line;
    std::getline(input, line);
    std::istringstream(line.substr(4)) >> ready_percent >> lcg;
    Verilated::randReset(2);
    Verilated::randSeed(static_cast<int>(lcg % 1000003));

    Vchronos_capture model;
    auto clock = [&]() {
        model.clk_i = 0;
        model.eval();
        model.clk_i = 1;
        model.eval();
        model.obs_valid_i = 0;
        model.reg_req_i = 0;
    };
    model.obs_valid_i = 0;
    model.reg_req_i = 0;
    model.sink_ready_i = 1;
    model.rst_ni = 0;
    clock();
    clock();
    model.rst_ni = 1;

    bool pending = false;
    std::string op;
    auto finish = [&]() {
        if (!pending) return;
        clock();
        if (op == "r") std::fprintf(dump, "r %u %u\n", unsigned(model.reg_addr_i), unsigned(model.reg_rdata_o));
        pending = false;
    };
    while (std::getline(input, line)) {
        std::istringstream fields(line);
        std::string tag;
        fields >> tag;
        if (tag == "o") {
            unsigned slot, kind, flags;
            fields >> slot >> kind >> flags >> std::hex;
            model.obs_valid_i |= 1u << slot;
            model.obs_kind_i = (model.obs_kind_i & ~(7u << (3 * slot))) | kind << (3 * slot);
            model.obs_flags_i = (model.obs_flags_i & ~(3u << (2 * slot))) | flags << (2 * slot);
            for (unsigned word = 0; word < 5; ++word) fields >> model.obs_payload_i[slot * 5 + word];
            continue;
        }
        finish();
        if (tag == "c") {
            unsigned ready, addr = 0, data = 0;
            fields >> ready >> op >> std::hex >> addr >> data;
            model.sink_ready_i = ready;
            model.reg_req_i = op != "-";
            model.reg_we_i = op == "w";
            model.reg_addr_i = addr;
            model.reg_wdata_i = data;
            pending = true;
        } else if (tag == "u") {
            unsigned state, status, cycles = 0;
            fields >> state >> std::hex >> status;
            do {
                lcg = lcg * 6364136223846793005ULL + 1442695040888963407ULL;
                model.sink_ready_i = (lcg >> 33) % 100 < ready_percent;
                model.reg_req_i = 1;
                model.reg_we_i = 0;
                model.reg_addr_i = status;
                clock();
                ++cycles;
            } while ((model.reg_rdata_o & 7) != state && cycles < 1000000);
            if ((model.reg_rdata_o & 7) != state) return 2;
            std::fprintf(dump, "u %u\n", cycles);
        } else if (!tag.empty()) {
            return 65;
        }
    }
    finish();
    std::fclose(dump);
    model.final();
    return 0;
}
