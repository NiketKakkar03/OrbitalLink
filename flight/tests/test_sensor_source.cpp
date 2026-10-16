#include "../device_source.hpp"
#include <cassert>
#include <cstring>
#include <functional>
#include <iostream>
struct SensorIo:orbit::DeviceIo {
    orbit_stats status{2,320,3,2,1,1,1,0,{1,0}};
    orbit_sensor_sample record{2,2,1,17,123456,0,1,{-5000,35000,1,0,0,0,0,0}};
    std::ptrdiff_t result{64};std::size_t requested{};bool rate_ok{true};unsigned long last_control{};
    orbit_sensor_state current{2,1,100,1,3,1,0,0,4};
    bool ready(int ms)override {assert(ms==25);return true;}
    std::ptrdiff_t read_records(void* output,std::size_t bytes)override {requested=bytes;if(result==64)std::memcpy(output,&record,64);return result;}
    orbit_stats stats()override{return status;}
    orbit_sensor_events events_info()override{return {100,99,0,1,0};}
    bool set_rate(const orbit_rate& r)override{assert(r.abi_version==2 && !r.reserved[0] && !r.reserved[1]);return rate_ok;}
    bool control(unsigned long command)override{last_control=command;return true;}
    bool set_fault(const orbit_sensor_fault& fault)override{current.fault_category=fault.category;current.fault_mode=fault.mode;current.fault_parameter=fault.parameter;return true;}
    std::optional<orbit_sensor_state> sensor_state()override{return current;}
};
static bool rejects(const std::function<void()>& f){try{f();}catch(const std::exception&){return true;}return false;}
int main(){
    static_assert(sizeof(orbit_sensor_sample)==64);
    auto owned=std::make_unique<SensorIo>();auto* io=owned.get();orbit::SensorSource source(std::move(owned));
    auto records=source.read(1);assert(records.size()==1 && io->requested==64);
    auto sample=orbit::SensorSource::telemetry(records[0],123,456);
    assert(sample.category==2 && sample.device_sequence==17 && sample.values[0]==-5000 && sample.temperature_millideg_c==-5000);
    assert(orbit::decode_frame(orbit::encode_telemetry_sample(sample)).sample.values==sample.values);
    assert(source.events_info().coalesced==99);
    assert(source.set_rate(100) && !source.set_rate(0) && !source.set_rate(60001));
    assert(source.enable() && io->last_control==ORBIT_SENSOR_ENABLE);
    assert(source.disable() && io->last_control==ORBIT_SENSOR_DISABLE);
    assert(source.reset() && io->last_control==ORBIT_SENSOR_RESET);
    assert(source.clear_fault() && io->last_control==ORBIT_SENSOR_CLEAR_FAULT);
    assert(source.set_fault(2,3,-25) && io->current.fault_category==2 && io->current.fault_parameter==-25);
    assert(!source.set_fault(0,1) && !source.set_fault(1,8));
    assert(source.state().fault_category==2 && source.state().fault_mode==3 && source.state().command_effects==4);
    io->current.enabled=2;assert(rejects([&]{source.state();}));io->current.enabled=1;
    io->rate_ok=false;assert(!source.set_rate(10));
    for(auto bytes:{0,1,63,65,1025}){io->result=bytes;assert(rejects([&]{source.read(1);}));}
    io->result=-1;assert(source.read(1).empty());io->result=64;
    io->record.abi_version=1;assert(rejects([&]{source.read(1);}));io->record.abi_version=2;
    io->record.values[7]=1;assert(rejects([&]{source.read(1);}));io->record.values[7]=0;
    io->record.category=0;assert(rejects([&]{source.read(1);}));
    io->record.category=65538;assert(rejects([&]{source.read(1);}));io->record.category=2;
    io->status.dropped=2;assert(rejects([&]{source.stats();}));io->status.dropped=1;
    io->status.dead=1;assert(rejects([&]{source.stats();}));io->status.dead=0;
    io->status.queued=2;assert(rejects([&]{source.stats();}));
    std::cout<<"ABI2 source record framing, category validation and exact accounting passed\n";
}
