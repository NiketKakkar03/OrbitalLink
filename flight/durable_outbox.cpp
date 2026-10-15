#include "durable_outbox.hpp"
#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>
#include <algorithm>
#include <memory>
#include <random>
#include <stdexcept>

namespace orbit {
namespace {
using Statement = std::unique_ptr<sqlite3_stmt, decltype(&sqlite3_finalize)>;
Statement prepare(sqlite3* db, const std::string& sql) {
    sqlite3_stmt* statement{};
    if (sqlite3_prepare_v2(db, sql.c_str(), -1, &statement, nullptr) != SQLITE_OK)
        throw std::runtime_error(std::string("outbox prepare: ") + sqlite3_errmsg(db));
    return Statement(statement, sqlite3_finalize);
}
void done(sqlite3* db, sqlite3_stmt* statement) {
    if (sqlite3_step(statement) != SQLITE_DONE)
        throw std::runtime_error(std::string("outbox write: ") + sqlite3_errmsg(db));
}
TelemetrySample sample(sqlite3_stmt* statement, int column) {
    const auto* bytes=static_cast<const std::uint8_t*>(sqlite3_column_blob(statement,column));
    const auto length=sqlite3_column_bytes(statement,column);
    if (!bytes || length <= 0) throw std::runtime_error("corrupt outbox sample");
    return decode_frame(std::vector<std::uint8_t>(bytes,bytes+length)).sample;
}
std::string identity(std::uint64_t value) { return std::to_string(value); }
}
void DurableOutbox::exec(const std::string& sql) const {
    if (sqlite3_exec(db_,sql.c_str(),nullptr,nullptr,nullptr)!=SQLITE_OK)
        throw std::runtime_error(std::string("outbox transaction: ")+sqlite3_errmsg(db_));
}
std::uint64_t DurableOutbox::scalar(const std::string& sql) const {
    auto statement=prepare(db_,sql);
    if(sqlite3_step(statement.get())!=SQLITE_ROW) throw std::runtime_error("outbox query failed");
    return static_cast<std::uint64_t>(sqlite3_column_int64(statement.get(),0));
}
DurableOutbox::DurableOutbox(const std::string& path,std::uint64_t bytes,std::size_t capacity):capacity_(capacity) {
    if(bytes<262144 || bytes>1073741824 || !capacity) throw std::invalid_argument("outbox bytes must be 256 KiB..1 GiB and capacity positive");
    lock_fd_=::open((path+".lock").c_str(),O_CREAT|O_RDWR,0600);
    if(lock_fd_<0 || flock(lock_fd_,LOCK_EX|LOCK_NB)!=0) {
        if(lock_fd_>=0) ::close(lock_fd_);
        lock_fd_=-1;
        throw std::runtime_error("outbox requires exclusive writable ownership");
    }
    try {
        if(sqlite3_open_v2(path.c_str(),&db_,SQLITE_OPEN_READWRITE|SQLITE_OPEN_CREATE|SQLITE_OPEN_FULLMUTEX,nullptr)!=SQLITE_OK)
            throw std::runtime_error("cannot open outbox");
        sqlite3_busy_timeout(db_,1000);
        exec("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL; PRAGMA fullfsync=ON; PRAGMA page_size=4096; PRAGMA temp_store=MEMORY;");
        // Conservatively reserve two database lengths for rollback journal and
        // its per-page/header overhead, plus 64 KiB. No WAL sidecar can grow.
        const auto pages=(bytes-65536)/3/4096;
        database_limit_=pages*4096;
        if(scalar("PRAGMA page_count")>pages) throw std::runtime_error("existing outbox exceeds selected disk budget");
        if(scalar("PRAGMA page_size")!=4096) throw std::runtime_error("unsupported outbox page size");
        exec("PRAGMA max_page_count="+std::to_string(pages));
        exec("CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY AUTOINCREMENT, frame BLOB NOT NULL);"
             "CREATE TABLE IF NOT EXISTS loss(id INTEGER PRIMARY KEY AUTOINCREMENT, boot TEXT NOT NULL, first TEXT NOT NULL, last TEXT NOT NULL, count INTEGER NOT NULL, complete INTEGER NOT NULL, frozen INTEGER NOT NULL DEFAULT 0);"
             "CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL);"
             "INSERT OR IGNORE INTO counters VALUES('generated',0),('acked',0),('dropped',0),('loss_acked',0);");
        if(database_limit_<32768)throw std::runtime_error("outbox schema leaves no telemetry budget");
        capacity_=std::min<std::size_t>(capacity_,static_cast<std::size_t>((database_limit_-32768)/256));
        high_watermark_=static_cast<std::size_t>(scalar("SELECT count(*) FROM samples"));
        if(high_watermark_>capacity_)throw std::runtime_error("existing outbox exceeds selected record capacity; reopen with the previous capacity and drain it first");
    } catch(...) {
        if(db_)sqlite3_close(db_);
        db_=nullptr;::close(lock_fd_);lock_fd_=-1;throw;
    }
}
DurableOutbox::~DurableOutbox(){if(db_)sqlite3_close(db_);if(lock_fd_>=0)::close(lock_fd_);}
std::vector<DurableLoss> DurableOutbox::losses_locked() const {
    auto statement=prepare(db_,"SELECT id,boot,first,last,count,complete FROM loss ORDER BY id");
    std::vector<DurableLoss> result;
    int status;
    while((status=sqlite3_step(statement.get()))==SQLITE_ROW) {
        const auto number=[&](int column){return std::stoull(reinterpret_cast<const char*>(sqlite3_column_text(statement.get(),column)));};
        result.push_back({static_cast<std::uint64_t>(sqlite3_column_int64(statement.get(),0)),number(1),number(2),number(3),static_cast<std::uint64_t>(sqlite3_column_int64(statement.get(),4)),sqlite3_column_int(statement.get(),5)!=0});
    }
    if(status!=SQLITE_DONE)throw std::runtime_error("outbox loss query failed");
    return result;
}
std::vector<DurableLoss> DurableOutbox::losses() const {std::lock_guard lock(mutex_);return losses_locked();}
void DurableOutbox::record_loss(const TelemetrySample& value) {
    auto reports=losses_locked();
    for(auto it=reports.rbegin();it!=reports.rend();++it) {
        if(scalar("SELECT frozen FROM loss WHERE id="+identity(it->id)))continue;
        if(it->detail_complete && it->boot_id==value.flight_boot_id && it->last!=UINT64_MAX && it->last+1==value.telemetry_sequence) {
            exec("UPDATE loss SET last='"+identity(value.telemetry_sequence)+"',count=count+1 WHERE id="+identity(it->id));return;
        }
    }
    if(reports.size()>=63) {
        for(const auto& report:reports)if(!report.detail_complete && !scalar("SELECT frozen FROM loss WHERE id="+identity(report.id))) {
            exec("UPDATE loss SET count=count+1 WHERE id="+identity(report.id));return;
        }
        if(reports.size()>=65)throw std::runtime_error("outbox loss metadata exhausted; stop acquisition");
        exec("INSERT INTO loss(id,boot,first,last,count,complete) VALUES("+identity((static_cast<std::uint64_t>(std::random_device{}())<<31 ^ std::random_device{}()) & 0x7fffffffffffffffULL)+",'0','0','0',1,0)");return;
    }
    exec("INSERT INTO loss(id,boot,first,last,count,complete) VALUES("+identity((static_cast<std::uint64_t>(std::random_device{}())<<31 ^ std::random_device{}()) & 0x7fffffffffffffffULL)+",'"+identity(value.flight_boot_id)+"','"+identity(value.telemetry_sequence)+"','"+identity(value.telemetry_sequence)+"',1,1)");
}
std::vector<TelemetrySample> DurableOutbox::push(const TelemetrySample& value) {
    std::lock_guard lock(mutex_);
    const auto size=scalar("SELECT count(*) FROM samples");
    std::size_t eviction=size>=capacity_?static_cast<std::size_t>(size-capacity_+1):0;
    // A persistent disk-full condition must not trigger a quadratic walk over
    // a potentially million-record backlog. Try geometric batches, capped at
    // 64 evictions and eight transactions, then fail without committing loss.
    constexpr std::size_t maximum_evictions=64;
    const auto retry_limit=std::min<std::size_t>(maximum_evictions,static_cast<std::size_t>(size)-(claimed_?1:0));
    for(;;) {
        std::vector<TelemetrySample> dropped;
        exec("BEGIN IMMEDIATE");
        try {
            for(std::size_t index=0;index<eviction;++index) {
                const auto restriction=claimed_?" WHERE id<>"+identity(*claimed_):"";
                auto front=prepare(db_,"SELECT id,frame FROM samples"+restriction+" ORDER BY id LIMIT 1");
                if(sqlite3_step(front.get())!=SQLITE_ROW)throw std::runtime_error("outbox full with protected in-flight sample");
                const auto id=sqlite3_column_int64(front.get(),0);
                auto lost=sample(front.get(),1);front.reset();
                exec("DELETE FROM samples WHERE id="+std::to_string(id));
                record_loss(lost);dropped.push_back(lost);
            }
            auto insert=prepare(db_,"INSERT INTO samples(frame) VALUES(?)");
            const auto frame=encode_telemetry_sample(value,0);
            if(sqlite3_bind_blob(insert.get(),1,frame.data(),static_cast<int>(frame.size()),SQLITE_TRANSIENT)!=SQLITE_OK)throw std::runtime_error("outbox bind failed");
            done(db_,insert.get());insert.reset();
            exec("UPDATE counters SET value=value+1 WHERE name='generated'; UPDATE counters SET value=value+"+std::to_string(dropped.size())+" WHERE name='dropped';");
            exec("COMMIT");
            ++accepted_;dropped_+=dropped.size();high_watermark_=std::max(high_watermark_,static_cast<std::size_t>(size-dropped.size()+1));
            return dropped;
        } catch(...) {
            const bool full=sqlite3_errcode(db_)==SQLITE_FULL;
            if(!sqlite3_get_autocommit(db_))sqlite3_exec(db_,"ROLLBACK",nullptr,nullptr,nullptr);
            if(full && eviction<retry_limit) {
                eviction=std::min(retry_limit,std::max<std::size_t>(1,eviction*2));
                continue;
            }
            throw;
        }
    }
}
std::optional<TelemetrySample> DurableOutbox::claim() {
    std::lock_guard lock(mutex_);
    auto row=prepare(db_,"SELECT id,frame FROM samples"+(claimed_?" WHERE id="+identity(*claimed_):" ORDER BY id LIMIT 1"));
    const auto status=sqlite3_step(row.get());
    if(status==SQLITE_DONE)return std::nullopt;
    if(status!=SQLITE_ROW)throw std::runtime_error("outbox claim failed");
    claimed_=static_cast<std::uint64_t>(sqlite3_column_int64(row.get(),0));
    return sample(row.get(),1);
}
void DurableOutbox::acknowledge(const TelemetrySample& value) {
    std::lock_guard lock(mutex_);
    if(!claimed_)throw std::logic_error("outbox ACK without claim");
    auto row=prepare(db_,"SELECT frame FROM samples WHERE id="+identity(*claimed_));
    if(sqlite3_step(row.get())!=SQLITE_ROW)throw std::runtime_error("outbox ACK row missing");
    const auto stored=sample(row.get(),0);row.reset();
    if(stored.spacecraft_id!=value.spacecraft_id || stored.flight_boot_id!=value.flight_boot_id || stored.telemetry_sequence!=value.telemetry_sequence)throw std::logic_error("outbox ACK identity mismatch");
    exec("BEGIN IMMEDIATE");
    try {exec("DELETE FROM samples WHERE id="+identity(*claimed_)+"; UPDATE counters SET value=value+1 WHERE name='acked'; COMMIT");}
    catch(...){if(!sqlite3_get_autocommit(db_))sqlite3_exec(db_,"ROLLBACK",nullptr,nullptr,nullptr);throw;}
    claimed_.reset();++popped_;
}
std::optional<DurableLoss> DurableOutbox::claim_loss() {
    std::lock_guard lock(mutex_);auto reports=losses_locked();
    for(const auto& report:reports)if(!claimed_loss_ || report.id==*claimed_loss_){exec("UPDATE loss SET frozen=1 WHERE id="+identity(report.id));claimed_loss_=report.id;return report;}
    return std::nullopt;
}
void DurableOutbox::acknowledge_loss(std::uint64_t id) {
    std::lock_guard lock(mutex_);
    if(!claimed_loss_||id!=*claimed_loss_)throw std::logic_error("outbox loss ACK mismatch");
    exec("BEGIN IMMEDIATE");
    try {exec("UPDATE counters SET value=value+(SELECT count FROM loss WHERE id="+identity(id)+") WHERE name='loss_acked'; DELETE FROM loss WHERE id="+identity(id)+"; COMMIT");}
    catch(...){if(!sqlite3_get_autocommit(db_))sqlite3_exec(db_,"ROLLBACK",nullptr,nullptr,nullptr);throw;}
    claimed_loss_.reset();
}
RetentionQueueSnapshot DurableOutbox::snapshot() const {
    std::lock_guard lock(mutex_);
    return {capacity_,static_cast<std::size_t>(scalar("SELECT count(*) FROM samples")),high_watermark_,accepted_,popped_,0,dropped_,0,false};
}
DurableAccounting DurableOutbox::accounting() const {
    std::lock_guard lock(mutex_);
    return {scalar("SELECT value FROM counters WHERE name='generated'"),
            scalar("SELECT value FROM counters WHERE name='acked'"),
            scalar("SELECT value FROM counters WHERE name='dropped'"),
            scalar("SELECT value FROM counters WHERE name='loss_acked'"),
            scalar("SELECT count(*) FROM samples")};
}
} // namespace orbit
