#ifndef DEVICE_IDENTITY_H
#define DEVICE_IDENTITY_H

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "app_config.h"

typedef struct
{
    char device_id[DEVICE_ID_BUFFER_SIZE];
    uint32_t session_id;
    uint32_t sequence;
} device_identity_t;

/* Stable per board: esp32-XXXXXX from the Wi-Fi STA MAC suffix. */
esp_err_t device_identity_generate_device_id(char *device_id,
                                             size_t device_id_size);

/* Runtime identity: stable device_id plus a fresh session_id per boot. */
esp_err_t device_identity_init(device_identity_t *identity);
esp_err_t device_identity_format_session_id(const device_identity_t *identity,
                                            char *session_id,
                                            size_t session_id_size);
esp_err_t device_identity_generate_frame_id(device_identity_t *identity,
                                            char *frame_id,
                                            size_t frame_id_size);

#endif
