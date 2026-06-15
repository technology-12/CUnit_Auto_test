/* Custom runner for the auto-generated function tests */
#include <stdio.h>
#include <CUnit/Basic.h>
#include "common.h"

/* Expose static print_result and clamp from main.c */
void print_result(const char *label, int value) {
    (void)label; (void)value;
    /* Stub: just suppress unused warnings */
}

int clamp(int value, int low, int high) {
    if (value < low) return low;
    if (value > high) return high;
    return value;
}

/* Extern declarations matching what test files export */
extern void register__1_src_main_print_result_clamp_tests(void);
extern void register__2_src_math_ops_add_subtract_tests(void);
extern void register__3_src_math_ops_factorial_max_of_three_tests(void);
extern void register__4_src_string_ops_str_length_str_compare_tests(void);

int main(void)
{
    if (CU_initialize_registry() != CUE_SUCCESS)
        return CU_get_error();

    register__1_src_main_print_result_clamp_tests();
    register__2_src_math_ops_add_subtract_tests();
    register__3_src_math_ops_factorial_max_of_three_tests();
    register__4_src_string_ops_str_length_str_compare_tests();

    CU_basic_set_mode(CU_BRM_VERBOSE);
    CU_basic_run_tests();
    unsigned int failures = CU_get_number_of_failures();
    CU_cleanup_registry();
    printf("\n=== Failures: %u ===\n", failures);
    return failures == 0 ? 0 : 1;
}
