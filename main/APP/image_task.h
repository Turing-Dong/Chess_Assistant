#ifndef IMAGE_TASK_H
#define IMAGE_TASK_H

#include <stddef.h>

#include "esp_err.h"
#include "device_identity.h"
#include "image_result.h"

typedef enum
{
    IMAGE_TASK_STATE_IDLE = 0,
    IMAGE_TASK_STATE_CAPTURE,
    IMAGE_TASK_STATE_UPLOAD_IMAGE,
    IMAGE_TASK_STATE_UPLOAD_MANIFEST,
    IMAGE_TASK_STATE_WAIT_RESULT,
    IMAGE_TASK_STATE_RESULT_READY,
    IMAGE_TASK_STATE_RESULT_FAILED,
    IMAGE_TASK_STATE_TIMEOUT,
    IMAGE_TASK_STATE_ERROR,
} image_task_state_t;

typedef void (*image_task_result_callback_t)(
    const image_process_result_t *result,
    void *user_data);

void image_task_set_result_callback(image_task_result_callback_t callback,
                                    void *user_data);
image_task_state_t image_task_get_state(void);

esp_err_t image_task_build_manifest_json(const device_identity_t *identity,
                                         const char *frame_id,
                                         uint16_t image_width,
                                         uint16_t image_height,
                                         char *json_buffer,
                                         size_t json_buffer_size);

/*
 * Processes exactly one frame:
 * capture JPEG -> upload image -> upload latest.json -> poll result JSON.
 * The camera buffer is released immediately after the JPEG upload finishes.
 */
esp_err_t image_task_process_one_frame(device_identity_t *identity);

#endif
