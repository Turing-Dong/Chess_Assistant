#include "lcd.h"
#include "lcdfont.h"

static const char *TAG = "lcd";

spi_device_handle_t MY_LCD_Handle;
uint8_t lcd_buf[LCD_BUF_SIZE];
lcd_obj_t lcd_self;

typedef struct
{
    uint8_t cmd;
    uint8_t data[16];
    uint8_t databytes;
} lcd_init_cmd_t;

static uint32_t lcd_total_pixels(void)
{
    return (uint32_t)lcd_self.width * lcd_self.height;
}

#if LCD_DRIVER_MODE == LCD_DRIVER_SOFTWARE

static inline void lcd_bus_delay(void)
{
    __asm__ __volatile__("nop");
    __asm__ __volatile__("nop");
}

static void lcd_write_9bit_software(uint8_t dc, uint8_t data)
{
    LCD_CS(0);

    /*
     * The panel RS pin is tied to GND in 3-wire mode. Command/data selection
     * is therefore carried exclusively by the first serial bit:
     * 0 = command, 1 = parameter/pixel data.
     */
    LCD_MOSI(dc);
    LCD_SCLK(0);
    lcd_bus_delay();
    LCD_SCLK(1);
    lcd_bus_delay();

    for (uint8_t i = 0; i < 8; i++)
    {
        LCD_MOSI(data & 0x80);
        data <<= 1;
        LCD_SCLK(0);
        lcd_bus_delay();
        LCD_SCLK(1);
        lcd_bus_delay();
    }

    LCD_CS(1);
}

#else

#define LCD_SPI_PACKED_CHUNK_SIZE \
    ((LCD_SPI_DATA_CHUNK * 9 + 7) / 8)

static uint8_t lcd_spi_tx_buf[LCD_SPI_PACKED_CHUNK_SIZE]
    __attribute__((aligned(4)));

static void IRAM_ATTR lcd_spi_pre_transfer_callback(spi_transaction_t *transaction)
{
    (void)transaction;
    gpio_set_level(LCD_NUM_CS, 0);
}

static void IRAM_ATTR lcd_spi_post_transfer_callback(spi_transaction_t *transaction)
{
    (void)transaction;
    gpio_set_level(LCD_NUM_CS, 1);
}

static void lcd_spi_device_init(void)
{
    spi_device_interface_config_t device_config = {
        .clock_speed_hz = LCD_SPI_CLOCK_HZ,
        /*
         * The verified GPIO waveform keeps SCLK high while idle, changes SDA
         * after the falling edge and lets the panel sample on the rising edge.
         */
        .mode = 3,
        .spics_io_num = -1,
        .queue_size = 1,
        .flags = SPI_DEVICE_HALFDUPLEX,
        .pre_cb = lcd_spi_pre_transfer_callback,
        .post_cb = lcd_spi_post_transfer_callback,
    };

    ESP_ERROR_CHECK(spi_bus_add_device(SPI2_HOST, &device_config, &MY_LCD_Handle));
}

static void lcd_write_9bit_hardware(uint8_t dc, uint8_t data)
{
    spi_transaction_ext_t transaction = {
        .base = {
            .flags = SPI_TRANS_VARIABLE_CMD | SPI_TRANS_USE_TXDATA,
            .cmd = dc ? 1 : 0,
            .length = 8,
            .tx_data = {data},
        },
        .command_bits = 1,
    };

    /*
     * Use the SPI peripheral's native command phase for the ninth D/C bit.
     * This avoids any dependence on CPU byte order for a non-byte-aligned
     * transmit buffer.
     */
    ESP_ERROR_CHECK(spi_device_polling_transmit(
        MY_LCD_Handle, (spi_transaction_t *)&transaction));
}

