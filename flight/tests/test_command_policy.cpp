#include "../command_policy.hpp"
#include "../orbit_protocol.hpp"
#include <cassert>
#include <atomic>
#include <thread>
int main() {
    orbit::CommandDispatcher dispatch;
    dispatch.open(42);
    orbit::CommandRequest request{42,1,100,1000,3,250};
    using S=orbit::CommandStatus;
    assert(dispatch.submit(request,false,0)==S::unauthorized);
    assert(dispatch.state().period_ms==100);
    auto wrong=request; wrong.session=41;
    assert(dispatch.submit(wrong,true,0)==S::wrong_session);
    auto invalid=request; invalid.argument=0;
    assert(dispatch.submit(invalid,true,0)==S::invalid_argument);
    assert(dispatch.submit(request,true,1000)==S::expired);
    assert(dispatch.submit(request,true,0)==S::applied);
    assert(dispatch.submit(request,true,0)==S::replay);
    assert(dispatch.state().period_ms==100);
    dispatch.execute(1000);
    assert(dispatch.pop_result()->status==S::expired);
    assert(dispatch.state().period_ms==100);
    request.sequence=2;
    assert(dispatch.submit(request,true,0)==S::applied);
    dispatch.execute(1);
    assert(dispatch.pop_result()->status==S::applied);
    assert(dispatch.state().period_ms==250);
    dispatch.open(43);
    assert(dispatch.submit(request,true,0)==S::wrong_session);
    request.session=43;
    for (unsigned i=1; i<=16; ++i) { request.sequence=i; assert(dispatch.submit(request,true,0)==S::applied); }
    request.sequence=17;
    assert(dispatch.submit(request,true,0)==S::queue_full);
    dispatch.open(44);
    dispatch.execute(1);
    assert(!dispatch.pop_result());
    assert(dispatch.state().period_ms==250);
    dispatch.configure(250,true);
    request.session=44;request.sequence=1;request.opcode=2;request.argument=0;
    assert(dispatch.submit(request,true,0)==S::applied);dispatch.execute(1);dispatch.pop_result();
    assert(!dispatch.state().enabled);
    request.sequence=3;request.opcode=1;
    assert(dispatch.submit(request,true,0)==S::applied);dispatch.execute(1);dispatch.pop_result();
    assert(dispatch.state().enabled);
    request.sequence=4;request.opcode=4;
    assert(dispatch.submit(request,true,0)==S::applied);dispatch.execute(1);dispatch.pop_result();
    assert(dispatch.state().generation==8);
    request.sequence=5;request.opcode=5;
    assert(dispatch.submit(request,true,0)==S::applied);dispatch.execute(1);dispatch.pop_result();
    assert(!dispatch.state().fault);
    request.sequence=6;request.deadline_ns=5'000'000'001ULL;
    assert(dispatch.submit(request,true,0)==S::expired);
    request.sequence=7;request.deadline_ns=5'000'000'000ULL;
    assert(dispatch.submit(request,true,0)==S::applied);
    dispatch.close();dispatch.execute(1);assert(!dispatch.pop_result());
    std::vector<std::uint8_t> payload;
    orbit::append_u64(payload,42);orbit::append_u64(payload,1);orbit::append_u64(payload,100);
    orbit::append_u64(payload,2000000);orbit::append_u32(payload,3);orbit::append_u32(payload,250);
    auto frame=orbit::encode_control(4,payload);
    auto decoded=orbit::decode_frame(frame);
    assert(decoded.command.session==42 && decoded.command.sequence==1 && decoded.command.id==100 && decoded.command.argument==250);
    orbit::FrameParser parser;
    for(auto byte:frame) { auto frames=parser.feed({byte}); if(!frames.empty()) assert(frames[0].command.deadline_ns==2000000); }
    parser.finish();
    // Concurrent acquisition and network ownership: session invalidation races
    // execution, but only accepted requests may contribute an applied effect.
    orbit::CommandDispatcher concurrent;
    std::atomic<bool> finished{false};
    std::atomic<std::uint64_t> accepted{0}, completed{0};
    std::thread acquisition([&] {
        while(!finished.load()) {
            concurrent.execute(1);
            while(auto result=concurrent.pop_result()) {
                assert(result->status==S::applied);
                completed.fetch_add(1);
            }
            std::this_thread::yield();
        }
    });
    for(std::uint64_t session=1;session<=200;++session) {
        concurrent.open(session);
        for(std::uint64_t sequence=1;sequence<=100;++sequence) {
            orbit::CommandRequest command{session,sequence,sequence,1000,4,0};
            auto status=concurrent.submit(command,true,0);
            assert(status==S::applied || status==S::queue_full);
            if(status==S::applied) accepted.fetch_add(1);
            std::this_thread::yield();
        }
        concurrent.close();
    }
    finished.store(true); acquisition.join();
    assert(concurrent.state().effects<=accepted.load());
    assert(completed.load()<=concurrent.state().effects);
    assert(concurrent.state().generation==7+concurrent.state().effects);
    assert(!concurrent.pop_result());
}
