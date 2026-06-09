/* Test file for batch _2_src_math_ops_add_subtract */
#include <CUnit/Basic.h>
#include "math_ops.h"

/* ---------- tests for add ---------- */
void test_add_positive(void) {
    CU_ASSERT_EQUAL(add(2, 3), 5);
}

void test_add_negative(void) {
    CU_ASSERT_EQUAL(add(-2, 3), 1);
}

void test_add_zero(void) {
    CU_ASSERT_EQUAL(add(0, 0), 0);
    CU_ASSERT_EQUAL(add(5, 0), 5);
}

void test_add_large_numbers(void) {
    CU_ASSERT_EQUAL(add(1000000, 2000000), 3000000);
}

/* ---------- tests for subtract ---------- */
void test_subtract_positive(void) {
    CU_ASSERT_EQUAL(subtract(5, 3), 2);
}

void test_subtract_negative_result(void) {
    CU_ASSERT_EQUAL(subtract(3, 5), -2);
}

void test_subtract_with_negative_operands(void) {
    CU_ASSERT_EQUAL(subtract(-5, -3), -2);
}

void test_subtract_zero(void) {
    CU_ASSERT_EQUAL(subtract(0, 0), 0);
    CU_ASSERT_EQUAL(subtract(10, 0), 10);
}

/* ---------- tests for multiply ---------- */
void test_multiply_positive(void) {
    CU_ASSERT_EQUAL(multiply(2, 3), 6);
}

void test_multiply_zero(void) {
    CU_ASSERT_EQUAL(multiply(5, 0), 0);
    CU_ASSERT_EQUAL(multiply(0, 5), 0);
}

void test_multiply_negative(void) {
    CU_ASSERT_EQUAL(multiply(-2, 3), -6);
    CU_ASSERT_EQUAL(multiply(-2, -3), 6);
}

/* ---------- tests for divide ---------- */

void test_divide_typical(void) {
    Result res = divide(10, 3);
    CU_ASSERT_EQUAL(res.quotient, 3);
    CU_ASSERT_EQUAL(res.remainder, 1);
    CU_ASSERT_EQUAL(res.error, SUCCESS);
}

void test_divide_by_zero(void) {
    /* Exercise b == 0 true → error set */
    Result res = divide(10, 0);
    CU_ASSERT_EQUAL(res.error, ERROR_ZERO);
    CU_ASSERT_EQUAL(res.quotient, 0);
    CU_ASSERT_EQUAL(res.remainder, 0);
}

void test_divide_negative_dividend(void) {
    /* Exercise b != 0 false → normal division */
    Result res = divide(-10, 3);
    /* C integer division truncates toward zero: -10/3 = -3, remainder -1 */
    CU_ASSERT_EQUAL(res.quotient, -3);
    CU_ASSERT_EQUAL(res.remainder, -1);
    CU_ASSERT_EQUAL(res.error, SUCCESS);
}

void test_divide_even_division(void) {
    Result res = divide(12, 4);
    CU_ASSERT_EQUAL(res.quotient, 3);
    CU_ASSERT_EQUAL(res.remainder, 0);
    CU_ASSERT_EQUAL(res.error, SUCCESS);
}

/* Registration function */
void register__2_src_math_ops_add_subtract_tests(void) {
    CU_pSuite suite = CU_add_suite("math_ops_add_subtract", NULL, NULL);
    if (NULL == suite) return;

    CU_add_test(suite, "test_add_positive", test_add_positive);
    CU_add_test(suite, "test_add_negative", test_add_negative);
    CU_add_test(suite, "test_add_zero", test_add_zero);
    CU_add_test(suite, "test_add_large_numbers", test_add_large_numbers);

    CU_add_test(suite, "test_subtract_positive", test_subtract_positive);
    CU_add_test(suite, "test_subtract_negative_result", test_subtract_negative_result);
    CU_add_test(suite, "test_subtract_with_negative_operands", test_subtract_with_negative_operands);
    CU_add_test(suite, "test_subtract_zero", test_subtract_zero);

    CU_add_test(suite, "test_multiply_positive", test_multiply_positive);
    CU_add_test(suite, "test_multiply_zero", test_multiply_zero);
    CU_add_test(suite, "test_multiply_negative", test_multiply_negative);

    CU_add_test(suite, "test_divide_typical", test_divide_typical);
    CU_add_test(suite, "test_divide_by_zero", test_divide_by_zero);
    CU_add_test(suite, "test_divide_negative_dividend", test_divide_negative_dividend);
    CU_add_test(suite, "test_divide_even_division", test_divide_even_division);
}
