#pragma once
#if __has_include("../driver/include/orbit_telemetry.h")
#include "../driver/include/orbit_telemetry.h"
#else
#include <orbit_telemetry.h>
#endif
#if __has_include("../driver/include/orbit_sensors.h")
#include "../driver/include/orbit_sensors.h"
#else
#include <orbit_sensors.h>
#endif
#include "orbit_protocol.hpp"
#include <algorithm>
#include <cerrno>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>
#ifdef __linux__
#include <fcntl.h>
#include <poll.h>
#include <sys/stat.h>
#include <sys/ioctl.h>
#include <unistd.h>
#endif

namespace orbit {
// Acquisition owns this interface and the underlying descriptor exclusively.
// Injection tests exercise the same framing/error paths without a kernel module.
class DeviceIo {
public:
    virtual ~DeviceIo()=default;
    virtual bool ready(int timeout_ms)=0;
    virtual std::ptrdiff_t read_records(void* destination,std::size_t bytes)=0;
    virtual orbit_stats stats()=0;
    virtual bool set_rate(const orbit_rate& rate)=0;
    virtual orbit_sensor_events events_info() {return {};}
    virtual orbit_sensor_clock clock_info(bool current=false) {(void)current;return {};}
};
#ifdef __linux__
class LinuxDeviceIo final:public DeviceIo {
    int fd_{-1};
    bool sensors_{};
public:
    explicit LinuxDeviceIo(const std::string& path,bool sensors=false):sensors_(sensors) {
        fd_=::open(path.c_str(),O_RDONLY|O_NONBLOCK|O_CLOEXEC);
        if(fd_<0)throw std::runtime_error("cannot open telemetry device: "+path);
        struct stat metadata{};
        if(fstat(fd_,&metadata)!=0 || !S_ISCHR(metadata.st_mode)) {
            ::close(fd_);fd_=-1;throw std::runtime_error("telemetry source must be a character device");
        }
    }
    ~LinuxDeviceIo() override {if(fd_>=0)::close(fd_);}
    bool ready(int timeout_ms) override {
        pollfd event{fd_,POLLIN,0};
        const auto result=::poll(&event,1,timeout_ms);
        if(result<0) {
            if(errno==EINTR)return false;
            throw std::runtime_error("telemetry device poll failed");
        }
        if(event.revents&(POLLHUP|POLLERR|POLLNVAL))throw std::runtime_error("telemetry device detached or failed");
        return result>0 && (event.revents&POLLIN);
    }
    std::ptrdiff_t read_records(void* destination,std::size_t bytes) override {
        const auto result=::read(fd_,destination,bytes);
        if(result<0 && (errno==EAGAIN || errno==EINTR))return -1;
        if(result<0)throw std::runtime_error("telemetry device read failed");
        return result;
    }
    orbit_stats stats() override {
        orbit_stats result{};
        if(::ioctl(fd_,sensors_?ORBIT_SENSOR_GET_STATS:ORBIT_IOC_GET_STATS,&result)!=0)throw std::runtime_error("telemetry GET_STATS failed");
        return result;
    }
    orbit_sensor_events events_info() override {
        orbit_sensor_events result{};
        if(!sensors_ || ::ioctl(fd_,ORBIT_SENSOR_GET_EVENTS,&result)!=0)throw std::runtime_error("sensor event statistics query failed");
        return result;
    }
    orbit_sensor_clock clock_info(bool current=false) override {
        orbit_sensor_clock result{};
        if(!sensors_ || ::ioctl(fd_,current?ORBIT_SENSOR_CLOCK_CURRENT:ORBIT_SENSOR_GET_CLOCK,&result)!=0)
            throw std::runtime_error("sensor clock calibration query failed");
        return result;
    }
    bool set_rate(const orbit_rate& rate) override {
        return ::ioctl(fd_,sensors_?ORBIT_SENSOR_SET_RATE:ORBIT_IOC_SET_RATE,&rate)==0;
    }
};
#endif
inline std::unique_ptr<DeviceIo> open_device(const std::string& path) {
#ifdef __linux__
    return std::make_unique<LinuxDeviceIo>(path);
#else
    (void)path;
    throw std::invalid_argument("--source device requires Linux; run it inside the ARM64 guest");
#endif
}
class DeviceSource {
    std::unique_ptr<DeviceIo> io_;
public:
    explicit DeviceSource(std::unique_ptr<DeviceIo> io):io_(std::move(io)) {
        if(!io_)throw std::invalid_argument("missing device IO backend");
        (void)stats();
    }
    orbit_stats stats() {
        auto value=io_->stats();
        if(value.abi_version!=ORBIT_ABI_VERSION || value.capacity!=ORBIT_FIFO_CAPACITY ||
           value.reserved[0] || value.reserved[1] || value.dead || value.queued>value.capacity)
            throw std::runtime_error("invalid or detached telemetry device statistics");
        return value;
    }
    std::vector<orbit_sample> read(std::size_t maximum_records) {
        if(maximum_records==0)return {};
        if(!io_->ready(25))return {};
        orbit_sample records[16]{};
        const auto limit=std::min<std::size_t>(maximum_records,16);
        const auto bytes=io_->read_records(records,limit*sizeof(orbit_sample));
        if(bytes<0)return {};
        if(bytes==0 || static_cast<std::size_t>(bytes)>limit*sizeof(orbit_sample) || bytes%sizeof(orbit_sample)!=0)
            throw std::runtime_error("telemetry device returned an incomplete record or EOF");
        std::vector<orbit_sample> result(records,records+bytes/sizeof(orbit_sample));
        for(const auto& record:result) {
            if(record.abi_version!=ORBIT_ABI_VERSION || (record.flags&~ORBIT_SAMPLE_TEST_SOURCE) || record.reserved[0] || record.reserved[1])
                throw std::runtime_error("telemetry device record ABI validation failed");
        }
        return result;
    }
    bool set_rate(std::uint32_t period_ms) {
        if(period_ms<1 || period_ms>60000)return false;
        return io_->set_rate({ORBIT_ABI_VERSION,period_ms,{0,0}});
    }
    static TelemetrySample telemetry(const orbit_sample& record,std::uint64_t boot,std::uint64_t sequence) {
        return {1,boot,sequence,record.device_generation,record.device_sequence,record.monotonic_ns,record.temperature_mc,record.voltage_mv};
    }
};
inline std::unique_ptr<DeviceIo> open_sensor_device(const std::string& path) {
#ifdef __linux__
    return std::make_unique<LinuxDeviceIo>(path,true);
#else
    (void)path;throw std::invalid_argument("sensor MMIO driver requires Linux");
#endif
}
class SensorSource {
    std::unique_ptr<DeviceIo> io_;
public:
    orbit_sensor_events events_info() {
        const auto info=io_->events_info();
        if(info.reserved || info.pending>8 || info.coalesced>info.observations || info.overflow>info.observations)
            throw std::runtime_error("invalid sensor event counters");
        return info;
    }
    orbit_sensor_clock clock_info(bool current=false) {return io_->clock_info(current);}
    explicit SensorSource(std::unique_ptr<DeviceIo> io):io_(std::move(io)) {
        if(!io_)throw std::invalid_argument("missing sensor IO backend");
        (void)stats();
    }
    orbit_stats stats() {
        auto s=io_->stats();
        if(s.abi_version!=2 || s.dead || s.capacity!=ORBIT_SENSOR_FIFO_CAPACITY+64 || s.queued>s.capacity ||
           s.generated<s.enqueued || s.generated-s.enqueued!=s.dropped ||
           s.enqueued<s.delivered || s.enqueued-s.delivered!=s.queued)
            throw std::runtime_error("invalid multisensor driver statistics");
        return s;
    }
    bool set_rate(std::uint32_t ms) {
        if(ms<1 || ms>60000)return false;
        return io_->set_rate({2,ms,{0,0}});
    }
    std::vector<orbit_sensor_sample> read(std::size_t maximum) {
        if(!maximum || !io_->ready(25))return {};
        orbit_sensor_sample records[16]{};maximum=std::min<std::size_t>(maximum,16);
        const auto bytes=io_->read_records(records,maximum*sizeof(records[0]));
        if(bytes<0)return {};
        if(!bytes || static_cast<std::size_t>(bytes)>maximum*sizeof(records[0]) || bytes%sizeof(records[0]))
            throw std::runtime_error("incomplete multisensor driver record");
        std::vector<orbit_sensor_sample> result(records,records+bytes/sizeof(records[0]));
        for(const auto& r:result) {if(r.abi_version!=2)throw std::runtime_error("sensor ABI mismatch");(void)telemetry(r,1,1);}
        return result;
    }
    static TelemetrySample telemetry(const orbit_sensor_sample& r,std::uint64_t boot,std::uint64_t sequence) {
        if(r.category<1 || r.category>6 || r.schema>UINT16_MAX)throw std::runtime_error("sensor category/schema overflow");
        TelemetrySample sample{1,boot,sequence,r.generation,r.sequence,r.monotonic_ns,0,0};
        sample.category=static_cast<std::uint16_t>(r.category);sample.schema=static_cast<std::uint16_t>(r.schema);sample.quality=r.quality;
        for(std::size_t i=0;i<8;++i)sample.values[i]=r.values[i];
        if(sample.category==1 && sample.values[0]>=0)sample.voltage_millivolts=static_cast<std::uint32_t>(sample.values[0]);
        if(sample.category==2)sample.temperature_millideg_c=sample.values[0];
        validate_sensor(sample);return sample;
    }
};
} // namespace orbit
