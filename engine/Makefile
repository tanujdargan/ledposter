# Pi: make            -> ./ledengine (needs libyaml-cpp-dev nlohmann-json3-dev)
# Anywhere: make test -> mapping/limiter/brightness/pattern/media/IPC tests, needs a C++ compiler
#                        and nlohmann-json (header only)
# Mac/Linux: make sim -> build/ledengine-sim, same engine with a stub matrix (needs yaml-cpp, nlohmann-json)
RGB_LIB ?= ../vendor/rpi-rgb-led-matrix
CXXFLAGS ?= -O2 -Wall -Wextra -std=c++17
SIM_DEPS ?= $(shell pkg-config --cflags --libs yaml-cpp nlohmann_json 2>/dev/null || echo -lyaml-cpp)
TEST_DEPS ?= $(shell pkg-config --cflags nlohmann_json 2>/dev/null)

ledengine: src/main.cpp src/*.h $(RGB_LIB)/lib/librgbmatrix.a
	$(CXX) $(CXXFLAGS) -I$(RGB_LIB)/include src/main.cpp -o $@ \
		$(RGB_LIB)/lib/librgbmatrix.a -lyaml-cpp -lpthread -lrt -lm

$(RGB_LIB)/lib/librgbmatrix.a:
	$(MAKE) -C $(RGB_LIB)/lib

sim: build/ledengine-sim

build/ledengine-sim: src/main.cpp src/*.h
	mkdir -p build
	$(CXX) $(CXXFLAGS) -DLEDPOSTER_SIM src/main.cpp -o $@ $(SIM_DEPS) -lpthread

test: tests/test_engine.cpp src/*.h
	mkdir -p build
	$(CXX) $(CXXFLAGS) -Isrc tests/test_engine.cpp -o build/test_engine $(TEST_DEPS) -lpthread
	./build/test_engine build/panel-id.ppm

clean:
	rm -rf ledengine build

.PHONY: sim test clean
