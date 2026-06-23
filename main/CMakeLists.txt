idf_component_register(
    SRCS
        "main.c"
        "APP/app_tasks.c"
        "APP/lvgl_app.c"
        "APP/lv_font_chess_16.c"
    INCLUDE_DIRS 
        "."
        "APP"
    REQUIRES
        BSP
        LVGL
        esp_timer)

