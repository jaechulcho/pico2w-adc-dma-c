# == DO NOT EDIT THE FOLLOWING LINES for the Raspberry Pi Pico VS Code Extension to work ==
if(WIN32)
    set(USERHOME $ENV{USERPROFILE})
else()
    set(USERHOME $ENV{HOME})
endif()
set(sdkVersion 2.2.0)
set(toolchainVersion 14_2_Rel1)
set(picotoolVersion 2.2.0-a4)
set(picoVscode ${USERHOME}/.pico-sdk/cmake/pico-vscode.cmake)
if (EXISTS ${picoVscode})
    include(${picoVscode})
endif()
# ====================================================================================
set(PICO_BOARD pico2_w CACHE STRING "Board type")

cmake_minimum_required(VERSION 3.13)

# Pull in SDK (must be before project)
include(pico_sdk_import.cmake)

project(pico2w_adc_dma C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

# Initialize the SDK
pico_sdk_init()

add_executable(pico2w_adc_dma
    main.c
)

# Pull in common dependencies
target_link_libraries(pico2w_adc_dma 
    pico_stdlib 
    hardware_adc 
    hardware_dma
    pico_stdio_usb
)

# Enable USB output, disable UART output
pico_enable_stdio_usb(pico2w_adc_dma 1)
pico_enable_stdio_uart(pico2w_adc_dma 0)

# Create map/bin/hex/uf2 file etc.
pico_add_extra_outputs(pico2w_adc_dma)
