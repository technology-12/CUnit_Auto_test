#include <CUnit/Basic.h>
#include "math_ops.h"

/* factorial tests */
void test_factorial_negative(void) {
    CU_ASSERT_EQUAL(factorial(-5), ERROR_NEGATIVE);
}
void test_factorial_zero(void) {
    CU_ASSERT_EQUAL(factorial(0), 1);
}
void test_factorial_one(void) {
    CU_ASSERT_EQUAL(factorial(1), 1);
}
void test_factorial_positive(void) {
    CU_ASSERT_EQUAL(factorial(5), 120);
}

/* max_of_three MC/DC tests */
/* MC/DC for a>=b with a>=c held true */
void test_max_of_three_C1_a_ge_b(void) {
    CU_ASSERT_EQUAL(max_of_three(3,2,1), 3);
    CU_ASSERT_EQUAL(max_of_three(2,3,1), 3);
}
/* MC/DC for a>=c with a>=b held true */
void test_max_of_three_C2_a_ge_c(void) {
    CU_ASSERT_EQUAL(max_of_three(3,2,1), 3);
    CU_ASSERT_EQUAL(max_of_three(3,2,4), 4);
}
/* MC/DC for b>=c in second decision (b>=a held true) */
void test_max_of_three_decision2_C2_b_ge_c(void) {
    CU_ASSERT_EQUAL(max_of_three(1,3,2), 3);
    CU_ASSERT_EQUAL(max_of_three(1,3,4), 4);
}
void test_max_of_three_else_branch(void) {
    CU_ASSERT_EQUAL(max_of_three(1,2,3), 3);
}

/* classify_number tests */
void test_classify_negative(void) {
    CU_ASSERT_EQUAL(classify_number(-1), OP_UNKNOWN);
}
void test_classify_zero(void) {
    CU_ASSERT_EQUAL(classify_number(0), OP_ADD);
}
void test_classify_one(void) {
    CU_ASSERT_EQUAL(classify_number(1), OP_SUBTRACT);
}
void test_classify_two(void) {
    CU_ASSERT_EQUAL(classify_number(2), OP_MULTIPLY);
}
void test_classify_default(void) {
    CU_ASSERT_EQUAL(classify_number(3), OP_DIVIDE);
}

/* power tests */
void test_power_negative_exp(void) {
    CU_ASSERT_EQUAL(power(2, -1), ERROR_NEGATIVE);
}
void test_power_zero_exp(void) {
    CU_ASSERT_EQUAL(power(2, 0), 1);
    CU_ASSERT_EQUAL(power(0, 0), 1);
}
void test_power_positive_exp(void) {
    CU_ASSERT_EQUAL(power(2, 3), 8);
    CU_ASSERT_EQUAL(power(0, 3), 0);
}
void test_power_loop_condition(void) {
    CU_ASSERT_EQUAL(power(5, 1), 5);
}

/* Registration function */
void register__3_src_math_ops_factorial_max_of_three_tests(void) {
    CU_pSuite suite = CU_add_suite("suite_math_ops", NULL, NULL);
    if (suite != NULL) {
        CU_add_test(suite, "factorial negative", test_factorial_negative);
        CU_add_test(suite, "factorial zero", test_factorial_zero);
        CU_add_test(suite, "factorial one", test_factorial_one);
        CU_add_test(suite, "factorial positive", test_factorial_positive);

        CU_add_test(suite, "max_of_three MC/DC a>=b", test_max_of_three_C1_a_ge_b);
        CU_add_test(suite, "max_of_three MC/DC a>=c", test_max_of_three_C2_a_ge_c);
        CU_add_test(suite, "max_of_three MC/DC b>=c in decision2", test_max_of_three_decision2_C2_b_ge_c);
        CU_add_test(suite, "max_of_three else branch", test_max_of_three_else_branch);

        CU_add_test(suite, "classify negative", test_classify_negative);
        CU_add_test(suite, "classify zero", test_classify_zero);
        CU_add_test(suite, "classify one", test_classify_one);
        CU_add_test(suite, "classify two", test_classify_two);
        CU_add_test(suite, "classify default", test_classify_default);

        CU_add_test(suite, "power negative exp", test_power_negative_exp);
        CU_add_test(suite, "power zero exp", test_power_zero_exp);
        CU_add_test(suite, "power positive exp", test_power_positive_exp);
        CU_add_test(suite, "power loop condition", test_power_loop_condition);
    }
}