static void lcd_write_data_hardware(const uint8_t *data, int len)
{
    while (len > 0)
    {
        int chunk = (len > LCD_SPI_DATA_CHUNK) ? LCD_SPI_DATA_CHUNK : len;
        size_t packed_index = 0;
        uint32_t pending = 0;
        uint8_t pending_bits = 0;

        for (int i = 0; i < chunk; i++)
        {
            pending = (pending << 9) | (0x100U | data[i]);
            pending_bits += 9;

            while (pending_bits >= 8)
            {
                pending_bits -= 8;
                lcd_spi_tx_buf[packed_index++] =
                    (uint8_t)(pending >> pending_bits);

                pending = pending_bits == 0
                              ? 0
                              : pending & ((1U << pending_bits) - 1U);
            }
        }

        if (pending_bits != 0)
        {
            lcd_spi_tx_buf[packed_index] =
                (uint8_t)(pending << (8U - pending_bits));
        }

        spi_transaction_t transaction = {
            .length = chunk * 9,
            .tx_buffer = lcd_spi_tx_buf,
        };

        /*
         * The queued DMA transaction blocks this task while the peripheral
         * shifts the packed 9-bit words, allowing the idle task to run.
         */
        ESP_ERROR_CHECK(spi_device_transmit(MY_LCD_Handle, &transaction));

        data += chunk;
        len -= chunk;
    }
}

#endif

static void lcd_write_9bit(uint8_t dc, uint8_t data)
{
#if LCD_DRIVER_MODE == LCD_DRIVER_SOFTWARE
    lcd_write_9bit_software(dc, data);
#else
    lcd_write_9bit_hardware(dc, data);
#endif
}

void lcd_write_cmd(uint8_t cmd)
{
    lcd_write_9bit(0, cmd);
}

void lcd_write_data(const uint8_t *data, int len)
{
    if (len <= 0)
    {
        return;
    }

#if LCD_DRIVER_MODE == LCD_DRIVER_HARDWARE_SPI
    /*
     * Commands, address parameters and initialization registers must retain
     * the original 9-bit framing: CS is released after every byte. Large
     * pixel buffers can use a continuous stream of packed 9-bit words.
     */
    if (len < LCD_SPI_BULK_MIN_BYTES)
    {
        for (int i = 0; i < len; i++)
        {
            lcd_write_9bit_hardware(1, data[i]);
        }
    }
    else
    {
        lcd_write_data_hardware(data, len);
    }
#else
    for (int i = 0; i < len; i++)
    {
        lcd_write_9bit(1, data[i]);
    }
#endif
}

void lcd_write_data16(uint16_t data)
{
    uint8_t data_buf[2] = {(uint8_t)(data >> 8), (uint8_t)data};

    lcd_write_data(data_buf, sizeof(data_buf));
}

void lcd_set_window(uint16_t xstar, uint16_t ystar, uint16_t xend, uint16_t yend)
{
    uint8_t data[4];

    data[0] = xstar >> 8;
    data[1] = xstar & 0xFF;
    data[2] = xend >> 8;
    data[3] = xend & 0xFF;
    lcd_write_cmd(lcd_self.setxcmd);
    lcd_write_data(data, sizeof(data));

    data[0] = ystar >> 8;
    data[1] = ystar & 0xFF;
    data[2] = yend >> 8;
    data[3] = yend & 0xFF;
    lcd_write_cmd(lcd_self.setycmd);
    lcd_write_data(data, sizeof(data));

    lcd_write_cmd(lcd_self.wramcmd);
}

void lcd_clear(uint16_t color)
{
    uint32_t pixels = lcd_total_pixels();
    uint32_t chunk_pixels = LCD_BUF_SIZE / 2;

    for (uint32_t i = 0; i < chunk_pixels; i++)
    {
        lcd_buf[i * 2] = color >> 8;
        lcd_buf[i * 2 + 1] = color & 0xFF;
    }

    lcd_set_window(0, 0, lcd_self.width - 1, lcd_self.height - 1);

    while (pixels)
    {
        uint32_t send_pixels = (pixels > chunk_pixels) ? chunk_pixels : pixels;
        lcd_write_data(lcd_buf, send_pixels * 2);
        pixels -= send_pixels;
    }
}

void lcd_fill(uint16_t sx, uint16_t sy, uint16_t ex, uint16_t ey, uint16_t color)
{
    uint32_t pixels;
    uint32_t chunk_pixels = LCD_BUF_SIZE / 2;

    if (sx >= lcd_self.width || sy >= lcd_self.height)
    {
        return;
    }

    if (ex >= lcd_self.width)
    {
        ex = lcd_self.width - 1;
    }

    if (ey >= lcd_self.height)
    {
        ey = lcd_self.height - 1;
    }

    if (ex < sx || ey < sy)
    {
        return;
    }

    pixels = (uint32_t)(ex - sx + 1) * (ey - sy + 1);

    for (uint32_t i = 0; i < chunk_pixels; i++)
    {
        lcd_buf[i * 2] = color >> 8;
        lcd_buf[i * 2 + 1] = color & 0xFF;
    }

    lcd_set_window(sx, sy, ex, ey);

    while (pixels)
    {
        uint32_t send_pixels = (pixels > chunk_pixels) ? chunk_pixels : pixels;
        lcd_write_data(lcd_buf, send_pixels * 2);
        pixels -= send_pixels;
    }
}

