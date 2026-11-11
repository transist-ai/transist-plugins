package main

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

// TestReleaseVersion checks actual discovery without launching Ollama.
func TestReleaseVersion(t *testing.T) {
	if os.Getenv("TRANSIST_VERSION_HANDSHAKE") == "1" {
		main()
		return
	}
	manifest, err := os.ReadFile("../../plugin.yaml")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(manifest), "\nversion: 0.1.1\n") {
		t.Fatal("manifest release must be exactly 0.1.1")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestReleaseVersion$")
	cmd.Env = append(os.Environ(), "TRANSIST_VERSION_HANDSHAKE=1")
	cmd.Stdin = strings.NewReader("{\"type\":\"app.handshake.request.v1\",\"protocol_version\":\"app.v1\"}\n")
	data, err := cmd.Output()
	// Discovery succeeds, then main exits 1 because no invoke follows the handshake.
	if exit, ok := err.(*exec.ExitError); !ok || exit.ExitCode() != 1 || len(exit.Stderr) != 0 {
		t.Fatalf("unexpected discovery exit: %v", err)
	}
	var response handshakeResponse
	if err := json.Unmarshal(data, &response); err != nil {
		t.Fatal(err)
	}
	if response.Type != "app.handshake.response.v1" || response.ProtocolVersion != "app.v1" {
		t.Fatal("release policy must not change the APP schema or protocol")
	}
	if response.Plugin["id"] != "ollama.cli" || response.Plugin["version"] != "0.1.1" {
		t.Fatal("handshake identity must match the 0.1.1 release")
	}
}
