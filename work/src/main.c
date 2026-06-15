#include <stdio.h>
#include "math_ops.h"
#include "string_ops.h"
#include "common.h"

static void print_result(const char *label, int value) {
    printf("%s: %d\n", label, value);
}

int clamp(int value, int low, int high) {
    if (value < low) {
        return low;
    }
    if (value > high) {
        return high;
    }
    return value;
}

int main(void) {
    /* Math ops */
    print_result("add(3, 5)", add(3, 5));
    print_result("subtract(10, 4)", subtract(10, 4));
    print_result("multiply(6, 7)", multiply(6, 7));

    Result dr = divide(10, 3);
    printf("divide(10,3): q=%d r=%d err=%d\n",
           dr.quotient, dr.remainder, dr.error);

    print_result("factorial(5)", factorial(5));
    print_result("max_of_three(7,2,9)", max_of_three(7, 2, 9));
    print_result("classify_number(2)", classify_number(2));
    print_result("power(2, 8)", power(2, 8));

    /* String ops */
    print_result("str_length(\"hello\")", str_length("hello"));
    print_result("str_compare(\"abc\",\"abd\")", str_compare("abc", "abd"));

    const char *found = find_char("hello world", 'w');
    printf("find_char('hello world','w'): %s\n",
           found ? found : "(null)");

    print_result("clamp(5, 0, 10)", clamp(5, 0, 10));
    print_result("clamp(50, 0, 10)", clamp(50, 0, 10));

    return SUCCESS;
}
