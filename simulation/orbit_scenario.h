/* Deterministic synthetic interactions; not a physical spacecraft model. */
#ifndef ORBIT_SCENARIO_H
#define ORBIT_SCENARIO_H
#include <stdint.h>
struct orbit_scenario_record { uint32_t category, quality; int32_t values[8]; };
/* One routine report per tick; at most one transition event per tick. */
static inline int orbit_scenario_tick(uint64_t tick, uint32_t seed,
                                     struct orbit_scenario_record *routine,
                                     struct orbit_scenario_record *event)
{
    static const uint32_t categories[10] = {3,1,3,2,3,1,3,4,3,5};
    const uint32_t phase = (uint32_t)(tick % 600);
    const int eclipse = phase >= 100 && phase < 400;
    const int disturbed = phase >= 200 && phase < 260;
    const int hot = phase >= 350 && phase < 400;
    int charge = eclipse ? 900 - (int)(phase - 100) * 3 : 900;
    int i, code = 0, severity = 0, related = 0;
    if (charge < 100) charge = 100;
    routine->category = categories[tick % 10]; routine->quality = 0;
    event->category = 6; event->quality = 0;
    for (i=0;i<8;++i) { routine->values[i]=0; event->values[i]=0; }
    switch (routine->category) {
    case 1:
        routine->values[0]=3200+charge; routine->values[1]=eclipse?450:-100;
        routine->values[2]=charge; routine->values[3]=eclipse?0:2400;
        routine->values[4]=eclipse?1800:1200; routine->values[5]=eclipse;
        break;
    case 2:
        routine->values[0]=eclipse?15000:25000;
        routine->values[1]=hot?85000:35000;
        routine->values[2]=eclipse;
        break;
    case 3:
        routine->values[0]=disturbed?2000:0;
        routine->values[1]=disturbed?-1000:0;
        routine->values[2]=disturbed?500:0;
        routine->values[3]=disturbed?15000:10;
        routine->values[4]=disturbed;
        break;
    case 4:
        routine->values[0]=(int32_t)((tick/10)&UINT64_C(0x7fffffff));
        routine->values[3]=(int32_t)(tick&UINT64_C(0x7fffffff));
        break;
    case 5:
        routine->values[0]=disturbed?0:(int32_t)(20+((tick*17+seed)%13));
        routine->values[1]=1000; routine->values[2]=disturbed;
        break;
    }
    if (phase==100) {code=1;related=1;}
    else if (phase==400) {code=2;related=1;}
    else if (phase==300) {code=3;severity=2;related=1;}
    else if (phase==350) {code=4;severity=2;related=2;}
    else if (phase==200) {code=5;severity=1;related=3;}
    else if (phase==260) {code=6;related=3;}
    event->values[0]=code;event->values[1]=severity;
    event->values[2]=related;event->values[3]=1;
    return code != 0;
}
#endif
