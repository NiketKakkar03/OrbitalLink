#include "../command_control.hpp"

#include <cassert>
#include <cstdint>
#include <iostream>
#include <limits>

namespace {
constexpr orbit::CommandAuthorization kAuthorized{true};
constexpr orbit::CommandAuthorization kUnauthorized{false};

orbit::CommandRequest command(std::uint64_t sequence, std::uint32_t type,
                              std::uint64_t argument = 0) {
    return orbit::CommandRequest{0xabcULL, sequence, 1000 + sequence, type, 1'000, argument, 99};
}

bool same_state(const orbit::FakeSourceState& a, const orbit::FakeSourceState& b) {
    return a.enabled == b.enabled && a.period_ms == b.period_ms && a.faulted == b.faulted &&
           a.generation == b.generation && a.effect_counter == b.effect_counter;
}

const orbit::CommandResult& result_of(const orbit::CommandAdmission& admission) {
    assert(admission.status == orbit::AdmissionStatus::kResult && admission.result.has_value());
    return *admission.result;
}

void assert_rejected_unchanged(orbit::CommandDispatcher& dispatcher,
                               orbit::FakeSourceControl& source,
                               const orbit::CommandRequest& request, std::uint64_t now_ms,
                               orbit::CommandAuthorization authorization, std::uint32_t reason) {
    const auto before = source.state();
    const auto result = result_of(dispatcher.enqueue(request, now_ms, authorization, source));
    assert(result.status == orbit::kResultRejected && result.reason == reason);
    assert(same_state(before, source.state()));
}

void execute_accepted(orbit::CommandDispatcher& dispatcher, orbit::FakeSourceControl& source,
                      const orbit::CommandRequest& request, std::uint64_t now_ms) {
    assert(dispatcher.enqueue(request, now_ms, kAuthorized, source).status ==
           orbit::AdmissionStatus::kQueued);
    const auto result = dispatcher.execute_one(now_ms, source);
    assert(result.has_value() && result->status == orbit::kResultAccepted);
}

void test_all_commands() {
    orbit::FakeSourceControl source;
    orbit::CommandDispatcher dispatcher(0xabcULL, 5);
    execute_accepted(dispatcher, source, command(1, orbit::kCommandDisable), 10);
    assert(!source.state().enabled);
    execute_accepted(dispatcher, source, command(2, orbit::kCommandEnable), 11);
    assert(source.state().enabled);
    execute_accepted(dispatcher, source, command(3, orbit::kCommandSetPeriod, 250), 12);
    assert(source.state().period_ms == 250);
    source.inject_fault();
    execute_accepted(dispatcher, source, command(4, orbit::kCommandClearFault), 13);
    assert(!source.state().faulted);
    const auto generation = source.state().generation;
    source.inject_fault();
    execute_accepted(dispatcher, source, command(5, orbit::kCommandReset), 14);
    assert(!source.state().faulted && source.state().generation == generation + 1);
    assert(source.state().effect_counter == 5);
}

void test_rejections_and_sequence_progression() {
    orbit::FakeSourceControl source;
    orbit::CommandDispatcher dispatcher(0xabcULL, 2, 8, 100);
    auto request = command(1, orbit::kCommandReset);
    request.role = 1;
    assert_rejected_unchanged(dispatcher, source, request, 10, kUnauthorized,
                              orbit::kRejectUnauthorized);
    assert(dispatcher.next_sequence() == 1);
    request.command_session_id = 0xdefULL;
    assert_rejected_unchanged(dispatcher, source, request, 10, kAuthorized,
                              orbit::kRejectBadSession);
    assert(dispatcher.next_sequence() == 1);
    assert_rejected_unchanged(dispatcher, source, command(2, orbit::kCommandReset), 10,
                              kAuthorized, orbit::kRejectBadSequence);
    assert(dispatcher.next_sequence() == 1);
    request = command(1, orbit::kCommandReset);
    request.deadline_monotonic_ms = 10;
    assert_rejected_unchanged(dispatcher, source, request, 10, kAuthorized, orbit::kRejectExpired);
    assert(dispatcher.next_sequence() == 2);
    assert_rejected_unchanged(dispatcher, source, command(2, orbit::kCommandSetPeriod, 1), 10,
                              kAuthorized, orbit::kRejectBadArguments);
    assert(dispatcher.next_sequence() == 3);
    request = command(3, orbit::kCommandEnable);
    request.deadline_monotonic_ms = 111;
    assert_rejected_unchanged(dispatcher, source, request, 10, kAuthorized,
                              orbit::kRejectBadArguments);
    assert(dispatcher.next_sequence() == 4);
}

void test_queue_full_and_execution_expiry() {
    orbit::FakeSourceControl source;
    orbit::CommandDispatcher dispatcher(0xabcULL, 1);
    auto first = command(1, orbit::kCommandDisable);
    first.deadline_monotonic_ms = 20;
    assert(dispatcher.enqueue(first, 10, kAuthorized, source).status ==
           orbit::AdmissionStatus::kQueued);
    const auto second = command(2, orbit::kCommandReset);
    assert_rejected_unchanged(dispatcher, source, second, 10, kAuthorized,
                              orbit::kRejectQueueFull);
    assert(dispatcher.next_sequence() == 2);
    const auto before = source.state();
    const auto expired = dispatcher.execute_one(20, source);
    assert(expired.has_value() && expired->status == orbit::kResultRejected &&
           expired->reason == orbit::kRejectExpired);
    assert(same_state(before, source.state()));
    assert(dispatcher.enqueue(second, 21, kAuthorized, source).status ==
           orbit::AdmissionStatus::kQueued);
}

void test_duplicates_conflicts_and_cache_eviction() {
    orbit::FakeSourceControl source;
    orbit::CommandDispatcher dispatcher(0xabcULL, 2, 1);
    const auto first = command(1, orbit::kCommandSetPeriod, 250);
    assert(dispatcher.enqueue(first, 10, kAuthorized, source).status ==
           orbit::AdmissionStatus::kQueued);
    assert(dispatcher.enqueue(first, 11, kAuthorized, source).status ==
           orbit::AdmissionStatus::kDuplicatePending);
    auto conflict = first;
    conflict.argument = 500;
    assert_rejected_unchanged(dispatcher, source, conflict, 11, kAuthorized,
                              orbit::kRejectBadSequence);
    assert(dispatcher.execute_one(12, source)->status == orbit::kResultAccepted);
    const auto after_first = source.state();
    assert(result_of(dispatcher.enqueue(first, 13, kAuthorized, source)).status ==
           orbit::kResultAccepted);
    assert(same_state(after_first, source.state()));
    assert_rejected_unchanged(dispatcher, source, conflict, 13, kAuthorized,
                              orbit::kRejectBadSequence);
    execute_accepted(dispatcher, source, command(2, orbit::kCommandDisable), 14);
    assert(dispatcher.cached_results() == 1);
    const auto after_second = source.state();
    const auto old = result_of(dispatcher.enqueue(first, 15, kAuthorized, source));
    assert(old.status == orbit::kResultOutcomeUnknown && old.reason == orbit::kRejectBadSequence);
    assert(same_state(after_second, source.state()));
}

void test_sequence_exhaustion() {
    orbit::FakeSourceControl source;
    const auto maximum = std::numeric_limits<std::uint64_t>::max();
    orbit::CommandDispatcher dispatcher(0xabcULL, 1, 1, 100, maximum);
    auto last = command(maximum, orbit::kCommandReset);
    last.command_id = 7;
    last.deadline_monotonic_ms = 100;
    assert(dispatcher.enqueue(last, 10, kAuthorized, source).status ==
           orbit::AdmissionStatus::kQueued);
    assert(dispatcher.exhausted());
    assert(dispatcher.execute_one(11, source)->status == orbit::kResultAccepted);
    const auto after = source.state();
    auto another = last;
    another.command_id = 8;
    const auto replay = result_of(dispatcher.enqueue(another, 12, kAuthorized, source));
    assert(replay.status == orbit::kResultRejected && replay.reason == orbit::kRejectBadSequence);
    assert(same_state(after, source.state()));
}
}  // namespace

int main() {
    test_all_commands();
    test_rejections_and_sequence_progression();
    test_queue_full_and_execution_expiry();
    test_duplicates_conflicts_and_cache_eviction();
    test_sequence_exhaustion();
    std::cout << "command control tests passed\n";
    return 0;
}