void lcd_set_cursor(uint16_t xpos, uint16_t ypos)
{
    lcd_set_window(xpos, ypos, xpos, ypos);
}

void lcd_scan_dir(uint8_t dir)
{
    uint8_t regval = 0;
    uint16_t temp;

    if (lcd_self.dir == 1)
    {
        dir = U2D_R2L;
    }

    switch (dir)
    {
        case L2R_U2D:
            regval = (0 << 7) | (0 << 6) | (0 << 5);
            break;
        case L2R_D2U:
            regval = (1 << 7) | (0 << 6) | (0 << 5);
            break;
        case R2L_U2D:
            regval = (0 << 7) | (1 << 6) | (0 << 5);
            break;
        case R2L_D2U:
            regval = (1 << 7) | (1 << 6) | (0 << 5);
            break;
        case U2D_L2R:
            regval = (0 << 7) | (0 << 6) | (1 << 5);
            break;
        case U2D_R2L:
            regval = (0 << 7) | (1 << 6) | (1 << 5);
            break;
        case D2U_L2R:
            regval = (1 << 7) | (0 << 6) | (1 << 5);
            break;
        case D2U_R2L:
            regval = (1 << 7) | (1 << 6) | (1 << 5);
            break;
        default:
            break;
    }

    regval |= (1 << 3);
    lcd_write_cmd(0x36);
    lcd_write_data(&regval, 1);

    if (regval & 0x20)
    {
        if (lcd_self.width < lcd_self.height)
        {
            temp = lcd_self.width;
            lcd_self.width = lcd_self.height;
            lcd_self.height = temp;
        }
    }
    else
    {
        if (lcd_self.width > lcd_self.height)
        {
            temp = lcd_self.width;
            lcd_self.width = lcd_self.height;
            lcd_self.height = temp;
        }
    }

    lcd_set_window(0, 0, lcd_self.width - 1, lcd_self.height - 1);
}

void lcd_display_dir(uint8_t dir)
{
    uint8_t regval;

    lcd_self.wramcmd = 0x2C;
    lcd_self.setxcmd = 0x2A;
    lcd_self.setycmd = 0x2B;

    switch (dir)
    {
        case 1:
            lcd_self.dir = 0;
            lcd_self.width = LCD_WIDTH;
            lcd_self.height = LCD_HEIGHT;
            regval = (1 << 3) | (1 << 7) | (0 << 6) | (0 << 5);
            break;
        case 2:
            lcd_self.dir = 1;
            lcd_self.width = LCD_HEIGHT;
            lcd_self.height = LCD_WIDTH;
            regval = (1 << 3) | (1 << 7) | (1 << 6) | (1 << 5);
            break;
        case 3:
            lcd_self.dir = 1;
            lcd_self.width = LCD_HEIGHT;
            lcd_self.height = LCD_WIDTH;
            regval = (1 << 3) | (0 << 7) | (0 << 6) | (1 << 5);
            break;
        case 0:
        default:
            lcd_self.dir = 0;
            lcd_self.width = LCD_WIDTH;
            lcd_self.height = LCD_HEIGHT;
            regval = (1 << 3) | (0 << 7) | (1 << 6) | (0 << 5);
            break;
    }

    lcd_write_cmd(0x36);
    lcd_write_data(&regval, 1);
    lcd_set_window(0, 0, lcd_self.width - 1, lcd_self.height - 1);
}

void lcd_draw_pixel(uint16_t x, uint16_t y, uint16_t color)
{
    if (x >= lcd_self.width || y >= lcd_self.height)
    {
        return;
    }

    lcd_set_cursor(x, y);
    lcd_write_data16(color);
}

