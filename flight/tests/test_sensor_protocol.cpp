#include "../orbit_protocol.hpp"
#include <cassert>
#include <fstream>
#include <iterator>
#include <iostream>
#include <functional>

bool rejects(const std::function<void()>& operation) {
    try {operation();} catch(const orbit::ProtocolError&) {return true;} return false;
}
int main() {
    std::ifstream fixture("protocol/fixtures/sensor_sample_v1.json");
    assert(fixture.good());
    std::string text((std::istreambuf_iterator<char>(fixture)),{});
    auto start=text.find("\"frame_hex\": \"");assert(start!=std::string::npos);start+=14;
    const auto end=text.find('"',start);std::vector<std::uint8_t> golden;
    for(auto i=start;i<end;i+=2)golden.push_back(static_cast<std::uint8_t>(std::stoul(text.substr(i,2),nullptr,16)));
    orbit::TelemetrySample sample{1,UINT64_MAX,42,7,99,UINT64_MAX,0,3300};
    sample.category=1;sample.values={3300,-200,800,4000,4500,1,0,0};
    const auto frame=orbit::encode_telemetry_sample(sample);
    assert(frame==golden && frame.size()==104);
    auto decoded=orbit::decode_frame(frame);assert(decoded.header.message_type==9 && decoded.header.payload_length==76);
    assert(decoded.sample.values==sample.values && decoded.sample.flight_boot_id==UINT64_MAX && decoded.sample.voltage_millivolts==3300);
    orbit::FrameParser parser;unsigned count=0;
    for(const auto byte:frame)count+=parser.feed({byte}).size();parser.finish();assert(count==1);
    for(auto category=1U;category<=6;++category) {
        auto value=sample;value.category=category;value.values={};
        if(category==6)value.values={8,2,2,1,0,0,0,0};
        assert(orbit::decode_frame(orbit::encode_telemetry_sample(value)).sample.category==category);
    }
    auto invalid=sample;invalid.schema=2;assert(rejects([&]{orbit::encode_telemetry_sample(invalid);}));
    invalid=sample;invalid.quality=2;assert(rejects([&]{orbit::encode_telemetry_sample(invalid);}));
    invalid=sample;invalid.category=7;assert(rejects([&]{orbit::encode_telemetry_sample(invalid);}));
    invalid=sample;invalid.values[7]=1;assert(rejects([&]{orbit::encode_telemetry_sample(invalid);}));
    invalid=sample;invalid.values[5]=2;assert(rejects([&]{orbit::encode_telemetry_sample(invalid);}));
    for(auto offset:{65U,67U,71U,95U,103U}) {
        auto malformed=frame;malformed[offset]=9;assert(rejects([&]{orbit::decode_frame(malformed);}));
    }
    std::cout<<"multisensor shared fixture, stream and category validation passed\n";
}
