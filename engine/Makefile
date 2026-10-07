# Pi: make            -> ./ledengine (needs libyaml-cpp-dev nlohmann-json3-dev)
# Anywhere: make test -> mapping/limiter/brightness/pattern/media/IPC/effect/text/overlay/button
#                        tests, needs a C++ compiler, nlohmann-json (header only) and the
#                        vendor submodule (for its BDF font parser and fonts)
# Mac/Linux: make sim -> build/ledengine-sim, same engine with a stub matrix (needs yaml-cpp, nlohmann-json)
RGB_LIB ?= ../vendor/rpi-rgb-led-matrix
CXXFLAGS ?= -O2 -Wall -Wextra -std=c++17
# Default fonts.dir: the vendor fonts in this checkout (config fonts.dir overrides it).
FONTS_DIR ?= $(abspath $(RGB_LIB)/fonts)
DEFS = -DLEDPOSTER_FONTS_DIR='"$(FONTS_DIR)"'
SIM_DEPS ?= $(shell pkg-config --cflags --libs yaml-cpp nlohmann_json 2>/dev/null || echo -lyaml-cpp)
TEST_DEPS ?= $(shell pkg-config --cflags nlohmann_json 2>/dev/null)
# Catches out-of-range writes in effects/text/overlays. `make test TEST_SAN=` to skip.
# Off by default on the Pi (aarch64): ASan's shadow memory doesn't fit Raspberry Pi
# OS's 39-bit address space on every gcc; the same tests run there without it.
TEST_SAN ?= $(if $(filter aarch64,$(shell uname -m)),,-fsanitize=address,undefined -fno-omit-frame-pointer)
# rpi-rgb-led-matrix's BDF font parser: inside librgbmatrix.a on the Pi, compiled
# on its own for sim and test (it only needs include/graphics.h and canvas.h).
BDF = $(RGB_LIB)/lib/bdf-font.cc

ledengine: src/main.cpp src/*.h $(RGB_LIB)/lib/librgbmatrix.a
	$(CXX) $(CXXFLAGS) $(DEFS) -I$(RGB_LIB)/include src/main.cpp -o $@ \
		$(RGB_LIB)/lib/librgbmatrix.a -lyaml-cpp -lpthread -lrt -lm

$(RGB_LIB)/lib/librgbmatrix.a:
	$(MAKE) -C $(RGB_LIB)/lib

sim: build/ledengine-sim

build/ledengine-sim: src/main.cpp src/*.h $(BDF)
	mkdir -p build
	$(CXX) $(CXXFLAGS) $(DEFS) -DLEDPOSTER_SIM -I$(RGB_LIB)/include src/main.cpp $(BDF) -o $@ \
		$(SIM_DEPS) -lpthread

test: tests/test_engine.cpp src/*.h $(BDF)
	mkdir -p build
	$(CXX) $(CXXFLAGS) $(TEST_SAN) $(DEFS) -Isrc -I$(RGB_LIB)/include tests/test_engine.cpp $(BDF) \
		-o build/test_engine $(TEST_DEPS) -lpthread
	./build/test_engine build/panel-id.ppm

clean:
	rm -rf ledengine build

.PHONY: sim test clean
