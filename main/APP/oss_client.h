#ifndef OSS_CLIENT_H
#define OSS_CLIENT_H

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/* Object keys are written into latest.json for the PC/backend processor. */
esp_err_t oss_build_image_key(const char *device_id,
                              const char *frame_id,
                              char *key,
                              size_t key_size);
esp_err_t oss_build_result_key(const char *device_id,
                               const char *frame_id,
                               char *key,
                               size_t key_size);
esp_err_t oss_build_image_url(const char *device_id,
                              const char *frame_id,
                              char *url,
                              size_t url_size);
esp_err_t oss_build_manifest_url(const char *device_id,
                                 char *url,
                                 size_t url_size);
esp_err_t oss_build_result_url(const char *device_id,
                               const char *frame_id,
                               char *url,
                               size_t url_size);
esp_err_t oss_build_heartbeat_url(const char *device_id,
                                  char *url,
                                  size_t url_size);

/* These functions perform HTTPS requests and keep TLS verification enabled. */
esp_err_t oss_upload_jpeg(const char *url,
                          const uint8_t *image_data,
                          size_t image_length);
esp_err_t oss_put_json(const char *url,
                       const char *json_text,
                       size_t json_length);
esp_err_t oss_get_json(const char *url,
                       char *json_buffer,
                       size_t json_buffer_size,
                       size_t *json_length,
                       int *http_status);

#endif
