#include "durable_outbox.hpp"
#include <cassert>
#include <chrono>
#include <filesystem>
#include <iostream>
#include <sys/wait.h>
#include <unistd.h>

using orbit::DurableOutbox;
orbit::TelemetrySample make_sample(std::uint64_t sequence,std::uint64_t boot=UINT64_MAX) {
    return {42,boot,sequence,7,static_cast<std::uint32_t>(sequence),123456789,21500,3300};
}
int main() {
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
    const auto child=fork();assert(child>=0);
    if(child==0) {
        sqlite3* db{};if(sqlite3_open(path.c_str(),&db)!=SQLITE_OK)_exit(3);
        if(sqlite3_exec(db,"BEGIN IMMEDIATE; DELETE FROM samples; DELETE FROM loss",nullptr,nullptr,nullptr)!=SQLITE_OK)_exit(4);
        _exit(0);
    }
    int status{};waitpid(child,&status,0);assert(WIFEXITED(status)&&WEXITSTATUS(status)==0);
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
    const auto committed=fork();assert(committed>=0);
    if(committed==0){DurableOutbox outbox(path,262144,2);outbox.push(make_sample(4,123));_exit(0);}
    waitpid(committed,&status,0);assert(WIFEXITED(status)&&WEXITSTATUS(status)==0);
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
    std::filesystem::remove_all(directory);
    std::cout<<"durable outbox restart, transaction rollback, immutable losses, ownership and disk budget checks passed\n";
}
