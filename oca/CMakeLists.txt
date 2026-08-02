cmake_minimum_required(VERSION 3.16)
project(oca VERSION 0.1.0 LANGUAGES C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

add_compile_options(-Wall -Wextra -Wpedantic)

find_package(OpenSSL 3 REQUIRED)

add_library(oca STATIC
    src/oca.c
    src/backend_sw.c
)
target_include_directories(oca PUBLIC include PRIVATE src)
target_link_libraries(oca PUBLIC OpenSSL::Crypto)

enable_testing()

add_executable(test_vectors tests/test_vectors.c)
target_link_libraries(test_vectors PRIVATE oca)
add_test(NAME vectors COMMAND test_vectors)

add_executable(oca_bench bench/bench.c)
target_link_libraries(oca_bench PRIVATE oca)
