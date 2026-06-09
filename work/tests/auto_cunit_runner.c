#include <stdio.h>
        #include <CUnit/Basic.h>

        extern void register_1_src_main_print_result_clamp_tests(void);
extern void register_2_src_math_ops_add_subtract_tests(void);
extern void register_3_src_math_ops_factorial_max_of_three_tests(void);
extern void register_4_src_string_ops_str_length_str_compare_tests(void);

        int main(void)
        {
            if (CU_initialize_registry() != CUE_SUCCESS) {
                return CU_get_error();
            }

            register_1_src_main_print_result_clamp_tests();
    register_2_src_math_ops_add_subtract_tests();
    register_3_src_math_ops_factorial_max_of_three_tests();
    register_4_src_string_ops_str_length_str_compare_tests();

            CU_basic_set_mode(CU_BRM_VERBOSE);
            CU_basic_run_tests();
            unsigned int failures = CU_get_number_of_failures();
            CU_cleanup_registry();
            return failures == 0 ? 0 : 1;
        }