void lcd_draw_line(uint16_t x1, uint16_t y1, uint16_t x2, uint16_t y2, uint16_t color)
{
    uint16_t t;
    int xerr = 0;
    int yerr = 0;
    int delta_x;
    int delta_y;
    int distance;
    int incx;
    int incy;
    int urow;
    int ucol;

    delta_x = x2 - x1;
    delta_y = y2 - y1;
    urow = x1;
    ucol = y1;

    if (delta_x > 0)
    {
        incx = 1;
    }
    else if (delta_x == 0)
    {
        incx = 0;
    }
    else
    {
        incx = -1;
        delta_x = -delta_x;
    }

    if (delta_y > 0)
    {
        incy = 1;
    }
    else if (delta_y == 0)
    {
        incy = 0;
    }
    else
    {
        incy = -1;
        delta_y = -delta_y;
    }

    distance = (delta_x > delta_y) ? delta_x : delta_y;

    for (t = 0; t <= distance + 1; t++)
    {
        lcd_draw_pixel(urow, ucol, color);
        xerr += delta_x;
        yerr += delta_y;

        if (xerr > distance)
        {
            xerr -= distance;
            urow += incx;
        }

        if (yerr > distance)
        {
            yerr -= distance;
            ucol += incy;
        }
    }
}

void lcd_draw_hline(uint16_t x, uint16_t y, uint16_t len, uint16_t color)
{
    if ((len == 0) || (x >= lcd_self.width) || (y >= lcd_self.height))
    {
        return;
    }

    lcd_fill(x, y, x + len - 1, y, color);
}

void lcd_draw_rectangle(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1, uint16_t color)
{
    lcd_draw_line(x0, y0, x1, y0, color);
    lcd_draw_line(x0, y0, x0, y1, color);
    lcd_draw_line(x0, y1, x1, y1, color);
    lcd_draw_line(x1, y0, x1, y1, color);
}

void lcd_draw_circle(uint16_t x0, uint16_t y0, uint16_t r, uint16_t color)
{
    int a = 0;
    int b = r;
    int di = 3 - (r << 1);

    while (a <= b)
    {
        lcd_draw_pixel(x0 - b, y0 - a, color);
        lcd_draw_pixel(x0 + b, y0 - a, color);
        lcd_draw_pixel(x0 - a, y0 + b, color);
        lcd_draw_pixel(x0 - a, y0 - b, color);
        lcd_draw_pixel(x0 + b, y0 + a, color);
        lcd_draw_pixel(x0 + a, y0 - b, color);
        lcd_draw_pixel(x0 + a, y0 + b, color);
        lcd_draw_pixel(x0 - b, y0 + a, color);
        a++;

        if (di < 0)
        {
            di += 4 * a + 6;
        }
        else
        {
            di += 10 + 4 * (a - b);
            b--;
        }
    }
}

void lcd_show_char(uint16_t x, uint16_t y, uint8_t chr, uint8_t size, uint8_t mode, uint16_t color)
{
    uint8_t temp;
    uint8_t t1;
    uint8_t t;
    uint8_t *pfont = NULL;
    uint8_t csize;
    uint16_t colortemp = WHITE;
    uint8_t bits_per_col = 8;

    if ((chr < ' ') || (chr > '~') || (x > (lcd_self.width - size / 2)) || (y > (lcd_self.height - size)))
    {
        return;
    }

    csize = (size / 8 + ((size % 8) ? 1 : 0)) * (size / 2);
    chr = chr - ' ';
    lcd_set_window(x, y, x + size / 2 - 1, y + size - 1);

    switch (size)
    {
        case 12:
            pfont = (uint8_t *)asc2_1206[chr];
            bits_per_col = 6;
            break;
        case 16:
            pfont = (uint8_t *)asc2_1608[chr];
            bits_per_col = 8;
            break;
        case 24:
            pfont = (uint8_t *)asc2_2412[chr];
            break;
        case 32:
            pfont = (uint8_t *)asc2_3216[chr];
            bits_per_col = 8;
            break;
        default:
            return;
    }

    if (size != 24)
    {
        for (t = 0; t < csize; t++)
        {
            temp = pfont[t];

            for (t1 = 0; t1 < bits_per_col; t1++)
            {
                if (temp & 0x80)
                {
                    colortemp = color;
                }
                else if (mode == 0)
                {
                    colortemp = WHITE;
                }

                lcd_write_data16(colortemp);
                temp <<= 1;
            }
        }
    }
    else
    {
        csize = (size * 16) / 8;

        for (t = 0; t < csize; t++)
        {
            temp = asc2_2412[chr][t];
            bits_per_col = (t % 2 == 0) ? 8 : 4;

            for (t1 = 0; t1 < bits_per_col; t1++)
            {
                if (temp & 0x80)
                {
                    colortemp = color;
                }
                else if (mode == 0)
                {
                    colortemp = WHITE;
                }

                lcd_write_data16(colortemp);
                temp <<= 1;
            }
        }
    }
}

