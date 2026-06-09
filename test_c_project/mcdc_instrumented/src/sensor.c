#include "sensor.h"
#include <string.h>
#include "mcdc_instrumentation.h"

/* 纯 && 判定 */
SensorStatus classify_reading(const SensorData *data)
{
    if (MCDC_COND("src_sensor_c_7", 0, data == NULL)) {
        return SENSOR_INVALID;
    }
    /* Decision 1: a && b && c — 纯合取 */
    if (MCDC_COND("src_sensor_c_11", 0, data->temperature > WARN_TEMP) && MCDC_COND("src_sensor_c_11", 1, data->humidity < 30.0f) && MCDC_COND("src_sensor_c_11", 2, data->status == 1)) {
        return SENSOR_WARN;
    }
    /* Decision 2: a || b — 纯析取 */
    if (MCDC_COND("src_sensor_c_15", 0, data->temperature < MIN_TEMP) || MCDC_COND("src_sensor_c_15", 1, data->temperature > MAX_TEMP)) {
        return SENSOR_ERROR;
    }
    return SENSOR_OK;
}

/* 混合 && / || 判定 */
int is_sensor_valid(const SensorData *data)
{
    if (MCDC_COND("src_sensor_c_24", 0, data == NULL)) {
        return 0;
    }
    /* Decision 3: (a && b) || c — 混合 */
    if ((MCDC_COND("src_sensor_c_28", 0, data->temperature >= MIN_TEMP && data->temperature <= MAX_TEMP)) || MCDC_COND("src_sensor_c_28", 1, data->status == 2)) {
        return 1;
    }
    return 0;
}

/* do-while 判定 */
int compute_checksum(const uint8_t *buf, int len)
{
    int sum = 0;
    int i = 0;
    /* Decision 4: do-while 条件 */
    do {
        sum += buf[i];
        i++;
    } while (MCDC_COND("src_sensor_c_43", 0, MCDC_COND("src_sensor_c_43", 0, i < len)));
    return sum & 0xFF;
}

/* for 循环判定 + 三元运算符 */
SensorData read_sensor(int id)
{
    SensorData data;
    memset(&data, 0, sizeof(data));
    /* Decision 5: for 条件 */
    for (int i = 0; MCDC_COND("src_sensor_c_53", 0, i < id) && MCDC_COND("src_sensor_c_53", 1, i < 10); i++) {
        data.temperature += 0.5f;
    }
    /* Decision 6: 三元运算符 */
    MCDC_COND("src_sensor_c_57", 0, data.status = (data.temperature > WARN_TEMP)) ? 1 : 0;
    return data;
}
