/* Read a bounded committed SQLite snapshot for the VM power-cut gate. */
#include <stdio.h>
#include <stdlib.h>
#include <sqlite3.h>
int main(int argc, char **argv)
{
    sqlite3 *db = NULL;
    sqlite3_stmt *counts = NULL, *frames = NULL;
    char *end;
    long minimum;
    int result = 2, status;
    if (argc != 3) return 2;
    minimum = strtol(argv[2], &end, 10);
    if (*end || minimum < 0 || minimum > 1000) return 2;
    /* READWRITE permits normal hot rollback-journal recovery after VM crash;
     * no CREATE, updates, journal-mode changes or ownership bypass writes. */
    if (sqlite3_open_v2(argv[1], &db, SQLITE_OPEN_READWRITE, NULL) != SQLITE_OK) goto done;
    sqlite3_busy_timeout(db, 1000);
    if (sqlite3_exec(db, "BEGIN", NULL, NULL, NULL) != SQLITE_OK) goto done;
    if (sqlite3_prepare_v2(db, "SELECT (SELECT count(*) FROM samples),"
        "(SELECT value FROM counters WHERE name='generated'),"
        "(SELECT value FROM counters WHERE name='acked'),"
        "(SELECT value FROM counters WHERE name='dropped')", -1, &counts, NULL) != SQLITE_OK) goto done;
    if (sqlite3_step(counts) != SQLITE_ROW) goto done;
    long long retained = sqlite3_column_int64(counts, 0);
    if (retained < minimum) { result = 4; goto done; }
    if (sqlite3_prepare_v2(db, "SELECT hex(frame) FROM samples ORDER BY id LIMIT 256", -1, &frames, NULL) != SQLITE_OK) goto done;
    printf("ORBIT_OUTBOX={\"retained\":%lld,\"generated\":%lld,\"acked\":%lld,\"dropped\":%lld,\"frames\":[",
        retained, (long long)sqlite3_column_int64(counts, 1),
        (long long)sqlite3_column_int64(counts, 2), (long long)sqlite3_column_int64(counts, 3));
    unsigned rows = 0;
    while ((status = sqlite3_step(frames)) == SQLITE_ROW) {
        const unsigned char *hex = sqlite3_column_text(frames, 0);
        int bytes = sqlite3_column_bytes(frames, 0);
        if (!hex || bytes < 1 || bytes > 512) goto done;
        printf("%s\"%s\"", rows++ ? "," : "", hex);
    }
    if (status != SQLITE_DONE) goto done;
    printf("]}\n");
    result = 0;
done:
    sqlite3_finalize(frames);
    sqlite3_finalize(counts);
    if (db) {
        sqlite3_exec(db, "ROLLBACK", NULL, NULL, NULL);
        sqlite3_close(db);
    }
    return result;
}
