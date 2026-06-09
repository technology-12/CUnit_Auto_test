#include "string_ops.h"
#include "common.h"

/* Decision: while loop */
int str_length(const char *s) {
    if (s == ((void*)0)) {
        return ERROR_NEGATIVE;
    }
    int len = 0;
    while (s[len] != '\0') {
        len++;
    }
    return len;
}

/* Decision: while + nested if */
int str_compare(const char *a, const char *b) {
    if (a == ((void*)0) || b == ((void*)0)) {
        return ERROR_NEGATIVE;
    }
    while (*a != '\0' && *b != '\0') {
        if (*a != *b) {
            return *a - *b;
        }
        a++;
        b++;
    }
    return *a - *b;
}

/* Decision: while + if with multiple conditions */
const char *find_char(const char *s, char c) {
    if (s == ((void*)0)) {
        return ((void*)0);
    }
    while (*s != '\0') {
        if (*s == c) {
            return s;
        }
        s++;
    }
    return ((void*)0);
}
