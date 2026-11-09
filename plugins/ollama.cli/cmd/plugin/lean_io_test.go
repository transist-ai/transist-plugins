//go:build (darwin && cgo) || (linux && (amd64 || arm64))

package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

func TestPromptByteContract(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "private-prompt-name.txt")
	if _, err := resolvePrompt(map[string]interface{}{"prompt_file": missing, "max_prompt_bytes": float64(32)}); err == nil || strings.Contains(err.Error(), missing) || strings.Contains(err.Error(), "private-prompt-name") {
		t.Fatal("bounded unavailable prompt-file error was not sanitized", err)
	}
	file := filepath.Join(t.TempDir(), "prompt.txt")
	if err := os.WriteFile(file, []byte("éé"), 0600); err != nil {
		t.Fatal(err)
	}
	for _, source := range []map[string]interface{}{{"prompt": "éé"}, {"prompt_file": file}} {
		source["max_prompt_bytes"] = float64(4)
		text, err := resolvePrompt(source)
		if err != nil || text != "éé" {
			t.Fatal(text, err)
		}
		source["max_prompt_bytes"] = float64(3)
		if _, err := resolvePrompt(source); err == nil {
			t.Fatal("Unicode byte overflow accepted")
		}
		for _, invalid := range []interface{}{true, nil, float64(0), float64(-1), float64(1.5), "4"} {
			source["max_prompt_bytes"] = invalid
			if _, err := resolvePrompt(source); err == nil {
				t.Fatal("invalid limit accepted", invalid)
			}
		}
	}
	if err := os.WriteFile(file, []byte{0xff}, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := resolvePrompt(map[string]interface{}{"prompt_file": file}); err == nil {
		t.Fatal("invalid UTF-8 accepted")
	}
	for _, bad := range []interface{}{nil, true, float64(0), float64(1.5), float64(maxScannerBufSize + 1)} {
		if _, err := byteLimit(map[string]interface{}{"max_output_bytes": bad}, "max_output_bytes", defaultCaptureBytes, maxScannerBufSize); err == nil {
			t.Fatal("invalid capture bound accepted", bad)
		}
	}
}

func TestCompleteOutputAndDiagnosticCaps(t *testing.T) {
	for _, mode := range []string{"compact", "legacy"} {
		req := map[string]interface{}{"invoke_id": "test", "paths": map[string]interface{}{"artifacts_dir": t.TempDir()}}
		response := strings.Repeat("é", 140000)
		result := executionResult(req, mode, response+"\n", "", nil, false, 1)
		if result.Status != "success" {
			t.Fatal("complete response rejected", result.Error)
		}
		out := result.Output.(map[string]interface{})
		if out["response_text"] != response {
			t.Fatal("authoritative text shortened")
		}
		if mode == "legacy" && out["stdout"] != response+"\n" {
			t.Fatal("legacy stdout shortened")
		}
		if mode == "compact" {
			if _, exists := out["stdout"]; exists {
				t.Fatal("compact duplicate stdout")
			}
			if _, exists := out["stderr"]; exists {
				t.Fatal("compact inline stderr")
			}
		}
		for _, hint := range result.ArtifactHints {
			if len(hint.Contents) > maxHintBytes {
				t.Fatal("oversized hint")
			}
			if mode == "compact" && hint.Name == "response.txt" {
				t.Fatal("duplicate response artifact")
			}
		}
		encoded, _ := json.Marshal(result)
		if len(encoded) > maxResultBytes {
			t.Fatal("oversized result")
		}
	}
	// Escaped diagnostics cannot force loss of otherwise valid business output.
	req := map[string]interface{}{"invoke_id": "test", "paths": map[string]interface{}{"artifacts_dir": t.TempDir()}}
	result := executionResult(req, "compact", "authoritative é\n", strings.Repeat("\x01", maxHintBytes), nil, false, 1)
	if result.Status != "success" || result.Output.(map[string]interface{})["response_text"] != "authoritative é" {
		t.Fatal("diagnostics defeated authoritative output", result.Error)
	}
	logs := result.Output.(map[string]interface{})["diagnostics"].(map[string]interface{})["logs"].([]logDiagnostic)
	if !logs[1].Truncated {
		t.Fatal("JSON-expanded diagnostic truncation unlabeled")
	}
	// Large authoritative output fails explicitly, with complete produced logs intact.
	directory := req["paths"].(map[string]interface{})["artifacts_dir"].(string)
	huge := strings.Repeat("é", maxResultBytes)
	result = executionResult(req, "compact", huge, "", nil, false, 1)
	if result.Status != "failed" || result.Error.Code != "validation_error" {
		t.Fatal("oversized authoritative output accepted")
	}
	full, err := os.ReadFile(filepath.Join(directory, "stdout.txt"))
	if err != nil || string(full) != huge {
		t.Fatal("produced evidence lost", err)
	}
}

