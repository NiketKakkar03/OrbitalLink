#pragma once
#include <cstdint>
#include <deque>
#include <mutex>
#include <optional>

namespace orbit {
struct CommandRequest {
    std::uint64_t session{}, sequence{}, id{}, deadline_ns{};
    std::uint32_t opcode{}, argument{};
};
enum class CommandStatus : std::uint32_t {
    applied=0, unauthorized=1, wrong_session=2, replay=3, expired=4,
    invalid_argument=5, queue_full=6, outcome_unknown=7
};
struct FakeDeviceState { bool enabled{true}; std::uint32_t period_ms{100}, generation{7}; bool fault{}; std::uint64_t effects{}; };

struct CommandResult { CommandRequest request; CommandStatus status; std::optional<FakeDeviceState> snapshot{}; std::uint64_t monotonic_ns{}; };

// Acquisition alone executes device effects. Network owns session policy.
// Both work and result queues are bounded; disconnect invalidates queued work.
class CommandDispatcher {
    mutable std::mutex mutex_;
    std::uint64_t session_{}, last_sequence_{};
    std::deque<CommandRequest> work_;
    std::deque<CommandResult> results_;
    FakeDeviceState state_;
  public:
    void open(std::uint64_t session) {
        std::lock_guard lock(mutex_);
        session_=session; last_sequence_=0; work_.clear(); results_.clear();
    }
    CommandStatus submit(const CommandRequest& request, bool authorized, std::uint64_t now) {
        std::lock_guard lock(mutex_);
        if (!authorized) return CommandStatus::unauthorized;
        if (request.session != session_ || !session_) return CommandStatus::wrong_session;
        if (!request.sequence || request.sequence <= last_sequence_) return CommandStatus::replay;
        if (request.deadline_ns <= now || request.deadline_ns-now > 5'000'000'000ULL) return CommandStatus::expired;
        if (request.opcode < 1 || request.opcode > 6 ||
            (request.opcode == 3 ? (request.argument < 1 || request.argument > 60'000) : request.argument != 0))
            return CommandStatus::invalid_argument;
        if (work_.size()+results_.size() >= 16) return CommandStatus::queue_full;
        last_sequence_=request.sequence;
        work_.push_back(request);
        return CommandStatus::applied; // accepted; wire completion waits for execute()
    }
    void configure(std::uint32_t period, bool fault=false) { std::lock_guard lock(mutex_); state_.period_ms=period; state_.fault=fault; }
    void close() { open(0); }
    void execute(std::uint64_t now) {
        std::lock_guard lock(mutex_);
        while (!work_.empty()) {
            auto request=work_.front(); work_.pop_front();
            auto status=CommandStatus::applied;
            if (request.deadline_ns <= now) status=CommandStatus::expired;
            else switch (request.opcode) {
                case 1: state_.enabled=true; break;
                case 2: state_.enabled=false; break;
                case 3: state_.period_ms=request.argument; break;
                case 4: ++state_.generation; break;
                case 5: state_.fault=false; break;
            }
            if(status==CommandStatus::applied && request.opcode != 6) ++state_.effects;
            results_.push_back({request,status, request.opcode==6 && status==CommandStatus::applied ? std::optional<FakeDeviceState>(state_) : std::nullopt, now});
        }
    }
    std::optional<CommandResult> pop_result() {
        std::lock_guard lock(mutex_);
        if (results_.empty()) return {};
        auto result=results_.front(); results_.pop_front(); return result;
    }
    FakeDeviceState state() const { std::lock_guard lock(mutex_); return state_; }
};
} // namespace orbit
