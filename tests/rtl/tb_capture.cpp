#include "Vchronos_capture.h"
#include "verilated.h"
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#ifndef TB_PAGE_BYTES
#error "TB_PAGE_BYTES must be defined"
#endif
#ifndef TB_SRAM_BYTES
#error "TB_SRAM_BYTES must be defined"
#endif

namespace {
struct Observation {
    unsigned slot, kind, flags;
    uint32_t payload[5];
};

struct Cycle {
    unsigned arm, stop, ready;
    std::vector<Observation> observations;
};

constexpr unsigned kPages = TB_SRAM_BYTES / TB_PAGE_BYTES;
constexpr unsigned kPageWords = TB_PAGE_BYTES / 8;
constexpr uint64_t kDrainLimit = 1000000;
}

int main(int argc, char** argv) {
    if (argc != 3) {
        std::fprintf(stderr, "usage: %s stimulus dump\n", argv[0]);
        return 64;
    }
    std::ifstream input(argv[1]);
    std::vector<Cycle> cycles;
    unsigned keep = 0x7f, drain_ready_percent = 100;
    uint64_t session = 1, config_tag = 1, seed = 1;
    std::string line;
    while (std::getline(input, line)) {
        std::istringstream fields(line);
        std::string tag;
        fields >> tag;
        if (tag == "cfg") {
            fields >> std::hex >> keep >> session >> config_tag >> std::dec >> drain_ready_percent >> seed;
        } else if (tag == "c") {
            Cycle cycle{};
            fields >> cycle.arm >> cycle.stop >> cycle.ready;
            cycles.push_back(cycle);
        } else if (tag == "o" && !cycles.empty()) {
            Observation item{};
            fields >> item.slot >> item.kind >> item.flags >> std::hex;
            for (auto& word : item.payload) fields >> word;
            cycles.back().observations.push_back(item);
        } else if (!tag.empty()) {
            std::fprintf(stderr, "bad stimulus line: %s\n", line.c_str());
            return 65;
        }
    }

    Verilated::randReset(2);
    Verilated::randSeed(static_cast<int>(seed % 1000003));
    Vchronos_capture model;
    auto clock = [&]() {
        model.clk_i = 0;
        model.eval();
        model.clk_i = 1;
        model.eval();
    };
    auto clear_inputs = [&]() {
        model.arm_i = 0;
        model.stop_i = 0;
        model.obs_valid_i = 0;
        model.obs_kind_i = 0;
        model.obs_flags_i = 0;
        for (unsigned word = 0; word < 40; ++word) model.obs_payload_i[word] = 0;
        model.rd_en_i = 0;
    };

    clear_inputs();
    model.keep_kinds_i = keep;
    model.session_i = session;
    model.config_tag_i = config_tag;
    model.sink_ready_i = 1;
    model.rst_ni = 0;
    clock();
    clock();
    model.rst_ni = 1;

    uint64_t cycle_count = 0, stop_cycle = 0, ready_cycles = 0;
    for (const auto& cycle : cycles) {
        clear_inputs();
        model.arm_i = cycle.arm;
        model.stop_i = cycle.stop;
        model.sink_ready_i = cycle.ready;
        for (const auto& item : cycle.observations) {
            model.obs_valid_i |= 1u << item.slot;
            model.obs_kind_i |= item.kind << (3 * item.slot);
            model.obs_flags_i |= item.flags << (2 * item.slot);
            for (unsigned word = 0; word < 5; ++word) model.obs_payload_i[item.slot * 5 + word] = item.payload[word];
        }
        if (cycle.stop) stop_cycle = cycle_count;
        ready_cycles += cycle.ready;
        clock();
        ++cycle_count;
    }

    clear_inputs();
    uint64_t lcg = seed;
    for (uint64_t spent = 0; model.state_o != 4 && spent < kDrainLimit; ++spent) {
        lcg = lcg * 6364136223846793005ULL + 1442695040888963407ULL;
        model.sink_ready_i = (lcg >> 33) % 100 < drain_ready_percent;
        ready_cycles += model.sink_ready_i;
        clock();
        ++cycle_count;
    }
    if (model.state_o != 4) {
        std::fprintf(stderr, "capture did not freeze\n");
        return 2;
    }
    const uint64_t frozen_cycle = cycle_count;

    std::FILE* dump = std::fopen(argv[2], "w");
    if (!dump) return 66;
    std::fprintf(dump, "result %u %u %llu %llu %llu %u %llu\n", unsigned(model.state_o), unsigned(model.storage_full_o),
                 (unsigned long long)cycle_count, (unsigned long long)stop_cycle, (unsigned long long)frozen_cycle,
                 unsigned(model.committed_o), (unsigned long long)ready_cycles);
    for (unsigned source = 0; source < 4; ++source) {
        uint64_t counters[4];
        model.acct_source_i = source;
        for (unsigned counter = 0; counter < 4; ++counter) {
            model.acct_counter_i = counter;
            model.eval();
            counters[counter] = model.acct_value_o;
        }
        std::fprintf(dump, "acct %u %llu %llu %llu %llu %u\n", source, (unsigned long long)counters[0],
                     (unsigned long long)counters[1], (unsigned long long)counters[2], (unsigned long long)counters[3],
                     unsigned(model.fifo_count_o));
    }
    for (unsigned slot = 0; slot < kPages; ++slot) {
        model.dir_slot_i = slot;
        model.eval();
        const unsigned valid = model.dir_valid_o;
        std::fprintf(dump, "dir %u %u %llu\n", slot, valid, valid ? (unsigned long long)model.dir_generation_o : 0ULL);
        if (!valid) continue;
        std::fprintf(dump, "page %u ", slot);
        for (unsigned word = 0; word < kPageWords; ++word) {
            model.rd_en_i = 1;
            model.rd_addr_i = slot * kPageWords + word;
            clock();
            uint64_t data = model.rd_data_o;
            for (unsigned byte = 0; byte < 8; ++byte) std::fprintf(dump, "%02x", unsigned((data >> (8 * byte)) & 0xff));
        }
        model.rd_en_i = 0;
        std::fprintf(dump, "\n");
    }
    std::fclose(dump);
    model.final();
    return 0;
}
