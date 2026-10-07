# Pi: make            -> ./ledengine (needs libyaml-cpp-dev)
# Anywhere: make test -> runs mapping/limiter/pattern tests, no hardware needed
RGB_LIB ?= ../vendor/rpi-rgb-led-matrix
CXXFLAGS ?= -O2 -Wall -Wextra -std=c++17

ledengine: src/main.cpp src/*.h $(RGB_LIB)/lib/librgbmatrix.a
	$(CXX) $(CXXFLAGS) -I$(RGB_LIB)/include src/main.cpp -o $@ \
		$(RGB_LIB)/lib/librgbmatrix.a -lyaml-cpp -lpthread -lrt -lm

$(RGB_LIB)/lib/librgbmatrix.a:
	$(MAKE) -C $(RGB_LIB)/lib

test: tests/test_engine.cpp src/*.h
	mkdir -p build
	$(CXX) $(CXXFLAGS) -Isrc tests/test_engine.cpp -o build/test_engine
	./build/test_engine build/panel-id.ppm

clean:
	rm -rf ledengine build

.PHONY: test clean
