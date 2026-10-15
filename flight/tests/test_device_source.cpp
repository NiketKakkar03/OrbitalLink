#include "../device_source.hpp"
#include <cassert>
#include <cstring>
#include <functional>
#include <iostream>

struct MockDevice:orbit::DeviceIo {
    bool readable{true}, rate_ok{true};
    int timeout{}, rate_calls{}, reads{};
    std::uint32_t period{};
    std::ptrdiff_t returned{-2};
    std::size_t requested{};
    orbit_stats info{ORBIT_ABI_VERSION,ORBIT_FIFO_CAPACITY,0,0,0,0,0,0,{0,0}};
    std::vector<orbit_sample> records{{ORBIT_ABI_VERSION,ORBIT_SAMPLE_TEST_SOURCE,123,9,987654321,-20000,3300,{0,0}}};
    bool ready(int timeout_ms) override {timeout=timeout_ms;return readable;}
    std::ptrdiff_t read_records(void* data,std::size_t bytes) override {
        ++reads;requested=bytes;
        if(returned!=-2)return returned;
        const auto copied=std::min(bytes,records.size()*sizeof(orbit_sample));
        std::memcpy(data,records.data(),copied);return static_cast<std::ptrdiff_t>(copied);
    }
    orbit_stats stats() override {return info;}
    bool set_rate(const orbit_rate& rate) override {
        assert(rate.abi_version==ORBIT_ABI_VERSION && !rate.reserved[0] && !rate.reserved[1]);
        ++rate_calls;period=rate.period_ms;return rate_ok;
    }
};
bool fails(const std::function<void()>& operation) {
    try{operation();}catch(const std::exception&){return true;}
    return false;
}
int main() {
    auto backend=std::make_unique<MockDevice>();auto* mock=backend.get();
    orbit::DeviceSource source(std::move(backend));
    mock->readable=false;assert(source.read(1).empty() && mock->timeout==25 && mock->reads==0);
    mock->readable=true;
    const auto records=source.read(1);assert(records.size()==1 && mock->requested==sizeof(orbit_sample));
    const auto sample=orbit::DeviceSource::telemetry(records[0],444,555);
    assert(sample.spacecraft_id==1 && sample.flight_boot_id==444 && sample.telemetry_sequence==555);
    assert(sample.device_generation==9 && sample.device_sequence==123 && sample.timestamp_monotonic_ns==987654321 && sample.temperature_millideg_c==-20000 && sample.voltage_millivolts==3300);
    mock->records.resize(20,mock->records.front());assert(source.read(1000).size()==16 && mock->requested==16*sizeof(orbit_sample));
    mock->returned=-1;assert(source.read(1).empty()); // transient EAGAIN/EINTR
    mock->returned=0;assert(fails([&]{source.read(1);}));
    mock->returned=39;assert(fails([&]{source.read(1);}));
    mock->returned=80;assert(fails([&]{source.read(1);})); // overlong count
    mock->returned=-2;
    mock->records.front().abi_version=2;assert(fails([&]{source.read(1);}));
    mock->records.front().abi_version=ORBIT_ABI_VERSION;
    mock->records.front().reserved[0]=1;assert(fails([&]{source.read(1);}));
    mock->records.front().reserved[0]=0;
    mock->records.front().flags=2;assert(fails([&]{source.read(1);}));
    mock->records.front().flags=0;assert(source.read(1).size()==1); // eventual hardware source
    mock->info.dead=1;assert(fails([&]{source.stats();}));mock->info.dead=0;
    mock->info.queued=ORBIT_FIFO_CAPACITY+1;assert(fails([&]{source.stats();}));mock->info.queued=0;
    assert(!source.set_rate(0) && !source.set_rate(60001) && mock->rate_calls==0);
    assert(source.set_rate(123) && mock->rate_calls==1 && mock->period==123);

    orbit::CommandDispatcher commands;commands.open(42);
    commands.device_backend([&](const orbit::CommandRequest& request) {
        assert(request.opcode==3);
        return source.set_rate(request.argument)?orbit::CommandStatus::applied:orbit::CommandStatus::outcome_unknown;
    });
    using Status=orbit::CommandStatus;
    for(const auto opcode:{1U,2U,4U,5U,6U})
        assert(commands.submit({42,1,8,1000,opcode,0},true,0)==Status::invalid_argument);
    assert(mock->rate_calls==1 && !commands.pop_result());
    assert(commands.submit({42,1,8,1000,3,222},true,0)==Status::applied);
    assert(mock->rate_calls==1); // acceptance never runs a device operation
    commands.execute(10);auto applied=commands.pop_result();
    assert(applied && applied->status==Status::applied && mock->rate_calls==2 && mock->period==222 && !applied->snapshot);
    mock->rate_ok=false;
    assert(commands.submit({42,2,9,1000,3,333},true,0)==Status::applied);
    commands.execute(10);auto failed=commands.pop_result();
    assert(failed && failed->status==Status::outcome_unknown && commands.state().period_ms==222 && commands.state().effects==1);
    assert(commands.submit({42,3,10,1000,3,444},true,0)==Status::applied);
    commands.execute(1000);auto expired=commands.pop_result();
    assert(expired && expired->status==Status::expired && mock->rate_calls==3);
    assert(commands.submit({42,4,11,1000,3,555},true,0)==Status::applied);
    commands.close();commands.execute(10);assert(mock->rate_calls==3 && !commands.pop_result());
#ifndef __linux__
    assert(fails([]{orbit::open_device("/dev/orbit_telemetry");}));
#endif
    std::cout<<"device adapter ABI, bounded reads, source mapping and command effects passed\n";
}
