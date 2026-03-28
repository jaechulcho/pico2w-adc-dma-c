#include <stdio.h>
#include <math.h>
#include "pico/stdlib.h"
#include "pico/stdio_usb.h"
#include "hardware/adc.h"
#include "hardware/dma.h"
#include "hardware/pwm.h"
#include "hardware/clocks.h" // Needed for clk_sys freq
#include "pico/cyw43_arch.h"

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define ADC_NUM_CH1 0
#define ADC_NUM_CH2 1
#define ADC_PIN_CH1 (26 + ADC_NUM_CH1) // GPIO 26
#define ADC_PIN_CH2 (26 + ADC_NUM_CH2) // GPIO 27

#define DAC_PIN 28
#define PWM_WRAP 1023
#define DDS_UPDATE_HZ 50000

#define SAMPLES_PER_CHANNEL 1000
#define SETTLING_SAMPLES 100
#define CAPTURE_DEPTH ((SAMPLES_PER_CHANNEL + SETTLING_SAMPLES) * 2)
#define PLOT_COUNT (SAMPLES_PER_CHANNEL * 2)

uint16_t capture_buf[CAPTURE_DEPTH];
uint16_t sine_lut[256];

// DDS / PWM State
volatile uint32_t phase_acc = 0;
volatile uint32_t phase_inc = 0;
volatile uint8_t dac_type = 0;   // 0:Off, 1:Sine, 2:Triangle, 3:Square, 4:Static PWM
volatile float dac_amp = 1.0f;   // Used as Amplitude or Duty Cycle (0.0 - 1.0)

bool dac_timer_callback(struct repeating_timer *t) {
    if (dac_type == 0) {
        pwm_set_gpio_level(DAC_PIN, 0);
        return true;
    }
    
    if (dac_type == 4) { // Static PWM Duty mode
        pwm_set_gpio_level(DAC_PIN, (uint16_t)(dac_amp * PWM_WRAP));
        return true;
    }

    phase_acc += phase_inc;
    uint16_t val = 0;
    uint32_t p8 = (phase_acc >> 24) & 0xFF; // 8-bit phase
    
    if (dac_type == 1) { // Sine
        val = sine_lut[p8];
    } else if (dac_type == 2) { // Triangle
        if (p8 < 128) val = (p8 << 3); // 128 * 8 = 1024
        else val = ((255 - p8) << 3);
    } else if (dac_type == 3) { // Square
        val = (phase_acc < 0x80000000) ? PWM_WRAP : 0;
    }
    
    pwm_set_gpio_level(DAC_PIN, (uint16_t)(val * dac_amp));
    return true;
}

void init_dac() {
    for (int i = 0; i < 256; i++) {
        sine_lut[i] = (uint16_t)((sin(2.0 * M_PI * i / 256.0) + 1.0) * 0.5 * PWM_WRAP);
    }
    gpio_set_function(DAC_PIN, GPIO_FUNC_PWM);
    uint slice_num = pwm_gpio_to_slice_num(DAC_PIN);
    pwm_set_wrap(slice_num, PWM_WRAP);
    pwm_set_enabled(slice_num, true);

    static struct repeating_timer timer;
    add_repeating_timer_us(-(1000000 / DDS_UPDATE_HZ), dac_timer_callback, NULL, &timer);
}

int main() {
    stdio_init_all();
    if (cyw43_arch_init()) {
        printf("WiFi init failed\n");
        return -1;
    }
    while (!stdio_usb_connected()) { sleep_ms(10); }

    init_dac();

    adc_gpio_init(ADC_PIN_CH1);
    adc_gpio_init(ADC_PIN_CH2);
    adc_init();
    adc_set_round_robin(0x03); 
    adc_select_input(ADC_NUM_CH1);
    adc_fifo_setup(true, true, 1, false, false);
    // ADC clock is typically 48MHz on RP2350 unless set otherwise.
    adc_set_clkdiv(4800);

    int dma_chan = dma_claim_unused_channel(true);
    dma_channel_config cfg = dma_channel_get_default_config(dma_chan);
    channel_config_set_transfer_data_size(&cfg, DMA_SIZE_16);
    channel_config_set_read_increment(&cfg, false);
    channel_config_set_write_increment(&cfg, true);
    channel_config_set_dreq(&cfg, DREQ_ADC);

    const uint32_t SYNC_HEADER = 0xABCDEF01;
    const uint32_t SYNC_FOOTER = 0xDEADBEEF;
    uint32_t frame_count = 0;

    while (true) {
        static uint32_t last_led_toggle = 0;
        uint32_t now = to_ms_since_boot(get_absolute_time());
        if (now - last_led_toggle >= 1000) {
            static bool led_state = false;
            led_state = !led_state;
            cyw43_arch_gpio_put(CYW43_WL_GPIO_LED_PIN, led_state);
            last_led_toggle = now;
        }

        int cmd = getchar_timeout_us(0);
        if (cmd == 'S') {
            uint32_t new_div = 0;
            for (int i = 0; i < 4; i++) {
                int b = getchar_timeout_us(100000);
                if (b != PICO_ERROR_TIMEOUT) new_div |= ((uint32_t)b << (i * 8));
            }
            if (new_div >= 96) adc_set_clkdiv((float)new_div);
        } else if (cmd == 'W') {
            int type = getchar_timeout_us(100000);
            if (type != PICO_ERROR_TIMEOUT) dac_type = (uint8_t)type;

            uint32_t freq = 0;
            for (int i = 0; i < 4; i++) {
                int b = getchar_timeout_us(100000);
                if (b != PICO_ERROR_TIMEOUT) freq |= ((uint32_t)b << (i * 8));
            }
            phase_inc = (uint32_t)(((uint64_t)freq << 32) / DDS_UPDATE_HZ);

            float amp = 1.0f;
            uint8_t* amp_ptr = (uint8_t*)&amp;
            for (int i = 0; i < 4; i++) {
                int b = getchar_timeout_us(100000);
                if (b != PICO_ERROR_TIMEOUT) amp_ptr[i] = (uint8_t)b;
            }
            dac_amp = amp;
            
            if (dac_type == 4 && freq > 0) {
                uint slice_num = pwm_gpio_to_slice_num(DAC_PIN);
                uint32_t f_sys = clock_get_hz(clk_sys); // Get actual system clock freq
                float div = (float)f_sys / (freq * 1024.0f);
                if (div < 1.0f) div = 1.0f;
                if (div > 255.0f) div = 255.0f;
                pwm_set_clkdiv(slice_num, div);
            }
        }

        dma_channel_configure(dma_chan, &cfg, capture_buf, &adc_hw->fifo, CAPTURE_DEPTH, true);
        adc_run(true);
        dma_channel_wait_for_finish_blocking(dma_chan);
        adc_run(false);
        adc_fifo_drain();

        uint16_t *stable_data = &capture_buf[SETTLING_SAMPLES * 2];
        fwrite(&SYNC_HEADER, sizeof(uint32_t), 1, stdout);
        fwrite(&frame_count, sizeof(uint32_t), 1, stdout);
        fwrite(stable_data, sizeof(uint16_t), PLOT_COUNT, stdout);
        fwrite(&SYNC_FOOTER, sizeof(uint32_t), 1, stdout);
        fflush(stdout);
        frame_count++;
    }
    return 0;
}

