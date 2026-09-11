#include <cassert>
#include <cstdint>
#include <iostream>

bool is_valid_temperature_millideg_c(std::int32_t temperature_millideg_c) {
    return temperature_millideg_c >= -100000 &&
           temperature_millideg_c <= 150000;
}

int main() {
    assert(is_valid_temperature_millideg_c(25125));
    assert(is_valid_temperature_millideg_c(-5250));
    assert(is_valid_temperature_millideg_c(-100000));
    assert(is_valid_temperature_millideg_c(150000));

    assert(!is_valid_temperature_millideg_c(-100001));
    assert(!is_valid_temperature_millideg_c(150001));
    assert(!is_valid_temperature_millideg_c(999999));

    std::cout << "C++ temperature tests passed\n";
    return 0;
}