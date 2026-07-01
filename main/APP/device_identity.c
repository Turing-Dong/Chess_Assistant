#include "device_identity.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "esp_mac.h"
#include "esp_random.h"

esp_err_t device_identity_generate_device_id(char *device_id,
                                             size_t device_id_size)
{
    if (device_id == NULL || device_id_size == 0U)
    {
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t mac[6] = {0};
    esp_err_t error = esp_read_mac(mac, ESP_MAC_WIFI_STA);
    if (error != ESP_OK)
    {
        return error;
    }

    int length = snprintf(device_id,
                          device_id_size,
                          "esp32-%02X%02X%02X",
                          mac[3],
                          mac[4],
                          mac[5]);
    if (length < 0 || (size_t)length >= device_id_size)
    {
        return ESP_ERR_INVALID_SIZE;
    }

    return ESP_OK;
}

esp_err_t device_identity_init(device_identity_t *identity)
{
    if (identity == NULL)
    {
        return ESP_ERR_INVALID_ARG;
    }

    memset(identity, 0, sizeof(*identity));

    esp_err_t error = device_identity_generate_device_id(
        identity->device_id,
        sizeof(identity->device_id));
    if (error != ESP_OK)
    {
        return error;
    }

    identity->session_id = esp_random();
    identity->sequence = 0U;

    return ESP_OK;
}

esp_err_t device_identity_format_session_id(const device_identity_t *identity,
                                            char *session_id,
                                            size_t session_id_size)
{
    if (identity == NULL || session_id == NULL || session_id_size == 0U)
    {
        return ESP_ERR_INVALID_ARG;
    }

    int length = snprintf(session_id,
                          session_id_size,
                          "%08" PRIX32,
                          identity->session_id);
    if (length < 0 || (size_t)length >= session_id_size)
    {
        return ESP_ERR_INVALID_SIZE;
    }

    return ESP_OK;
}

esp_err_t device_identity_generate_frame_id(device_identity_t *identity,
                                            char *frame_id,
                                            size_t frame_id_size)
{
    if (identity == NULL || frame_id == NULL || frame_id_size == 0U)
    {
        return ESP_ERR_INVALID_ARG;
    }

    identity->sequence++;

    int length = snprintf(frame_id,
                          frame_id_size,
                          "%s-%08" PRIX32 "-%06" PRIu32,
                          identity->device_id,
                          identity->session_id,
                          identity->sequence);
    if (length < 0 || (size_t)length >= frame_id_size)
    {
        identity->sequence--;
        return ESP_ERR_INVALID_SIZE;
    }

    return ESP_OK;
}
