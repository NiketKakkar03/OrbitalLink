#pragma once

#include "orbit_protocol.hpp"

#include <cstddef>
#include <cstdint>
#include <deque>
#include <limits>
#include <optional>
#include <unordered_map>
#include <utility>

namespace orbit {

struct FakeSourceState {
    bool enabled{true};
    std::uint64_t period_ms{100};
    bool faulted{false};
    std::uint64_t generation{7};
    std::uint64_t effect_counter{0};
};

class FakeSourceControl {
  public:
    explicit FakeSourceControl(std::uint64_t initial_period_ms = 100) {
        state_.period_ms = initial_period_ms;
    }
    const FakeSourceState& state() const { return state_; }
    void inject_fault() { state_.faulted = true; }

    CommandResult apply(const CommandRequest& request) {
        switch (request.command_type) {
            case kCommandEnable: state_.enabled = true; break;
            case kCommandDisable: state_.enabled = false; break;
            case kCommandSetPeriod: state_.period_ms = request.argument; break;
            case kCommandReset:
                ++state_.generation;
                state_.faulted = false;
                break;
            case kCommandClearFault: state_.faulted = false; break;
            default: break;
        }
        ++state_.effect_counter;
        return result(request, kResultAccepted, 0);
    }

    CommandResult result(const CommandRequest& request, std::uint32_t status,
                         std::uint32_t reason) const {
        return CommandResult{request.command_session_id, request.command_sequence, request.command_id,
                             request.command_type, status, reason, state_.effect_counter,
                             state_.generation, static_cast<std::uint32_t>(state_.period_ms)};
    }

  private:
    FakeSourceState state_;
};

struct CommandAuthorization {
    bool may_control{false};
};

enum class AdmissionStatus { kQueued, kDuplicatePending, kResult };

struct CommandAdmission {
    AdmissionStatus status{AdmissionStatus::kResult};
    std::optional<CommandResult> result;
};

class CommandDispatcher {
  public:
    static constexpr std::uint64_t kDefaultValidityHorizonMs = 60'000;

    CommandDispatcher(std::uint64_t session_id, std::size_t queue_capacity,
                      std::size_t result_capacity = 64,
                      std::uint64_t validity_horizon_ms = kDefaultValidityHorizonMs,
                      std::uint64_t initial_sequence = 1)
        : session_id_(session_id), queue_capacity_(queue_capacity),
          result_capacity_(result_capacity), validity_horizon_ms_(validity_horizon_ms),
          next_sequence_(initial_sequence) {}

    CommandAdmission enqueue(const CommandRequest& request, std::uint64_t now_ms,
                             CommandAuthorization authorization,
                             const FakeSourceControl& source) {
        if (!authorization.may_control)
            return with_result(rejected(request, kRejectUnauthorized, source));
        if (request.command_session_id != session_id_)
            return with_result(rejected(request, kRejectBadSession, source));

        if (request.command_sequence < next_sequence_ || exhausted_)
            return replay_result(request, source);
        if (request.command_sequence > next_sequence_)
            return with_result(rejected(request, kRejectBadSequence, source));

        if (is_expired(request, now_ms))
            return consume_rejection(request, kRejectExpired, source);
        if (deadline_too_far(request, now_ms) || !valid_arguments(request))
            return consume_rejection(request, kRejectBadArguments, source);
        if (queue_.size() >= queue_capacity_)
            return with_result(rejected(request, kRejectQueueFull, source));

        queue_.push_back(request);
        pending_.emplace(request.command_sequence, request);
        advance_sequence();
        return CommandAdmission{AdmissionStatus::kQueued, std::nullopt};
    }

    std::optional<CommandResult> execute_one(std::uint64_t now_ms, FakeSourceControl& source) {
        if (queue_.empty()) return std::nullopt;
        const CommandRequest request = queue_.front();
        queue_.pop_front();
        pending_.erase(request.command_sequence);
        CommandResult result = is_expired(request, now_ms)
                                   ? rejected(request, kRejectExpired, source)
                                   : source.apply(request);
        cache_result(request, result);
        return result;
    }

