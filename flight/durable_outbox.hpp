#pragma once
#include "orbit_protocol.hpp"
#include "retention_queue.hpp"
#include <sqlite3.h>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

namespace orbit {
struct DurableLoss {
    std::uint64_t id{}, boot_id{}, first{}, last{}, count{};
    bool detail_complete{true};
};
struct DurableAccounting {
    std::uint64_t generated{}, acknowledged{}, dropped{}, loss_acknowledged{}, retained{};
};
// One owner, FULL synchronous rollback-journal transactions. Claimed telemetry
// and claimed loss reports remain on disk until an authenticated matching ACK.
class DurableOutbox {
public:
    DurableOutbox(const std::string& path, std::uint64_t bytes, std::size_t capacity);
    ~DurableOutbox();
    DurableOutbox(const DurableOutbox&) = delete;
    DurableOutbox& operator=(const DurableOutbox&) = delete;
    std::vector<TelemetrySample> push(const TelemetrySample& sample);
    std::optional<TelemetrySample> claim();
    void acknowledge(const TelemetrySample& sample);
    std::optional<DurableLoss> claim_loss();
    void acknowledge_loss(std::uint64_t id);
    std::vector<DurableLoss> losses() const;
    RetentionQueueSnapshot snapshot() const;
    DurableAccounting accounting() const;
    std::uint64_t database_limit_bytes() const { return database_limit_; }
private:
    void exec(const std::string& sql) const;
    std::uint64_t scalar(const std::string& sql) const;
    void record_loss(const TelemetrySample& sample);
    std::vector<DurableLoss> losses_locked() const;
    sqlite3* db_{};
    int lock_fd_{-1};
    mutable std::mutex mutex_;
    std::optional<std::uint64_t> claimed_, claimed_loss_;
    std::size_t capacity_, high_watermark_{}, accepted_{}, popped_{}, dropped_{};
    std::uint64_t database_limit_{};
};
} // namespace orbit