// TestLeanAPP exchanges actual APP frames in an isolated outer fixture session.
func TestLeanAPP(t *testing.T) {
	if os.Getenv("OLLAMA_LEAN_APP") == "1" {
		main()
		os.Exit(0)
	}
	for _, mode := range []string{"legacy", "compact", "nonzero", "timeout", "aggregate", "mixed", "prelaunch", "invalid_mode"} {
		t.Run(mode, func(t *testing.T) {
			dir := t.TempDir()
			marker := filepath.Join(dir, "launched")
			script := "#!/bin/sh\ntouch '" + marker + "'\ncat >/dev/null\n"
			switch mode {
			case "aggregate":
				script += "i=0; while [ $i -lt 100 ]; do printf 'éé\n'; i=$((i+1)); done\n"
			case "mixed":
				script += "i=0; while [ $i -lt 100 ]; do printf 'éé\n'; printf 'éé\n' >&2; i=$((i+1)); done\n"
			case "timeout":
				script += "printf 'partial é\n'; sleep 60\n"
			case "nonzero":
				script += "printf 'produced é\n'; echo failed >&2; exit 7\n"
			default:
				script += "printf 'answer é\n'\n"
			}
			if err := os.WriteFile(filepath.Join(dir, "ollama"), []byte(script), 0700); err != nil {
				t.Fatal(err)
			}
			input := map[string]interface{}{"prompt": "éé", "model": "fake", "timeout_seconds": 1, "output_mode": "compact"}
			if mode == "legacy" {
				delete(input, "output_mode")
			}
			if mode == "aggregate" || mode == "mixed" {
				input["max_output_bytes"] = 32
			}
			if mode == "prelaunch" {
				input["max_prompt_bytes"] = 3
			}
			if mode == "invalid_mode" {
				input["output_mode"] = "invalid"
			}
			req := map[string]interface{}{"action": "generate", "invoke_id": "test", "input": input, "paths": map[string]interface{}{"artifacts_dir": dir}}
			data, _ := json.Marshal(req)
			cmd := exec.Command(os.Args[0], "-test.run=^TestLeanAPP$")
			cmd.Env = append(os.Environ(), "OLLAMA_LEAN_APP=1", "PATH="+dir+":"+os.Getenv("PATH"))
			cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
			cmd.Stdin = strings.NewReader("{\"type\":\"app.handshake.request.v1\"}\n" + string(data) + "\n")
			frames, err := cmd.Output()
			if err != nil {
				t.Fatal(err)
			}
			lines := strings.Split(strings.TrimSpace(string(frames)), "\n")
			var result map[string]interface{}
			if err := json.Unmarshal([]byte(lines[len(lines)-1]), &result); err != nil {
				t.Fatal(err)
			}
			if result["type"] != "app.invoke.result.v1" {
				t.Fatal("missing final result")
			}
			if mode == "prelaunch" || mode == "invalid_mode" {
				if _, err := os.Stat(marker); !os.IsNotExist(err) {
					t.Fatal("input budget launched child")
				}
			}
			if mode == "legacy" || mode == "compact" {
				if result["status"] != "success" || result["output"].(map[string]interface{})["response_text"] != "answer é" {
					t.Fatal("incomplete successful output", result)
				}
			} else {
				code := "validation_error"
				if mode == "nonzero" {
					code = "tool_execution_error"
				}
				if mode == "timeout" {
					code = "timeout_error"
				}
				if result["status"] != "failed" || result["error"].(map[string]interface{})["code"] != code {
					t.Fatal("wrong failure", result)
				}
			}
			if mode == "aggregate" {
				prefix, err := os.ReadFile(filepath.Join(dir, "stdout.txt"))
				if err != nil || len(prefix) > 32 || len(prefix) == 0 {
					t.Fatal("unbounded or absent prefix", err, len(prefix))
				}
				if len(result["output"].(map[string]interface{})) != 0 {
					t.Fatal("partial authoritative response exposed")
				}
			}
		})
	}
}
