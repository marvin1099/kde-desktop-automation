cmake_minimum_required(VERSION 3.20)
project(kde_desktop_screenshot LANGUAGES CXX)

include(GNUInstallDirs)

find_package(Qt6 6.4 REQUIRED COMPONENTS Core DBus Gui)

add_executable(kde-desktop-screenshot main.cpp)
target_compile_features(kde-desktop-screenshot PRIVATE cxx_std_17)
target_link_libraries(kde-desktop-screenshot PRIVATE Qt6::Core Qt6::DBus Qt6::Gui)
target_compile_options(kde-desktop-screenshot PRIVATE -Wall -Wextra -Wpedantic)

add_executable(kde-desktop-screenshot-test-window test_window.cpp)
target_compile_features(kde-desktop-screenshot-test-window PRIVATE cxx_std_17)
target_link_libraries(kde-desktop-screenshot-test-window PRIVATE Qt6::Gui)
target_compile_options(kde-desktop-screenshot-test-window PRIVATE -Wall -Wextra -Wpedantic)

set(KDE_DESKTOP_SCREENSHOT_EXECUTABLE
    "${CMAKE_INSTALL_FULL_BINDIR}/kde-desktop-screenshot")
configure_file(
    kde-desktop-screenshot.desktop.in
    "${CMAKE_CURRENT_BINARY_DIR}/kde-desktop-screenshot.desktop"
    @ONLY
)

install(TARGETS kde-desktop-screenshot RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}")
install(
    FILES "${CMAKE_CURRENT_BINARY_DIR}/kde-desktop-screenshot.desktop"
    DESTINATION "${CMAKE_INSTALL_DATADIR}/applications"
)
