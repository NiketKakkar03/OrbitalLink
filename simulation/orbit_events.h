/* Bounded pre-publication event reports; popped reports must remain immutable. */
#ifndef ORBIT_EVENTS_H
#define ORBIT_EVENTS_H
#include "orbit_scenario.h"
#include <limits.h>
#define ORBIT_EVENT_PENDING_CAPACITY 8
struct orbit_event_accumulator {
    struct orbit_scenario_record pending[ORBIT_EVENT_PENDING_CAPACITY];
    unsigned count;
    uint64_t observations, coalesced, overflow;
};
static inline int orbit_event_push(struct orbit_event_accumulator *queue,
                                  const struct orbit_scenario_record *event)
{
    unsigned i;
    ++queue->observations;
    for(i=0;i<queue->count;++i) {
        struct orbit_scenario_record *p=&queue->pending[i];
        if(p->quality==event->quality && p->values[0]==event->values[0] &&
           p->values[1]==event->values[1] && p->values[2]==event->values[2]) {
            const int32_t n=event->values[3];
            if(n<1 || p->values[3]>INT_MAX-n) {++queue->overflow;return 0;}
            p->values[3]+=n;++queue->coalesced;return 1;
        }
    }
    if(queue->count==ORBIT_EVENT_PENDING_CAPACITY) {++queue->overflow;return 0;}
    queue->pending[queue->count++]=*event;return 1;
}
static inline int orbit_event_pop(struct orbit_event_accumulator *queue,
                                 struct orbit_scenario_record *event)
{
    unsigned i;
    if(!queue->count)return 0;
    *event=queue->pending[0];--queue->count;
    for(i=0;i<queue->count;++i)queue->pending[i]=queue->pending[i+1];
    return 1;
}
#endif
