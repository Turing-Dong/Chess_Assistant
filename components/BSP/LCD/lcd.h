#ifndef __LCD_H__
#define __LCD_H__

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "pca9555a.h"
#include "spi.h"

/* LCD driver selection */
#define LCD_DRIVER_SOFTWARE       0
#define LCD_DRIVER_HARDWARE_SPI   1

#ifndef LCD_DRIVER_MODE
#define LCD_DRIVER_MODE           LCD_DRIVER_HARDWARE_SPI
#endif

/* Keep enabled while diagnosing a white screen. The application stops after
 * LCD initialization and leaves a solid red frame on the panel. */
#define LCD_DIAGNOSTIC_MODE       0

#if (LCD_DRIVER_MODE != LCD_DRIVER_SOFTWARE) && \
    (LCD_DRIVER_MODE != LCD_DRIVER_HARDWARE_SPI)
#error "LCD_DRIVER_MODE must be LCD_DRIVER_SOFTWARE or LCD_DRIVER_HARDWARE_SPI"
#endif

/* ST7796 SPI interface */
#define LCD_NUM_RS      GPIO_NUM_9
#define LCD_NUM_CS      GPIO_NUM_10
#define LCD_NUM_MOSI    GPIO_NUM_11
#define LCD_NUM_SCLK    GPIO_NUM_12
#define LCD_NUM_SDO     GPIO_NUM_13

/* Verified hardware SPI clock for the 3-wire 9-bit interface. */
#define LCD_SPI_CLOCK_HZ    (8 * 1000 * 1000)
#define LCD_SPI_DATA_CHUNK  1024
#define LCD_SPI_BULK_MIN_BYTES  32

#define LCD_RS(x)       gpio_set_level(LCD_NUM_RS, (x) ? 1 : 0)
#define LCD_DC(x)       LCD_RS(x)
#define LCD_CS(x)       gpio_set_level(LCD_NUM_CS, (x) ? 1 : 0)
#define LCD_MOSI(x)     gpio_set_level(LCD_NUM_MOSI, (x) ? 1 : 0)
#define LCD_SCLK(x)     gpio_set_level(LCD_NUM_SCLK, (x) ? 1 : 0)
#define LCD_SDO()       gpio_get_level(LCD_NUM_SDO)
#define LCD_PWR(x)      pca9555a_pin_write(SLCD_PWR_IO, (x) ? 1 : 0)
#define LCD_RST(x)      pca9555a_pin_write(SLCD_RST_IO, (x) ? 1 : 0)

#define LCD_WIDTH       320
#define LCD_HEIGHT      480

#define LCD_DIR_PORTRAIT             0
#define LCD_DIR_PORTRAIT_REVERSE     1
#define LCD_DIR_LANDSCAPE            2
#define LCD_DIR_LANDSCAPE_REVERSE    3
#define LCD_DEFAULT_DIR              LCD_DIR_LANDSCAPE

#define WHITE           0xFFFF
#define BLACK           0x0000
#define RED             0xF800
#define GREEN           0x07E0
#define BLUE            0x001F
#define MAGENTA         0xF81F
#define YELLOW          0xFFE0
#define CYAN            0x07FF
#define BROWN           0xBC40
#define BRRED           0xFC07
#define GRAY            0x8430
#define DARKBLUE        0x01CF
#define LIGHTBLUE       0x7D7C
#define GRAYBLUE        0x5458
#define LIGHTGREEN      0x841F
#define LGRAY           0xC618
#define LGRAYBLUE       0xA651
#define LBBLUE          0x2B12

#define L2R_U2D         0
#define L2R_D2U         1
#define R2L_U2D         2
#define R2L_D2U         3
#define U2D_L2R         4
#define U2D_R2L         5
#define D2U_L2R         6
#define D2U_R2L         7

#define DFT_SCAN_DIR    L2R_U2D

typedef struct _lcd_obj_t
{
    uint16_t width;
    uint16_t height;
    uint8_t dir;
    uint16_t wramcmd;
    uint16_t setxcmd;
    uint16_t setycmd;
    uint16_t rs;
    uint16_t cs;
} lcd_obj_t;

#define LCD_TOTAL_BUF_SIZE      (LCD_WIDTH * LCD_HEIGHT * 2)
#define LCD_BUF_SIZE            4096

extern lcd_obj_t lcd_self;
extern uint8_t lcd_buf[LCD_BUF_SIZE];

void lcd_init(void);
void lcd_on(void);
void lcd_off(void);
void lcd_clear(uint16_t color);
void lcd_display_dir(uint8_t dir);
void lcd_scan_dir(uint8_t dir);
void lcd_write_cmd(uint8_t cmd);
void lcd_write_data(const uint8_t *data, int len);
void lcd_write_data16(uint16_t data);
void lcd_set_cursor(uint16_t xpos, uint16_t ypos);
void lcd_set_window(uint16_t xstar, uint16_t ystar, uint16_t xend, uint16_t yend);
void lcd_fill(uint16_t sx, uint16_t sy, uint16_t ex, uint16_t ey, uint16_t color);
void lcd_show_num(uint16_t x, uint16_t y, uint32_t num, uint8_t len, uint8_t size, uint16_t color);
void lcd_show_xnum(uint16_t x, uint16_t y, uint32_t num, uint8_t len, uint8_t size, uint8_t mode, uint16_t color);
void lcd_show_string(uint16_t x, uint16_t y, uint16_t width, uint16_t height, uint8_t size, char *p, uint16_t color);
void lcd_draw_rectangle(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1, uint16_t color);
void lcd_draw_hline(uint16_t x, uint16_t y, uint16_t len, uint16_t color);
void lcd_draw_line(uint16_t x1, uint16_t y1, uint16_t x2, uint16_t y2, uint16_t color);
void lcd_draw_pixel(uint16_t x, uint16_t y, uint16_t color);
void lcd_draw_circle(uint16_t x0, uint16_t y0, uint16_t r, uint16_t color);
void lcd_show_char(uint16_t x, uint16_t y, uint8_t chr, uint8_t size, uint8_t mode, uint16_t color);

#endif
