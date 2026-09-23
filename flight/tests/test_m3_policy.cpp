#include "../gap_tracker.hpp"
#include "../retention_queue.hpp"
#include "../retry_policy.hpp"

#include <cassert>
#include <chrono>
#include <future>
#include <stdexcept>
#include <thread>
#include <vector>

using namespace std::chrono_literals;

namespace {

void test_drop_oldest_and_counters() {
    orbit::RetentionQueue<int> queue(3);
    assert(!queue.push(1));
    assert(!queue.push(2));
    assert(!queue.push(3));
    const auto dropped = queue.push(4);
    assert(dropped && *dropped == 1);

    assert(queue.try_pop() == 2);
    const auto drained = queue.drain(10);
    assert((drained == std::vector<int>{3, 4}));

    const auto state = queue.snapshot();
    assert(state.capacity == 3);
    assert(state.size == 0);
    assert(state.high_watermark == 3);
    assert(state.accepted == 4);
    assert(state.popped == 1);
    assert(state.drained == 2);
    assert(state.overflow_dropped == 1);
    assert(state.explicitly_discarded == 0);
}

void test_bounded_drain_and_discard() {
    orbit::RetentionQueue<int> queue(6);
    for (int value = 0; value < 6; ++value) {
        queue.push(value);
    }
    assert((queue.drain(2) == std::vector<int>{0, 1}));
    assert(queue.discard(3) == 3);
    assert(queue.try_pop() == 5);
    assert(!queue.try_pop());

    const auto state = queue.snapshot();
    assert(state.accepted == 6);
    assert(state.popped == 1);
    assert(state.drained == 2);
    assert(state.explicitly_discarded == 3);
    assert(state.size == 0);
}

void test_wait_and_close_wakeup() {
    orbit::RetentionQueue<int> queue(2);
    auto item_waiter = std::async(std::launch::async, [&queue] { return queue.wait_pop(); });
    assert(item_waiter.wait_for(20ms) == std::future_status::timeout);
    queue.push(7);
    assert(item_waiter.get() == 7);

    auto close_waiter = std::async(std::launch::async, [&queue] { return queue.wait_pop(); });
    assert(close_waiter.wait_for(20ms) == std::future_status::timeout);
    queue.close();
    assert(!close_waiter.get());
    assert(queue.snapshot().closed);

    bool rejected = false;
    try {
        queue.push(8);
    } catch (const std::logic_error&) {
        rejected = true;
    }
    assert(rejected);
}

void test_close_drains_survivors() {
    orbit::RetentionQueue<int> queue(2);
    queue.push(10);
    queue.push(11);
    queue.close();
    assert(queue.wait_pop() == 10);
    assert(queue.wait_pop() == 11);
    assert(!queue.wait_pop());
}

void test_concurrent_accounting_invariant() {
    constexpr std::size_t produced = 10'000;
    orbit::RetentionQueue<std::size_t> queue(32);
    auto consumer = std::async(std::launch::async, [&queue] {
        std::size_t consumed = 0;
        while (queue.wait_pop()) {
            ++consumed;
        }
        return consumed;
    });
    for (std::size_t value = 0; value < produced; ++value) {
        queue.push(value);
    }
    queue.close();

    const std::size_t consumed = consumer.get();
    const auto state = queue.snapshot();
    assert(state.accepted == produced);
    assert(state.size == 0);
    assert(state.popped == consumed);
    assert(state.popped + state.overflow_dropped == state.accepted);
}

void test_default_backoff_and_reset() {
    orbit::RetryPolicy retry;
    const std::vector<orbit::RetryPolicy::Duration> expected{
        250ms, 500ms, 1000ms, 2000ms, 4000ms, 5000ms, 5000ms};
    for (const auto delay : expected) {
        assert(retry.next_delay() == delay);
    }
    auto state = retry.snapshot();
    assert(state.attempts == expected.size());
    assert(state.last_nominal_delay == 5000ms);
    assert(state.last_delay == 5000ms);

    retry.reset();
    state = retry.snapshot();
    assert(state.attempts == 0);
    assert(state.last_delay == 0ms);
    assert(retry.next_delay() == 250ms);
}

void test_injected_jitter_is_bounded() {
    orbit::RetryPolicy positive(250ms, 5000ms, 40ms, [](auto) { return 500ms; });
    assert(positive.next_delay() == 290ms);

    orbit::RetryPolicy negative(250ms, 5000ms, 40ms, [](auto) { return -500ms; });
    assert(negative.next_delay() == 210ms);

    orbit::RetryPolicy capped(5000ms, 5000ms, 40ms, [](auto) { return 40ms; });
    assert(capped.next_delay() == 5000ms);
}

void test_injected_clock_produces_deterministic_deadline() {
    const orbit::RetryPolicy::TimePoint fixed_time{123s};
    orbit::RetryPolicy retry(
        250ms,
        5000ms,
        0ms,
        [](auto) { return 0ms; },
        [fixed_time] { return fixed_time; });
    assert(retry.next_deadline() == fixed_time + 250ms);
    assert(retry.snapshot().attempts == 1);
}

void test_invalid_configuration() {
    bool rejected = false;
    try {
        orbit::RetentionQueue<int> queue(0);
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    assert(rejected);

    rejected = false;
    try {
        orbit::RetryPolicy retry(0ms, 5s);
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    assert(rejected);
}

void test_gap_ranges_coalesce_and_remain_bounded() {
    orbit::GapTracker gaps(2);
    gaps.record_dropped(10);
    gaps.record_dropped(11);
    gaps.record_dropped(12);
    gaps.record_dropped(20);
    gaps.record_dropped(21);

    const auto complete = gaps.snapshot();
    assert(complete.total_dropped == 5);
    assert((complete.detailed_ranges == std::vector<orbit::DroppedSequenceRange>{
                                            {10, 12}, {20, 21}}));
    assert(complete.omitted_dropped == 0);
    assert(complete.detail_complete);

    gaps.record_dropped(30);
    gaps.record_dropped(31);
    gaps.record_dropped(40);
    const auto incomplete = gaps.snapshot();
    assert(incomplete.total_dropped == 8);
    assert(incomplete.detailed_ranges.size() == 2);
    assert(incomplete.omitted_dropped == 3);
    assert(!incomplete.detail_complete);
}

void test_gap_tracker_is_thread_safe() {
    constexpr std::size_t thread_count = 4;
    constexpr std::size_t losses_per_thread = 2'000;
    orbit::GapTracker gaps(1);
    std::vector<std::thread> threads;
    for (std::size_t thread = 0; thread < thread_count; ++thread) {
        threads.emplace_back([&gaps, thread] {
            const std::uint64_t base = static_cast<std::uint64_t>(thread) << 32U;
            for (std::size_t index = 0; index < losses_per_thread; ++index) {
                gaps.record_dropped(base + index * 2);
            }
        });
    }
    for (auto& thread : threads) {
        thread.join();
    }

    const auto state = gaps.snapshot();
    assert(state.total_dropped == thread_count * losses_per_thread);
    assert(state.detailed_ranges.size() == 1);
    assert(state.omitted_dropped == state.total_dropped - 1);
    assert(!state.detail_complete);
}

void test_gap_tracker_rejects_zero_capacity() {
    bool rejected = false;
    try {
        orbit::GapTracker gaps(0);
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    assert(rejected);
}

}  // namespace

int main() {
    test_drop_oldest_and_counters();
    test_bounded_drain_and_discard();
    test_wait_and_close_wakeup();
    test_close_drains_survivors();
    test_concurrent_accounting_invariant();
    test_default_backoff_and_reset();
    test_injected_jitter_is_bounded();
    test_injected_clock_produces_deterministic_deadline();
    test_invalid_configuration();
    test_gap_ranges_coalesce_and_remain_bounded();
    test_gap_tracker_is_thread_safe();
    test_gap_tracker_rejects_zero_capacity();
}
