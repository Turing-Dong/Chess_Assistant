#include "time_sync.h"

#include <sys/time.h>
#include <time.h>

#include "esp_log.h"
#include "esp_sntp.h"
#include "sdkconfig.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "time_sync";

bool system_time_is_valid(void)
{
    time_t now = 0;
    struct tm time_info = {0};

    time(&now);
    localtime_r(&now, &time_info);

    return time_info.tm_year >= (2024 - 1900);
}

uint64_t system_time_now_ms(void)
{
    struct timeval now = {0};
    gettimeofday(&now, NULL);

    return ((uint64_t)now.tv_sec * 1000ULL) +
           ((uint64_t)now.tv_usec / 1000ULL);
}

esp_err_t system_time_sync(uint32_t timeout_ms)
{
    if (system_time_is_valid())
    {
        time_t now = 0;
        time(&now);
        ESP_LOGI(TAG, "System time already valid: %lld", (long long)now);
        return ESP_OK;
    }

    if (!esp_sntp_enabled())
    {
        esp_sntp_setoperatingmode(SNTP_OPMODE_POLL);
        esp_sntp_setservername(0, "ntp.aliyun.com");
#if CONFIG_LWIP_SNTP_MAX_SERVERS > 1
        esp_sntp_setservername(1, "cn.pool.ntp.org");
#endif
#if CONFIG_LWIP_SNTP_MAX_SERVERS > 2
        esp_sntp_setservername(2, "pool.ntp.org");
#endif
        esp_sntp_init();
    }
    else
    {
        (void)esp_sntp_restart();
    }

    uint32_t elapsed_ms = 0U;
    while (elapsed_ms < timeout_ms)
    {
        if (system_time_is_valid())
        {
            time_t now = 0;
            time(&now);
            ESP_LOGI(TAG, "System time synchronized: %lld", (long long)now);
            return ESP_OK;
        }

        vTaskDelay(pdMS_TO_TICKS(500U));
        elapsed_ms += 500U;
    }

    ESP_LOGE(TAG, "System time sync timeout");
    return ESP_ERR_TIMEOUT;
}
