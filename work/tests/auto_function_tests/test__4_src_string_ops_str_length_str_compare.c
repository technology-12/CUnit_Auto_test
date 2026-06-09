#include <CUnit/Basic.h>
#include "string_ops.h"
#include "common.h"

/* str_length tests */
void test_str_length_null(void) {
    CU_ASSERT_EQUAL(str_length(NULL), ERROR_NEGATIVE);
}

void test_str_length_empty(void) {
    CU_ASSERT_EQUAL(str_length(""), 0);
}

void test_str_length_nonempty(void) {
    CU_ASSERT_EQUAL(str_length("hello"), 5);
    CU_ASSERT_EQUAL(str_length("a"), 1);
}

/* str_compare tests */
void test_str_compare_a_null_b_notnull(void) {
    CU_ASSERT_EQUAL(str_compare(NULL, "abc"), ERROR_NEGATIVE);
}

void test_str_compare_a_notnull_b_null(void) {
    CU_ASSERT_EQUAL(str_compare("abc", NULL), ERROR_NEGATIVE);
}

void test_str_compare_both_null(void) {
    CU_ASSERT_EQUAL(str_compare(NULL, NULL), ERROR_NEGATIVE);
}

void test_str_compare_a_empty(void) {
    CU_ASSERT(str_compare("", "abc") < 0);
}

void test_str_compare_b_empty(void) {
    CU_ASSERT(str_compare("abc", "") > 0);
}

void test_str_compare_equal(void) {
    CU_ASSERT_EQUAL(str_compare("abc", "abc"), 0);
}

void test_str_compare_a_less(void) {
    CU_ASSERT(str_compare("abc", "abd") < 0);
}

void test_str_compare_a_greater(void) {
    CU_ASSERT(str_compare("abd", "abc") > 0);
}

void test_str_compare_a_shorter(void) {
    CU_ASSERT(str_compare("ab", "abc") < 0);
}

void test_str_compare_b_shorter(void) {
    CU_ASSERT(str_compare("abc", "ab") > 0);
}

void test_str_compare_both_empty(void) {
    CU_ASSERT_EQUAL(str_compare("", ""), 0);
}

/* find_char tests */
void test_find_char_null(void) {
    CU_ASSERT_PTR_NULL(find_char(NULL, 'a'));
}

void test_find_char_empty(void) {
    CU_ASSERT_PTR_NULL(find_char("", 'a'));
}

void test_find_char_found_first(void) {
    const char *s = "hello";
    const char *result = find_char(s, 'h');
    CU_ASSERT_PTR_EQUAL(result, &s[0]);
}

void test_find_char_found_mid(void) {
    const char *s = "hello";
    const char *result = find_char(s, 'l');
    CU_ASSERT_PTR_EQUAL(result, &s[2]);
}

void test_find_char_not_found(void) {
    CU_ASSERT_PTR_NULL(find_char("hello", 'z'));
}

void test_find_char_multiple(void) {
    const char *s = "banana";
    const char *result = find_char(s, 'a');
    CU_ASSERT_PTR_EQUAL(result, &s[1]); /* first 'a' at index 1 */
}

/* Registration function */
void register__4_src_string_ops_str_length_str_compare_tests(void) {
    CU_pSuite suite = CU_add_suite("string_ops", NULL, NULL);
    if (suite == NULL) return;

    CU_add_test(suite, "str_length_null", test_str_length_null);
    CU_add_test(suite, "str_length_empty", test_str_length_empty);
    CU_add_test(suite, "str_length_nonempty", test_str_length_nonempty);

    CU_add_test(suite, "str_compare_a_null_b_notnull", test_str_compare_a_null_b_notnull);
    CU_add_test(suite, "str_compare_a_notnull_b_null", test_str_compare_a_notnull_b_null);
    CU_add_test(suite, "str_compare_both_null", test_str_compare_both_null);
    CU_add_test(suite, "str_compare_a_empty", test_str_compare_a_empty);
    CU_add_test(suite, "str_compare_b_empty", test_str_compare_b_empty);
    CU_add_test(suite, "str_compare_equal", test_str_compare_equal);
    CU_add_test(suite, "str_compare_a_less", test_str_compare_a_less);
    CU_add_test(suite, "str_compare_a_greater", test_str_compare_a_greater);
    CU_add_test(suite, "str_compare_a_shorter", test_str_compare_a_shorter);
    CU_add_test(suite, "str_compare_b_shorter", test_str_compare_b_shorter);
    CU_add_test(suite, "str_compare_both_empty", test_str_compare_both_empty);

    CU_add_test(suite, "find_char_null", test_find_char_null);
    CU_add_test(suite, "find_char_empty", test_find_char_empty);
    CU_add_test(suite, "find_char_found_first", test_find_char_found_first);
    CU_add_test(suite, "find_char_found_mid", test_find_char_found_mid);
    CU_add_test(suite, "find_char_not_found", test_find_char_not_found);
    CU_add_test(suite, "find_char_multiple", test_find_char_multiple);
}
