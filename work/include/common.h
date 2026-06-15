#ifndef COMMON_H
#define COMMON_H

/* Shared macros */
#define MAX_BUFFER_SIZE  256
#define SUCCESS          0
#define ERROR_NEGATIVE  -1
#define ERROR_ZERO      -2
#define IS_VALID(x)      ((x) > 0)

/* Shared structs */
typedef struct {
    int quotient;
    int remainder;
    int error;          /* 0 = ok, non-zero = error */
} Result;

typedef enum {
    OP_ADD,
    OP_SUBTRACT,
    OP_MULTIPLY,
    OP_DIVIDE,
    OP_UNKNOWN = 99
} Operation;

/* Utility */
int clamp(int value, int low, int high);

#endif
