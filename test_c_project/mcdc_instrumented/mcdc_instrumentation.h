#ifndef MCDC_INSTRUMENTATION_H
#define MCDC_INSTRUMENTATION_H

#include <stdio.h>
#include <stdlib.h>

extern const char *MCDC_TEST_NAME;
extern FILE *MCDC_TRACE_FILE;

#define MCDC_INIT() \
    do { \
        MCDC_TRACE_FILE = fopen("mcdc_trace.txt", "a"); \
        if (MCDC_TRACE_FILE == NULL) { \
            fprintf(stderr, "MCDC: failed to open trace file\n"); \
            exit(1); \
        } \
    } while (0)

#define MCDC_COND(d_id, c_id, cond) \
    (__extension__({ \
        int _mcdc_val = (cond) ? 1 : 0; \
        int _mcdc_dec = 0; /* filled by outer logic if needed */ \
        if (MCDC_TRACE_FILE != NULL) { \
            fprintf(MCDC_TRACE_FILE, "MCDC_TRACE:%s:%d:%d:%d:%s\n", \
                    (d_id), (c_id), _mcdc_val, _mcdc_val, MCDC_TEST_NAME); \
        } \
        _mcdc_val; \
    }))

#define MCDC_CLOSE() \
    do { \
        if (MCDC_TRACE_FILE != NULL) { \
            fclose(MCDC_TRACE_FILE); \
            MCDC_TRACE_FILE = NULL; \
        } \
    } while (0)

#endif /* MCDC_INSTRUMENTATION_H */
