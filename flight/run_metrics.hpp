#pragma once
#include <array>
#include <cstdint>
#include <ostream>
namespace orbit {
// Network owner only; read after join. Fixed 1ms buckets through 10 seconds.
class RunMetrics {
    std::array<std::uint64_t,10002> ack_{}, source_{};
    std::uint64_t sent_bytes_{}, ack_count_{}, source_count_{}, max_ack_ns_{};
    static void add(std::array<std::uint64_t,10002>& buckets,std::uint64_t ns) {
        auto bucket=ns/1000000; if(bucket>10000)bucket=10001; ++buckets[bucket];
    }
    static std::uint64_t percentile(const std::array<std::uint64_t,10002>& buckets,std::uint64_t count) {
        if(!count)return 0;
        const auto target=(count*95+99)/100;std::uint64_t seen=0;
        for(std::size_t i=0;i<buckets.size();++i) {seen+=buckets[i];if(seen>=target)return i+1;}
        return 10002;
    }
public:
    void sent(std::uint64_t bytes) {sent_bytes_+=bytes;}
    void acked(std::uint64_t send_to_ack_ns,std::uint64_t source_to_ack_ns=0) {
        ++ack_count_;add(ack_,send_to_ack_ns);if(send_to_ack_ns>max_ack_ns_)max_ack_ns_=send_to_ack_ns;
        if(source_to_ack_ns) {++source_count_;add(source_,source_to_ack_ns);}
    }
    void json(std::ostream& out) const {
        const auto value=[&out](std::uint64_t count,std::uint64_t p95) {
            if(!count || p95>=10002)out<<"null";else out<<p95;
        };
        out<<",\"wire_application_bytes_sent\":"<<sent_bytes_<<",\"ack_latency_samples\":"<<ack_count_
           <<",\"ack_latency_p95_upper_ms\":";
        value(ack_count_,percentile(ack_,ack_count_));
        out<<",\"ack_latency_max_ns\":"<<max_ack_ns_
           <<",\"source_to_ack_samples\":"<<source_count_
           <<",\"source_to_ack_p95_upper_ms\":";
        value(source_count_,percentile(source_,source_count_));
        out<<",\"latency_histogram_overflow_ms\":10000,\"ack_latency_overflow_samples\":"<<ack_.back()
           <<",\"source_latency_overflow_samples\":"<<source_.back();
    }
};
}
