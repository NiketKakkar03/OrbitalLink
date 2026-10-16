#pragma once
#if __has_include("../simulation/orbit_scenario.h")
#include "../simulation/orbit_scenario.h"
#include "../simulation/orbit_events.h"
#else
#include <orbit_scenario.h>
#include <orbit_events.h>
#endif
#include "orbit_protocol.hpp"
namespace orbit {
inline TelemetrySample scenario_sample(const orbit_scenario_record& record,
                                       std::uint64_t boot,std::uint64_t sequence,
                                       std::uint32_t device_sequence,std::uint64_t now,
                                       std::uint32_t generation=1) {
    TelemetrySample sample{1,boot,sequence,generation,device_sequence,now,0,0};
    sample.category=static_cast<std::uint16_t>(record.category);sample.schema=1;sample.quality=record.quality;
    for(std::size_t i=0;i<8;++i)sample.values[i]=record.values[i];
    if(sample.category==1 && sample.values[0]>=0)sample.voltage_millivolts=static_cast<std::uint32_t>(sample.values[0]);
    if(sample.category==2)sample.temperature_millideg_c=sample.values[0];
    return sample;
}
}
