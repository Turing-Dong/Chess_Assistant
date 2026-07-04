#ifndef APP_CONFIG_H
#define APP_CONFIG_H

#define OSS_BASE_URL \
    "https://esp32-chess-assistant.oss-cn-hangzhou.aliyuncs.com"

#define DEVICE_ID_BUFFER_SIZE            32U
#define FRAME_ID_BUFFER_SIZE             80U
#define SESSION_ID_BUFFER_SIZE           16U
#define OSS_URL_BUFFER_SIZE              512U
#define RESULT_JSON_BUFFER_SIZE          2048U
#define MANIFEST_JSON_BUFFER_SIZE        1024U

#define OSS_UPLOAD_TIMEOUT_MS            30000U
#define OSS_DOWNLOAD_TIMEOUT_MS          10000U

#define OSS_UPLOAD_RETRY_COUNT           3U
#define OSS_UPLOAD_RETRY_BASE_DELAY_MS   2000U
#define IMAGE_SUCCESS_BEEP_MS            500U

#define RESULT_POLL_INTERVAL_MS          1000U
#define RESULT_WAIT_TIMEOUT_MS           30000U

#define IMAGE_UPLOAD_PERIOD_MS           30000U
#define IMAGE_RESULT_MIN_CONFIDENCE      0.80F

#define DEVICE_HEARTBEAT_PERIOD_MS       30000U
#define DEVICE_FIRMWARE_VERSION          "1.0.0"

#define TIME_SYNC_TIMEOUT_MS             20000U
#define WIFI_WAIT_TIMEOUT_MS             30000U

#endif
