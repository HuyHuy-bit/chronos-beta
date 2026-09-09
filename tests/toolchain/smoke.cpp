#include "Vtoolchain_smoke.h"
#include <iostream>

int main() {
    Vtoolchain_smoke model;
    auto tick = [&]() {
        model.clk_i = 0;
        model.eval();
        model.clk_i = 1;
        model.eval();
    };
    model.rst_ni = 0;
    model.enable_i = 0;
    tick();
    if (model.count_o != 0) return 1;
    model.rst_ni = 1;
    unsigned expected = 0;
    for (unsigned cycle = 0; cycle < 600; ++cycle) {
        model.enable_i = cycle % 3 != 0;
        if (model.enable_i) expected = (expected + 1) & 255;
        tick();
        if (model.count_o != expected) return 2;
    }
    model.rst_ni = 0;
    tick();
    if (model.count_o != 0) return 3;
    model.final();
    std::cout << "PASS: 600 cycles, hold, wrap, and reset\n";
    return 0;
}
