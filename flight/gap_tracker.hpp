#pragma once

#include <cstddef>
#include <cstdint>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <vector>

namespace orbit {

struct DroppedSequenceRange {
    std::uint64_t first{};
    std::uint64_t last{};

    bool operator==(const DroppedSequenceRange&) const = default;
};

struct GapTrackerSnapshot {
    std::uint64_t total_dropped{};
    std::vector<DroppedSequenceRange> detailed_ranges;
    std::uint64_t omitted_dropped{};
    bool detail_complete{};
};

// Retains a bounded summary of dropped telemetry sequence numbers. Calls are
// expected in sequence order; adjacent losses coalesce without consuming an
// additional detailed-range slot.
class GapTracker {
public:
    explicit GapTracker(std::size_t maximum_detailed_ranges)
        : maximum_detailed_ranges_(maximum_detailed_ranges) {
        if (maximum_detailed_ranges == 0) {
            throw std::invalid_argument("gap tracker range capacity must be positive");
        }
    }

    GapTracker(const GapTracker&) = delete;
    GapTracker& operator=(const GapTracker&) = delete;

    void record_dropped(std::uint64_t sequence) {
        std::lock_guard lock(mutex_);
        ++total_dropped_;

        if (!detailed_ranges_.empty()) {
            auto& last_range = detailed_ranges_.back();
            if (last_range.last != std::numeric_limits<std::uint64_t>::max() &&
                sequence == last_range.last + 1) {
                last_range.last = sequence;
                return;
            }
        }

        if (detailed_ranges_.size() < maximum_detailed_ranges_) {
            detailed_ranges_.push_back(DroppedSequenceRange{sequence, sequence});
            return;
        }

        ++omitted_dropped_;
    }

    GapTrackerSnapshot snapshot() const {
        std::lock_guard lock(mutex_);
        return GapTrackerSnapshot{
            total_dropped_,
            detailed_ranges_,
            omitted_dropped_,
            omitted_dropped_ == 0,
        };
    }

private:
    const std::size_t maximum_detailed_ranges_;
    mutable std::mutex mutex_;
    std::vector<DroppedSequenceRange> detailed_ranges_;
    std::uint64_t total_dropped_{};
    std::uint64_t omitted_dropped_{};
};

}  // namespace orbit
