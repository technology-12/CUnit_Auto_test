#include <CUnit/CUnit.h>
#include <CUnit/Basic.h>
#include "common.h"
#include <stdio.h>

/* Expose static print_result by compiling src/main.c with -Dstatic= */
extern void print_result(const char *label, int value);

static void test_print_result_basic(void)
{
    /* Verify that print_result executes without crashing; no stdout capture for now */
    print_result("basic", 42);
    CU_ASSERT_TRUE(1);
}

static void test_clamp_value_less_than_low_returns_low(void)
{
    /* value < low true -> return low */
    CU_ASSERT_EQUAL(clamp(5, 10, 20), 10);
}

static void test_clamp_value_inside_range_returns_value(void)
{
    /* value < low false && value > high false -> return value */
    CU_ASSERT_EQUAL(clamp(15, 10, 20), 15);
}

static void test_clamp_value_greater_than_high_returns_high(void)
{
    /* value > high true -> return high */
    CU_ASSERT_EQUAL(clamp(25, 10, 20), 20);
}

void register__1_src_main_print_result_clamp_tests(void)
{
    CU_pSuite suite = CU_add_suite("src_main_print_result_clamp", NULL, NULL);
    if (suite == NULL) return;
    CU_add_test(suite, "test print_result basic", test_print_result_basic);
    CU_add_test(suite, "test clamp value < low returns low", test_clamp_value_less_than_low_returns_low);
    CU_add_test(suite, "test clamp value inside range returns value", test_clamp_value_inside_range_returns_value);
    CU_add_test(suite, "test clamp value > high returns high", test_clamp_value_greater_than_high_returns_high);
}
