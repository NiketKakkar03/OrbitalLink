#include "../scenario_source.hpp"
#include "../../simulation/orbit_events.h"
#include <array>
#include <cassert>
#include <iostream>
int main() {
    std::array<unsigned,7> counts{};unsigned events=0;
    for(std::uint64_t tick=0;tick<600;++tick) {
        orbit_scenario_record a{},e{},again{},again_e{};
        const auto event=orbit_scenario_tick(tick,42,&a,&e);
        assert(event==orbit_scenario_tick(tick,42,&again,&again_e));
        assert(a.category==again.category && a.quality==0);
        for(int i=0;i<8;++i)assert(a.values[i]==again.values[i]);
        ++counts[a.category];events+=event;
        const auto sample=orbit::scenario_sample(a,123,tick+1,static_cast<std::uint32_t>(tick),1000+tick);
        const auto decoded=orbit::decode_frame(orbit::encode_telemetry_sample(sample));
        assert(decoded.sample.category==sample.category && decoded.sample.values==sample.values);
        if(a.category==1) {
            assert(a.values[2]>=100 && a.values[2]<=900);
            assert((a.values[3]==0)==(a.values[5]==1));
            if(tick>=100 && tick<400)assert(a.values[1]>0);
        }
        if(a.category==3 && tick>=200 && tick<260)assert(a.values[4]==1 && a.values[3]>1000);
        if(a.category==5 && tick>=200 && tick<260)assert(a.values[0]==0 && a.values[2]==1);
        if(event)assert(e.category==6 && e.values[3]==1);
    }
    assert(counts[1]==120 && counts[2]==60 && counts[3]==300 && counts[4]==60 && counts[5]==60);
    assert(events==6);
    orbit_event_accumulator queue{};orbit_scenario_record warning{6,0,{4,2,2,1,0,0,0,0}},report{};
    for(int i=0;i<100;++i)assert(orbit_event_push(&queue,&warning));
    assert(queue.count==1 && queue.coalesced==99 && queue.observations==100 && queue.overflow==0);
    assert(orbit_event_pop(&queue,&report) && report.values[3]==100);
    warning.values[3]=1;assert(orbit_event_push(&queue,&warning));
    assert(report.values[3]==100); // published copy is immutable
    for(int i=1;i<8;++i){warning.values[0]=i+10;assert(orbit_event_push(&queue,&warning));}
    warning.values[0]=99;assert(!orbit_event_push(&queue,&warning) && queue.overflow==1 && queue.count==8);

    std::cout<<"scenario categories, repeatability, eclipse/power/thermal and pointing/payload interactions passed\n";
}
