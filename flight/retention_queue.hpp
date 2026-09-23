#pragma once

#include <algorithm>
#include <condition_variable>
#include <cstddef>
#include <deque>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <utility>
#include <vector>

namespace orbit {

struct RetentionQueueSnapshot {
    std::size_t capacity{};
    std::size_t size{};
    std::size_t high_watermark{};
    std::size_t accepted{};
    std::size_t popped{};
    std::size_t drained{};
    std::size_t overflow_dropped{};
    std::size_t explicitly_discarded{};
    bool closed{};
};

// A bounded, multi-producer/multi-consumer FIFO. When full, push() retains the
// newest item and returns the oldest item that was displaced.
template <typename T>
class RetentionQueue {
public:
    explicit RetentionQueue(std::size_t capacity) : capacity_(capacity) {
        if (capacity == 0) {
            throw std::invalid_argument("retention queue capacity must be positive");
        }
    }

    RetentionQueue(const RetentionQueue&) = delete;
    RetentionQueue& operator=(const RetentionQueue&) = delete;

    std::optional<T> push(T item) {
        std::optional<T> dropped;
        {
            std::lock_guard lock(mutex_);
            if (closed_) {
                throw std::logic_error("cannot push to a closed retention queue");
            }
            ++accepted_;
            if (items_.size() == capacity_) {
                dropped.emplace(std::move(items_.front()));
                items_.pop_front();
                ++overflow_dropped_;
            }
            items_.push_back(std::move(item));
            high_watermark_ = std::max(high_watermark_, items_.size());
        }
        available_.notify_one();
        return dropped;
    }

    std::optional<T> try_pop() {
        std::lock_guard lock(mutex_);
        return pop_locked();
    }

    // Waits until an item is available or close() is called. Items already in
    // the queue remain consumable after close(); nullopt means closed and empty.
    std::optional<T> wait_pop() {
        std::unique_lock lock(mutex_);
        available_.wait(lock, [this] { return closed_ || !items_.empty(); });
        return pop_locked();
    }

    // Removes at most max_items in FIFO order without waiting.
    std::vector<T> drain(std::size_t max_items) {
        std::lock_guard lock(mutex_);
        const std::size_t count = std::min(max_items, items_.size());
        std::vector<T> result;
        result.reserve(count);
        for (std::size_t index = 0; index < count; ++index) {
            result.push_back(std::move(items_.front()));
            items_.pop_front();
        }
        drained_ += count;
        return result;
    }

    // Drops at most max_items from the oldest end and returns the exact count.
    std::size_t discard(std::size_t max_items) {
        std::lock_guard lock(mutex_);
        const std::size_t count = std::min(max_items, items_.size());
        for (std::size_t index = 0; index < count; ++index) {
            items_.pop_front();
        }
        explicitly_discarded_ += count;
        return count;
    }

    void close() {
        {
            std::lock_guard lock(mutex_);
            closed_ = true;
        }
        available_.notify_all();
    }

    RetentionQueueSnapshot snapshot() const {
        std::lock_guard lock(mutex_);
        return RetentionQueueSnapshot{
            capacity_,
            items_.size(),
            high_watermark_,
            accepted_,
            popped_,
            drained_,
            overflow_dropped_,
            explicitly_discarded_,
            closed_,
        };
    }

private:
    std::optional<T> pop_locked() {
        if (items_.empty()) {
            return std::nullopt;
        }
        T item = std::move(items_.front());
        items_.pop_front();
        ++popped_;
        return item;
    }

    const std::size_t capacity_;
    mutable std::mutex mutex_;
    std::condition_variable available_;
    std::deque<T> items_;
    std::size_t high_watermark_{};
    std::size_t accepted_{};
    std::size_t popped_{};
    std::size_t drained_{};
    std::size_t overflow_dropped_{};
    std::size_t explicitly_discarded_{};
    bool closed_{};
};

}  // namespace orbit