uint32_t lcd_pow(uint8_t m, uint8_t n)
{
    uint32_t result = 1;

    while (n--)
    {
        result *= m;
    }

    return result;
}

void lcd_show_num(uint16_t x, uint16_t y, uint32_t num, uint8_t len, uint8_t size, uint16_t color)
{
    uint8_t temp;
    uint8_t enshow = 0;

    for (uint8_t t = 0; t < len; t++)
    {
        temp = (num / lcd_pow(10, len - t - 1)) % 10;

        if (enshow == 0 && t < (len - 1))
        {
            if (temp == 0)
            {
                lcd_show_char(x + (size / 2) * t, y, ' ', size, 0, color);
                continue;
            }

            enshow = 1;
        }

        lcd_show_char(x + (size / 2) * t, y, temp + '0', size, 0, color);
    }
}

void lcd_show_xnum(uint16_t x, uint16_t y, uint32_t num, uint8_t len, uint8_t size, uint8_t mode, uint16_t color)
{
    uint8_t temp;
    uint8_t enshow = 0;

    for (uint8_t t = 0; t < len; t++)
    {
        temp = (num / lcd_pow(10, len - t - 1)) % 10;

        if (enshow == 0 && t < (len - 1))
        {
            if (temp == 0)
            {
                lcd_show_char(x + (size / 2) * t, y, (mode & 0x80) ? '0' : ' ', size, mode & 0x01, color);
                continue;
            }

            enshow = 1;
        }

        lcd_show_char(x + (size / 2) * t, y, temp + '0', size, mode & 0x01, color);
    }
}

void lcd_show_string(uint16_t x, uint16_t y, uint16_t width, uint16_t height, uint8_t size, char *p, uint16_t color)
{
    uint16_t x0 = x;

    width += x;
    height += y;

    while ((*p <= '~') && (*p >= ' '))
    {
        if (x >= width)
        {
            x = x0;
            y += size;
        }

        if (y >= height)
        {
            break;
        }

        lcd_show_char(x, y, *p, size, 0, color);
        x += size / 2;
        p++;
    }
}

void lcd_on(void)
{
    lcd_write_cmd(0x29);
    vTaskDelay(pdMS_TO_TICKS(10));
}

void lcd_off(void)
{
    lcd_write_cmd(0x28);
    vTaskDelay(pdMS_TO_TICKS(10));
}

static void lcd_hard_reset(void)
{
    uint8_t output_regs[2] = {0};
    uint8_t config_regs[2] = {0};

    pca9555a_ioconfig(PCA9555A_DEFAULT_CONFIG & ~(SLCD_PWR_IO | SLCD_RST_IO));
    LCD_PWR(1);
    LCD_RST(1);
    vTaskDelay(pdMS_TO_TICKS(10));
    LCD_RST(0);
    vTaskDelay(pdMS_TO_TICKS(20));
    LCD_RST(1);
    vTaskDelay(pdMS_TO_TICKS(120));

    esp_err_t output_error =
        pca9555a_read_registers(PCA9555A_OUTPUT_PORT0_REG, output_regs, sizeof(output_regs));
    esp_err_t config_error =
        pca9555a_read_registers(PCA9555A_CONFIG_PORT0_REG, config_regs, sizeof(config_regs));

    if (output_error == ESP_OK && config_error == ESP_OK)
    {
        uint16_t output = ((uint16_t)output_regs[1] << 8) | output_regs[0];
        uint16_t config = ((uint16_t)config_regs[1] << 8) | config_regs[0];

        ESP_LOGI(TAG, "PCA9555A LCD control: output=0x%04X config=0x%04X",
                 output, config);
    }
    else
    {
        ESP_LOGE(TAG, "Failed to verify LCD power/reset: output=%s config=%s",
                 esp_err_to_name(output_error), esp_err_to_name(config_error));
    }
}

