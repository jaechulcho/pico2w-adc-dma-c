#include <stdio.h>
#include "pico/stdlib.h"
#include "pico/stdio_usb.h"
#include "hardware/adc.h"
#include "hardware/dma.h"

#define ADC_NUM_CH1 0
#define ADC_NUM_CH2 1
#define ADC_PIN_CH1 (26 + ADC_NUM_CH1) // GPIO 26
#define ADC_PIN_CH2 (26 + ADC_NUM_CH2) // GPIO 27

#define SAMPLES_PER_CHANNEL 1000
#define SETTLING_SAMPLES 100
#define CAPTURE_DEPTH ((SAMPLES_PER_CHANNEL + SETTLING_SAMPLES) * 2)
#define PLOT_COUNT (SAMPLES_PER_CHANNEL * 2)

uint16_t capture_buf[CAPTURE_DEPTH];

int main() {
    stdio_init_all();
    
    // Wait for USB connection (optional but recommended for seeing first prints)
    while (!stdio_usb_connected()) {
        sleep_ms(100);
    }
    
    printf("\n--- Pico 2 W ADC DMA High-Speed USB Start ---\n");

    // ADC setup
    adc_gpio_init(ADC_PIN_CH1);
    adc_gpio_init(ADC_PIN_CH2);
    adc_init();
    
    // Select round-robin for ADC0 and ADC1
    adc_set_round_robin(0x03); 
    // Start with CH1 (ADC0)
    adc_select_input(ADC_NUM_CH1);

    // Setup ADC FIFO
    adc_fifo_setup(true, true, 1, false, false);

    // Set ADC sampling rate (48MHz / 4800 = 10kHz total, 5kHz per channel)
    adc_set_clkdiv(4800);

    // DMA setup
    int dma_chan = dma_claim_unused_channel(true);
    dma_channel_config cfg = dma_channel_get_default_config(dma_chan);

    // Reading from ADC FIFO, writing to capture_buf
    channel_config_set_transfer_data_size(&cfg, DMA_SIZE_16);
    channel_config_set_read_increment(&cfg, false);
    channel_config_set_write_increment(&cfg, true);

    // Pace transfers based on ADC FIFO DREQ
    channel_config_set_dreq(&cfg, DREQ_ADC);

    printf("ADC and DMA configured. Starting capture loop...\n");

    uint32_t frame_count = 0;
    const uint32_t SYNC_HEADER = 0xABCDEF01;
    uint32_t current_clkdiv = 9600; // Default 5kHz

    while (true) {
        // Check for commands from PC
        int cmd = getchar_timeout_us(0);
        if (cmd == 'S') {
            // Read 4 bytes for new clkdiv (uint32)
            uint32_t new_div = 0;
            for (int i = 0; i < 4; i++) {
                int b = getchar_timeout_us(100000); // 100ms timeout for each byte
                if (b != PICO_ERROR_TIMEOUT) {
                    new_div |= ((uint32_t)b << (i * 8));
                }
            }
            if (new_div >= 96) { // Minimum 96 for 500ksps
                current_clkdiv = new_div;
                adc_set_clkdiv(current_clkdiv);
            }
        }

        // Configure DMA for a single transfer
        dma_channel_configure(
            dma_chan,          // Channel to be configured
            &cfg,              // The configuration we just created
            capture_buf,       // The write address
            &adc_hw->fifo,     // The read address
            CAPTURE_DEPTH,     // Number of transfers
            true               // Start immediately
        );

        // Start the ADC free-running mode
        adc_run(true);

        // Wait for DMA to finish
        dma_channel_wait_for_finish_blocking(dma_chan);

        // Stop the ADC
        adc_run(false);
        adc_fifo_drain();

        // Send results in binary format (with header and frame counter)
        // Offset by SETTLING_SAMPLES*2 to skip the settling period for both channels
        uint16_t *stable_data = &capture_buf[SETTLING_SAMPLES * 2];
        
        fwrite(&SYNC_HEADER, sizeof(uint32_t), 1, stdout);
        fwrite(&frame_count, sizeof(uint32_t), 1, stdout);
        fwrite(stable_data, sizeof(uint16_t), PLOT_COUNT, stdout);
        fflush(stdout);
        
        frame_count++;
        // Low latency: No sleep here, just continue to next capture
    }

    return 0;
}
