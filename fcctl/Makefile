# fcctl — admin CLI for the fc-mcp HA stack.
# `make build` builds for the host arch; `make linux` cross-compiles the static
# linux/amd64 binary you scp to a cluster host.

BIN_DIR := ../bin
BIN     := $(BIN_DIR)/fcctl

.PHONY: build linux test vet clean

build:
	go build -trimpath -o $(BIN) .

linux:
	GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -o $(BIN) .

test:
	go test ./...

vet:
	go vet ./...

clean:
	rm -f $(BIN)
