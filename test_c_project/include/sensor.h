#ifndef SENSOR_H
#define SENSOR_H

#include <stdint.h>

#define MAX_TEMP 100.0f
#define MIN_TEMP -40.0f
#define WARN_TEMP 80.0f

typedef struct {
    float temperature;
    float humidity;
    int status;
} SensorData;

typedef enum {
    SENSOR_OK = 0,
    SENSOR_WARN,
    SENSOR_ERROR,
    SENSOR_INVALID
} SensorStatus;

SensorData read_sensor(int id);
SensorStatus classify_reading(const SensorData *data);
int is_sensor_valid(const SensorData *data);
int compute_checksum(const uint8_t *buf, int len);

#endif
