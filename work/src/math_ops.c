#include "math_ops.h"
#include <stdlib.h>

int add(int a, int b) {
    return a + b;
}

int subtract(int a, int b) {
    return a - b;
}

int multiply(int a, int b) {
    return a * b;
}

/* Decision: if/else with struct return */
Result divide(int a, int b) {
    Result result = {0, 0, 0};
    if (b == 0) {
        result.error = ERROR_ZERO;
        return result;
    }
    result.quotient = a / b;
    result.remainder = a % b;
    result.error = SUCCESS;
    return result;
}

/* Decision: for loop + if */
int factorial(int n) {
    if (n < 0) {
        return ERROR_NEGATIVE;
    }
    int result = 1;
    for (int i = 1; i <= n; i++) {
        result *= i;
    }
    return result;
}

/* Decision: nested if/else with && and || */
int max_of_three(int a, int b, int c) {
    if (a >= b && a >= c) {
        return a;
    } else if (b >= a && b >= c) {
        return b;
    } else {
        return c;
    }
}

/* Decision: switch statement */
Operation classify_number(int n) {
    if (n < 0) {
        return OP_UNKNOWN;
    }
    switch (n) {
        case 0:
            return OP_ADD;
        case 1:
            return OP_SUBTRACT;
        case 2:
            return OP_MULTIPLY;
        default:
            return OP_DIVIDE;
    }
}

/* Decision: while loop + if */
int power(int base, int exp) {
    if (exp < 0) {
        return ERROR_NEGATIVE;
    }
    if (exp == 0) {
        return 1;
    }
    int result = 1;
    while (exp > 0) {
        result *= base;
        exp--;
    }
    return result;
}
