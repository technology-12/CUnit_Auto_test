CC      = gcc
CFLAGS  = -Wall -Wextra -Iinclude -Isrc -fprofile-arcs -ftest-coverage
LDFLAGS = -lgcov --coverage

SRCDIR  = src
OBJDIR  = build
TARGET  = build/demo.exe

SRCS = $(wildcard $(SRCDIR)/*.c)
OBJS = $(patsubst $(SRCDIR)/%.c, $(OBJDIR)/%.o, $(SRCS))

.PHONY: all clean test coverage

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

$(OBJDIR)/%.o: $(SRCDIR)/%.c
	@if not exist $(OBJDIR) mkdir $(OBJDIR)
	$(CC) $(CFLAGS) -c -o $@ $<

test: $(TARGET)
	$(TARGET)

coverage:
	gcov $(SRCS) -o $(OBJDIR)
	@echo Coverage files generated

clean:
	if exist $(OBJDIR) rmdir /s /q $(OBJDIR)
	if exist *.gcov del /q *.gcov
	if exist *.gcda del /q *.gcda
	if exist *.gcno del /q *.gcno
