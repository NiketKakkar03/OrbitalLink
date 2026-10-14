#pragma once

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <stdexcept>
#include <utility>

namespace orbit {

struct RetryPolicySnapshot {
    std::size_t attempts{};
    std::chrono::milliseconds last_nominal_delay{};
    std::chrono::milliseconds last_delay{};
};

class RetryPolicy {
public:
    using Duration = std::chrono::milliseconds;
    using Clock = std::chrono::steady_clock;
    using TimePoint = Clock::time_point;
    // Called with max_jitter and may return a signed offset. Out-of-range
    // provider values are clamped so jitter can never exceed its configured bound.
    using JitterProvider = std::function<Duration(Duration max_jitter)>;
    using ClockProvider = std::function<TimePoint()>;

    explicit RetryPolicy(
        Duration initial_delay = Duration{250},
        Duration maximum_delay = Duration{5000},
        Duration max_jitter = Duration{0},
        JitterProvider jitter_provider = [](Duration) { return Duration{0}; },
        ClockProvider clock_provider = [] { return Clock::now(); })
        : initial_delay_(initial_delay),
          maximum_delay_(maximum_delay),
          max_jitter_(max_jitter),
          jitter_provider_(std::move(jitter_provider)),
          clock_provider_(std::move(clock_provider)) {
        if (initial_delay_.count() <= 0 || maximum_delay_ < initial_delay_ ||
            max_jitter_.count() < 0 || !jitter_provider_ || !clock_provider_) {
            throw std::invalid_argument("invalid retry policy configuration");
        }
    }

    TimePoint next_deadline() {
        const TimePoint now = clock_provider_();
        return now + next_delay();
    }

    Duration next_delay() {
        Duration nominal = initial_delay_;
        for (std::size_t index = 0; index < attempts_ && nominal < maximum_delay_; ++index) {
            nominal = std::min(maximum_delay_, Duration{nominal.count() * 2});
        }

        Duration jitter = jitter_provider_(max_jitter_);
        jitter = std::clamp(jitter, -max_jitter_, max_jitter_);
        const Duration delay = std::clamp(nominal + jitter, Duration{0}, maximum_delay_);

        ++attempts_;
        last_nominal_delay_ = nominal;
        last_delay_ = delay;
        return delay;
    }

    void reset() noexcept {
        attempts_ = 0;
        last_nominal_delay_ = Duration{0};
        last_delay_ = Duration{0};
    }

    RetryPolicySnapshot snapshot() const noexcept {
        return RetryPolicySnapshot{attempts_, last_nominal_delay_, last_delay_};
    }

private:
    Duration initial_delay_;
    Duration maximum_delay_;
    Duration max_jitter_;
    JitterProvider jitter_provider_;
    ClockProvider clock_provider_;
    std::size_t attempts_{};
    Duration last_nominal_delay_{};
    Duration last_delay_{};
};

}  // namespace orbit