    std::size_t queued() const { return queue_.size(); }
    std::size_t cached_results() const { return results_.size(); }
    std::uint64_t next_sequence() const { return next_sequence_; }
    bool exhausted() const { return exhausted_; }

  private:
    struct CachedResult { CommandRequest request; CommandResult result; };

    static bool same_request(const CommandRequest& left, const CommandRequest& right) {
        return left.command_session_id == right.command_session_id &&
               left.command_sequence == right.command_sequence && left.command_id == right.command_id &&
               left.command_type == right.command_type &&
               left.deadline_monotonic_ms == right.deadline_monotonic_ms &&
               left.argument == right.argument && left.role == right.role;
    }

    static bool valid_arguments(const CommandRequest& request) {
        switch (request.command_type) {
            case kCommandEnable:
            case kCommandDisable:
            case kCommandReset:
            case kCommandClearFault: return request.argument == 0;
            case kCommandSetPeriod: return request.argument >= 10 && request.argument <= 60'000;
            default: return false;
        }
    }

    static bool is_expired(const CommandRequest& request, std::uint64_t now_ms) {
        return request.deadline_monotonic_ms <= now_ms;
    }

    bool deadline_too_far(const CommandRequest& request, std::uint64_t now_ms) const {
        return request.deadline_monotonic_ms - now_ms > validity_horizon_ms_;
    }

    static CommandAdmission with_result(CommandResult result) {
        return CommandAdmission{AdmissionStatus::kResult, std::move(result)};
    }

    static CommandResult rejected(const CommandRequest& request, std::uint32_t reason,
                                  const FakeSourceControl& source) {
        return source.result(request, kResultRejected, reason);
    }

    CommandAdmission consume_rejection(const CommandRequest& request, std::uint32_t reason,
                                       const FakeSourceControl& source) {
        const CommandResult result = rejected(request, reason, source);
        cache_result(request, result);
        advance_sequence();
        return with_result(result);
    }

    CommandAdmission replay_result(const CommandRequest& request,
                                   const FakeSourceControl& source) const {
        const auto pending = pending_.find(request.command_sequence);
        if (pending != pending_.end()) {
            if (same_request(pending->second, request))
                return CommandAdmission{AdmissionStatus::kDuplicatePending, std::nullopt};
            return with_result(rejected(request, kRejectBadSequence, source));
        }
        const auto cached = results_.find(request.command_sequence);
        if (cached != results_.end()) {
            if (same_request(cached->second.request, request)) return with_result(cached->second.result);
            return with_result(rejected(request, kRejectBadSequence, source));
        }
        return with_result(source.result(request, kResultOutcomeUnknown, kRejectBadSequence));
    }

    void cache_result(const CommandRequest& request, const CommandResult& result) {
        if (result_capacity_ == 0) return;
        results_.emplace(request.command_sequence, CachedResult{request, result});
        result_order_.push_back(request.command_sequence);
        while (result_order_.size() > result_capacity_) {
            results_.erase(result_order_.front());
            result_order_.pop_front();
        }
    }

    void advance_sequence() {
        if (next_sequence_ == std::numeric_limits<std::uint64_t>::max()) exhausted_ = true;
        else ++next_sequence_;
    }

    std::uint64_t session_id_{};
    std::size_t queue_capacity_{};
    std::size_t result_capacity_{};
    std::uint64_t validity_horizon_ms_{};
    std::uint64_t next_sequence_{1};
    bool exhausted_{false};
    std::deque<CommandRequest> queue_;
    std::unordered_map<std::uint64_t, CommandRequest> pending_;
    std::deque<std::uint64_t> result_order_;
    std::unordered_map<std::uint64_t, CachedResult> results_;
};

}  // namespace orbit