static void lcd_gpio_init(void)
{
    gpio_config_t gpio_init_struct = {
#if LCD_DRIVER_MODE == LCD_DRIVER_SOFTWARE
        .pin_bit_mask = (1ULL << LCD_NUM_CS) |
                        (1ULL << LCD_NUM_MOSI) |
                        (1ULL << LCD_NUM_SCLK),
#else
        .pin_bit_mask = (1ULL << LCD_NUM_CS),
#endif
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };

    if (gpio_init_struct.pin_bit_mask != 0)
    {
        gpio_config(&gpio_init_struct);
    }

#if LCD_DRIVER_MODE == LCD_DRIVER_SOFTWARE
    gpio_config_t gpio_input_struct = {
        .pin_bit_mask = 1ULL << LCD_NUM_SDO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };

    gpio_config(&gpio_input_struct);

    LCD_CS(1);
    LCD_MOSI(1);
    LCD_SCLK(1);
#else
    LCD_CS(1);
    ESP_LOGI(TAG, "Hardware SPI: %d Hz, mode 3, GPIO-controlled CS",
             LCD_SPI_CLOCK_HZ);
#endif
}

static void lcd_send_init_cmds(void)
{
    int cmd = 0;
    const lcd_init_cmd_t st7796_init_cmds[] = {
        {0x01, {0}, 0x80},
        {0x11, {0}, 0x80},
        {0xF0, {0xC3}, 1},
        {0xF0, {0x96}, 1},
        {0x36, {0x48}, 1},
        {0x3A, {0x55}, 1},
        {0xB4, {0x01}, 1},
        {0xB7, {0xC6}, 1},
        {0xE8, {0x40, 0x8A, 0x00, 0x00, 0x29, 0x19, 0xA5, 0x33}, 8},
        {0xC1, {0x06}, 1},
        {0xC2, {0xA7}, 1},
        {0xC5, {0x18}, 1},
        {0xE0, {0xF0, 0x09, 0x0B, 0x06, 0x04, 0x15, 0x2F, 0x54, 0x42, 0x3C, 0x17, 0x14, 0x18, 0x1B}, 14},
        {0xE1, {0xF0, 0x09, 0x0B, 0x06, 0x04, 0x03, 0x2D, 0x43, 0x42, 0x3B, 0x16, 0x14, 0x17, 0x1B}, 14},
        {0xF0, {0x3C}, 1},
        {0xF0, {0x69}, 0x81},
        {0x29, {0}, 0x80},
        {0, {0}, 0xFF},
    };

    while (st7796_init_cmds[cmd].databytes != 0xFF)
    {
        lcd_write_cmd(st7796_init_cmds[cmd].cmd);
        lcd_write_data(st7796_init_cmds[cmd].data, st7796_init_cmds[cmd].databytes & 0x1F);

        if (st7796_init_cmds[cmd].databytes & 0x80)
        {
            vTaskDelay(pdMS_TO_TICKS(120));
        }

        cmd++;
    }
}

void lcd_init(void)
{
#if LCD_DRIVER_MODE == LCD_DRIVER_HARDWARE_SPI
    ESP_LOGI(TAG, "LCD init: 3-wire hardware SPI, CS=%d SCK=%d SDA=%d",
             LCD_NUM_CS, LCD_NUM_SCLK, LCD_NUM_MOSI);
#else
    ESP_LOGI(TAG, "LCD init: 3-wire software SPI, CS=%d SCK=%d SDA=%d",
             LCD_NUM_CS, LCD_NUM_SCLK, LCD_NUM_MOSI);
#endif

    lcd_self.dir = 0;
    lcd_self.rs = LCD_NUM_RS;
    lcd_self.cs = LCD_NUM_CS;

    lcd_gpio_init();
#if LCD_DRIVER_MODE == LCD_DRIVER_HARDWARE_SPI
    lcd_spi_device_init();
#endif
    lcd_hard_reset();
    lcd_send_init_cmds();
    lcd_display_dir(LCD_DEFAULT_DIR);

#if LCD_DIAGNOSTIC_MODE
    /* A visible red frame proves reset, 9-bit transfer and GRAM writes. */
    ESP_LOGI(TAG, "LCD command initialization complete; writing red test frame");
    lcd_clear(RED);
    ESP_LOGI(TAG, "LCD red test frame complete");
#endif
}
