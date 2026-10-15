#include "durable_outbox.hpp"
#include <cassert>
#include <chrono>
#include <cerrno>
#include <spawn.h>
#include <stdexcept>
#include <filesystem>
#include <iostream>
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

using orbit::DurableOutbox;
orbit::TelemetrySample make_sample(std::uint64_t sequence,std::uint64_t boot=UINT64_MAX) {
    return {42,boot,sequence,7,static_cast<std::uint32_t>(sequence),123456789,21500,3300};
}
orbit::TelemetrySample make_sensor(std::uint64_t sequence,std::uint16_t category) {
    auto value=make_sample(sequence);value.category=category;
    if(category==6)value.values={1,1,1,1,0,0,0,0};
    if(category==5)value.values={1,1000,0,0,0,0,0,0};
    return value;
}
extern char** environ;

pid_t spawn_helper(const std::string& executable,const std::string& mode,const std::string& path,int write_fd=-1,int close_fd=-1) {
    std::vector<std::string> arguments{executable,"--child-mode",mode,path,std::to_string(write_fd)};
    std::vector<char*> argv;
    for(auto& argument:arguments)argv.push_back(argument.data());
    argv.push_back(nullptr);
    posix_spawn_file_actions_t actions;
    const auto initialized=posix_spawn_file_actions_init(&actions);
    if(initialized!=0)throw std::runtime_error("child file-actions initialization failed: "+std::to_string(initialized));
    if(close_fd>=0) {
        const auto added=posix_spawn_file_actions_addclose(&actions,close_fd);
        if(added!=0){posix_spawn_file_actions_destroy(&actions);throw std::runtime_error("child close-action failed: "+std::to_string(added));}
    }
    pid_t child{};
    const auto result=posix_spawn(&child,executable.c_str(),&actions,nullptr,argv.data(),environ);
    posix_spawn_file_actions_destroy(&actions);
    if(result!=0)throw std::runtime_error("cannot spawn SQLite helper "+mode+": "+std::to_string(result));
    return child;
}
void expect_child(pid_t child,bool killed,const char* mode) {
    int status{};
    pid_t waited;
    do{waited=waitpid(child,&status,0);}while(waited<0 && errno==EINTR);
    const bool expected=waited==child && (killed?(WIFSIGNALED(status)&&WTERMSIG(status)==SIGKILL):(WIFEXITED(status)&&WEXITSTATUS(status)==0));
    if(!expected) {
        std::cerr<<"SQLite helper "<<mode<<" pid="<<child<<" wait="<<waited<<" raw_status="<<status;
        if(WIFEXITED(status))std::cerr<<" exit="<<WEXITSTATUS(status);
        if(WIFSIGNALED(status))std::cerr<<" signal="<<WTERMSIG(status);
        std::cerr<<'\n';
        throw std::runtime_error("unexpected SQLite helper termination");
    }
}
[[noreturn]] void child_helper(const std::string& mode,const std::string& path,int write_fd) {
    try {
        if(mode=="committed") {
            DurableOutbox outbox(path,262144,2);outbox.push(make_sample(4,123));_exit(0);
        }
        if(mode=="rollback") {
            sqlite3* db{};
            if(sqlite3_open(path.c_str(),&db)!=SQLITE_OK)throw std::runtime_error("cannot open rollback DB");
            if(sqlite3_exec(db,"BEGIN IMMEDIATE; DELETE FROM samples; DELETE FROM loss",nullptr,nullptr,nullptr)!=SQLITE_OK)
                throw std::runtime_error(sqlite3_errmsg(db));
            _exit(0);
        }
        if(mode=="hot-journal") {
            DurableOutbox owner(path,1024*1024,128);
            sqlite3* db{};
            if(sqlite3_open(path.c_str(),&db)!=SQLITE_OK)throw std::runtime_error("cannot open hot-journal DB");
            const char* transaction=
                "PRAGMA synchronous=FULL; PRAGMA fullfsync=ON;"
                "PRAGMA cache_size=1; PRAGMA cache_spill=ON;"
                "BEGIN IMMEDIATE;"
                "UPDATE samples SET frame=zeroblob(length(frame));"
                "UPDATE counters SET value=value+10000;"
                "UPDATE loss SET count=count+10000;";
            if(sqlite3_exec(db,transaction,nullptr,nullptr,nullptr)!=SQLITE_OK)throw std::runtime_error(sqlite3_errmsg(db));
            if(sqlite3_db_cacheflush(db)!=SQLITE_OK)throw std::runtime_error(sqlite3_errmsg(db));
            const char ready='R';
            if(::write(write_fd,&ready,1)!=1)throw std::runtime_error("cannot signal crash barrier");
            for(;;)pause();
        }
        throw std::runtime_error("unknown helper mode");
    } catch(const std::exception& error) {
        std::cerr<<"SQLite helper "<<mode<<": "<<error.what()<<'\n';_exit(14);
    }
}
int main(int argc,char** argv) {
    if(argc==5 && std::string(argv[1])=="--child-mode")child_helper(argv[2],argv[3],std::stoi(argv[4]));
    const auto executable=std::filesystem::absolute(argv[0]).string();
    const auto directory=std::filesystem::temp_directory_path()/ ("orbit-outbox-tests-"+std::to_string(getpid()));
    std::filesystem::create_directory(directory);
    const auto path=(directory/"outbox.db").string();
    {
        DurableOutbox outbox(path,262144,2);
        outbox.push(make_sample(0));outbox.push(make_sample(1));
        const auto claimed=outbox.claim();assert(claimed && claimed->telemetry_sequence==0);
        const auto dropped=outbox.push(make_sample(2));
        assert(dropped.size()==1 && dropped[0].telemetry_sequence==1);
        const auto loss=outbox.claim_loss();assert(loss && loss->boot_id==UINT64_MAX && loss->first==1);
        bool locked=false;
        try {DurableOutbox competing(path,262144,2);}catch(const std::exception&){locked=true;}
        assert(locked);
    }
    {
        DurableOutbox outbox(path,262144,2);
        auto first=outbox.claim();assert(first && orbit::encode_telemetry_sample(*first,0)==orbit::encode_telemetry_sample(make_sample(0),0));
        bool mismatch=false;
        try {outbox.acknowledge(make_sample(9));}catch(const std::exception&){mismatch=true;}
        assert(mismatch && outbox.snapshot().size==2);
        outbox.push(make_sample(3));
        const auto losses=outbox.losses();assert(losses.size()==2 && losses[0].count+losses[1].count==2);
        // Persisted frozen report remains byte-for-byte unchanged across restart
        // even though the next adjacent telemetry sequence is now discarded.
        bool original=false;
        for(const auto& report:losses)if(report.first==1){assert(report.last==1 && report.count==1);original=true;}
        assert(original);
        outbox.acknowledge(*first);
    }
    // Crash before COMMIT restores both sample and its loss accounting.
    const auto child=spawn_helper(executable,"rollback",path);
    expect_child(child,false,"rollback");
    {
        DurableOutbox outbox(path,262144,2);assert(outbox.snapshot().size==1 && outbox.losses().size()==2);
        auto next=outbox.claim();assert(next && next->telemetry_sequence==3);outbox.acknowledge(*next);
        while(auto loss=outbox.claim_loss())outbox.acknowledge_loss(loss->id);
        assert(outbox.snapshot().size==0 && outbox.losses().empty());
        const auto counts=outbox.accounting();
        assert(counts.generated==4 && counts.acknowledged==2 && counts.dropped==2 && counts.loss_acknowledged==2);
        assert(counts.generated==counts.acknowledged+counts.dropped+counts.retained);
    }
    // A process exit immediately after a successful transaction retains data.
    const auto committed=spawn_helper(executable,"committed",path);
    expect_child(committed,false,"committed");
    {
        DurableOutbox outbox(path,262144,2);auto next=outbox.claim();assert(next && next->flight_boot_id==123);outbox.acknowledge(*next);
    }
    // Actual physical DB budget bounds storage, even with an enormous count cap.
    const auto bounded=(directory/"bounded.db").string();
    {
        DurableOutbox outbox(bounded,262144,1000000);
        std::size_t losses=0;
        for(std::uint64_t index=0;index<2000;++index) losses+=outbox.push(make_sample(index)).size();
        assert(losses>0 && losses+outbox.snapshot().size==2000);
        assert(std::filesystem::file_size(bounded)<=outbox.database_limit_bytes());
        assert(!std::filesystem::exists(bounded+"-wal"));
        const auto ranges=outbox.losses();assert(ranges.size()==1 && ranges[0].count==losses);
    }
    // SQLite FULL during insertion atomically rolls back eviction and loss rows.
    {
        DurableOutbox outbox(path,262144,1);outbox.push(make_sample(5));
        sqlite3* db{};assert(sqlite3_open(path.c_str(),&db)==SQLITE_OK);
        assert(sqlite3_exec(db,"CREATE TABLE scratch(data BLOB); CREATE TRIGGER full_disk BEFORE INSERT ON samples BEGIN INSERT INTO scratch VALUES(zeroblob(1048576)); END",nullptr,nullptr,nullptr)==SQLITE_OK);
        sqlite3_close(db);
        bool failed=false;try{outbox.push(make_sample(6));}catch(const std::exception&){failed=true;}
        assert(failed && outbox.snapshot().size==1 && outbox.losses().empty());
        assert(outbox.claim()->telemetry_sequence==5);
    }
    // Shrinking a configured count limit cannot silently bulk-evict a backlog.
    bool shrink_refused=false;
    try {DurableOutbox smaller(bounded,262144,1);}catch(const std::exception&){shrink_refused=true;}
    assert(shrink_refused);
    {
        DurableOutbox restored(bounded,262144,1000000);
        assert(restored.snapshot().size>1);
        const auto counts=restored.accounting();assert(counts.generated==2000);
    }
    // Persistent SQLITE_FULL on a large backlog has bounded recovery work.
    // A single ordinary push may evict at most64 records, never scan and roll
    // back increasing batches across the complete queue before failing.
    const auto large=(directory/"large.db").string();
    {
        DurableOutbox outbox(large,4*1024*1024,2048);
        for(std::uint64_t index=0;index<1024;++index)outbox.push(make_sample(index));
        const auto before=outbox.accounting();
        sqlite3* db{};assert(sqlite3_open(large.c_str(),&db)==SQLITE_OK);
        assert(sqlite3_exec(db,"CREATE TABLE scratch(data BLOB); CREATE TRIGGER full_disk BEFORE INSERT ON samples BEGIN INSERT INTO scratch VALUES(zeroblob(8388608)); END",nullptr,nullptr,nullptr)==SQLITE_OK);
        sqlite3_close(db);
        const auto start=std::chrono::steady_clock::now();
        bool failed=false;try{outbox.push(make_sample(1024));}catch(const std::exception&){failed=true;}
        assert(failed && std::chrono::steady_clock::now()-start<std::chrono::seconds(3));
        const auto after=outbox.accounting();
        assert(after.generated==before.generated && after.acknowledged==before.acknowledged && after.dropped==before.dropped && after.retained==before.retained);
        assert(outbox.losses().empty() && outbox.claim()->telemetry_sequence==0);
    }
    // Kill a writer after dirty pages reach the DB, while its rollback journal
    // is live. A pipe barrier makes this a transaction-boundary test, with no
    // scheduling delay or assumption about how quickly SQLite writes pages.
    const auto hot=(directory/"hot-journal.db").string();
    constexpr std::uint64_t hot_budget=1024*1024;
    orbit::DurableAccounting original_accounting;
    std::vector<orbit::DurableLoss> original_losses;
    std::vector<std::vector<std::uint8_t>> original_frames;
    {
        DurableOutbox outbox(hot,hot_budget,128);
        for(std::uint64_t index=0;index<160;++index)outbox.push(make_sample(index,987654321));
        original_accounting=outbox.accounting();original_losses=outbox.losses();
        for(std::uint64_t index=32;index<160;++index)
            original_frames.push_back(orbit::encode_telemetry_sample(make_sample(index,987654321),0));
        assert(original_accounting.retained==128 && original_accounting.dropped==32 && original_losses.size()==1);
    }
    int barrier[2];assert(pipe(barrier)==0);
    const auto writer=spawn_helper(executable,"hot-journal",hot,barrier[1],barrier[0]);
    ::close(barrier[1]);
    char ready{};
    const auto received=::read(barrier[0],&ready,1);::close(barrier[0]);
    if(received!=1 || ready!='R'){expect_child(writer,false,"hot-journal before barrier");throw std::runtime_error("SQLite helper exited without crash barrier");}
    const auto journal_bytes=std::filesystem::file_size(hot+"-journal");
    assert(journal_bytes>512);
    assert(std::filesystem::file_size(hot)+journal_bytes<=hot_budget);
    assert(kill(writer,SIGKILL)==0);
    expect_child(writer,true,"hot-journal SIGKILL");
    {
        DurableOutbox recovered(hot,hot_budget,128);
        const auto counts=recovered.accounting();
        assert(counts.generated==original_accounting.generated && counts.acknowledged==original_accounting.acknowledged && counts.dropped==original_accounting.dropped && counts.loss_acknowledged==original_accounting.loss_acknowledged && counts.retained==original_accounting.retained);
        const auto reports=recovered.losses();
        assert(reports.size()==original_losses.size());
        const auto& loss=reports.front();const auto& original=original_losses.front();
        assert(loss.id==original.id && loss.boot_id==original.boot_id && loss.first==original.first && loss.last==original.last && loss.count==original.count && loss.detail_complete==original.detail_complete);
        for(const auto& frame:original_frames) {
            const auto next=recovered.claim();assert(next && orbit::encode_telemetry_sample(*next,0)==frame);
            recovered.acknowledge(*next);
        }
        assert(recovered.snapshot().size==0);
    }
    // Window claims are distinct, protected and ACKed by exact identity even
    // out of order. Reconnect releases only claims, never durable rows.
    {
        DurableOutbox outbox((directory/"window.db").string(),1024*1024,64,true);
        for(std::uint64_t index=0;index<8;++index) {
            outbox.push(make_sensor(index*3,6));outbox.push(make_sensor(index*3+1,4));outbox.push(make_sensor(index*3+2,5));
        }
        std::vector<orbit::TelemetrySample> claimed;
        unsigned urgent=0,operational=0,bulk=0;
        for(unsigned index=0;index<7;++index) {
            const auto next=outbox.claim_next(7);assert(next);claimed.push_back(*next);
            if(next->category==6)++urgent;else if(next->category==5)++bulk;else ++operational;
        }
        assert(urgent==4 && operational==2 && bulk==1 && !outbox.claim_next(7));
        assert(outbox.snapshot().size==24);
        outbox.acknowledge(claimed.back());assert(outbox.snapshot().size==23);
        bool duplicate=false;try{outbox.acknowledge(claimed.back());}catch(const std::logic_error&){duplicate=true;}
        assert(duplicate);
        outbox.release_claims();
        std::vector<std::uint64_t> replayed;
        while(auto next=outbox.claim_next(64))replayed.push_back(next->telemetry_sequence);
        assert(replayed.size()==23);
        for(const auto& previous:claimed)if(previous.telemetry_sequence!=claimed.back().telemetry_sequence)
            assert(std::find(replayed.begin(),replayed.end(),previous.telemetry_sequence)!=replayed.end());
        bool bad_window=false;try{outbox.claim_next(65);}catch(const std::invalid_argument&){bad_window=true;}
        assert(bad_window);
    }
    // Bulk cannot consume the urgent reserve. Even a fully claimed window
    // never loses its immutable frames when another urgent event arrives.
    {
        DurableOutbox outbox((directory/"reserve.db").string(),1024*1024,8,true);
        for(std::uint64_t index=0;index<8;++index)outbox.push(make_sensor(index,5));
        assert(outbox.snapshot().size==7 && outbox.accounting().dropped==1);
        for(unsigned index=0;index<7;++index)assert(outbox.claim_next(8));
        assert(outbox.push(make_sensor(8,6)).empty());
        auto urgent=outbox.claim_next(8);assert(urgent && urgent->category==6);
        const auto dropped=outbox.push(make_sensor(9,6));
        assert(dropped.size()==1 && dropped.front().telemetry_sequence==9);
        assert(outbox.snapshot().size==8 && outbox.accounting().generated==10 && outbox.accounting().dropped==2);
        outbox.acknowledge(*urgent);assert(outbox.snapshot().size==7);
    }
    // Enabling priority on an existing FIFO backlog rederives scheduling keys
    // from unchanged payloads, including typedrows stored by a FIFO owner.
    const auto mode_path=(directory/"mode-change.db").string();
    const std::vector<orbit::TelemetrySample> mode_samples{make_sensor(100,5),make_sensor(101,4),make_sensor(102,6),make_sample(103)};
    {
        DurableOutbox fifo(mode_path,1024*1024,16,false);
        for(const auto& value:mode_samples)fifo.push(value);
    }
    {
        DurableOutbox priority(mode_path,1024*1024,16,true);
        const auto urgent=priority.claim_next(4);assert(urgent && urgent->category==6 && urgent->telemetry_sequence==102);
        std::vector<orbit::TelemetrySample> replay{*urgent};
        while(auto next=priority.claim_next(4))replay.push_back(*next);
        assert(replay.size()==mode_samples.size());
        for(const auto& original:mode_samples) {
            const auto found=std::find_if(replay.begin(),replay.end(),[&](const auto& value){return value.telemetry_sequence==original.telemetry_sequence;});
            assert(found!=replay.end() && orbit::encode_telemetry_sample(*found,0)==orbit::encode_telemetry_sample(original,0));
        }
        const auto counts=priority.accounting();assert(counts.generated==4 && counts.retained==4 && counts.dropped==0 && counts.acknowledged==0 && priority.losses().empty());
    }
    {
        DurableOutbox fifo_again(mode_path,1024*1024,16,false);
        const auto first=fifo_again.claim();assert(first && first->category==5 && first->telemetry_sequence==100);
    }
    {
        DurableOutbox priority_again(mode_path,1024*1024,16,true);
        assert(priority_again.claim()->telemetry_sequence==102);
    }
    // A failure halfway through reclassification rolls back all scheduling
    // changes. It cannot alter payloads or cumulative delivery accounting.
    const auto failed_mode=(directory/"mode-failure.db").string();
    {
        DurableOutbox fifo(failed_mode,1024*1024,16,false);
        fifo.push(make_sensor(200,5));fifo.push(make_sensor(201,6));
    }
    sqlite3* failed_db{};assert(sqlite3_open(failed_mode.c_str(),&failed_db)==SQLITE_OK);
    assert(sqlite3_exec(failed_db,"CREATE TRIGGER reject_reclassification BEFORE UPDATE OF priority ON samples WHEN OLD.id=2 BEGIN SELECT RAISE(ABORT,'injected priority migration failure'); END",nullptr,nullptr,nullptr)==SQLITE_OK);
    sqlite3_close(failed_db);
    bool rebuild_failed=false;
    try{DurableOutbox priority(failed_mode,1024*1024,16,true);}catch(const std::exception&){rebuild_failed=true;}
    assert(rebuild_failed);
    assert(sqlite3_open(failed_mode.c_str(),&failed_db)==SQLITE_OK);
    sqlite3_stmt* priorities{};
    assert(sqlite3_prepare_v2(failed_db,"SELECT priority FROM samples ORDER BY id",-1,&priorities,nullptr)==SQLITE_OK);
    assert(sqlite3_step(priorities)==SQLITE_ROW && sqlite3_column_int(priorities,0)==1);
    assert(sqlite3_step(priorities)==SQLITE_ROW && sqlite3_column_int(priorities,0)==1);
    sqlite3_finalize(priorities);sqlite3_close(failed_db);
    {
        DurableOutbox preserved(failed_mode,1024*1024,16,false);
        const auto counts=preserved.accounting();assert(counts.generated==2 && counts.retained==2 && counts.dropped==0 && counts.acknowledged==0);
        assert(preserved.claim()->telemetry_sequence==200);
    }
    std::filesystem::remove_all(directory);
    std::cout<<"durable outbox restart, transaction rollback, immutable losses, ownership and disk budget checks passed\n";
}
