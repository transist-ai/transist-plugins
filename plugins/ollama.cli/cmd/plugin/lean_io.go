package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"unicode/utf8"
)

const (
	defaultCaptureBytes = 1024 * 1024
	maxHintBytes        = 256 * 1024
	maxResultBytes      = 1024 * 1024
)

var errCaptureLimit = errors.New("ollama aggregate output exceeds max_output_bytes; captured prefix preserved, authoritative response incomplete")

// byteLimit validates optional positive byte controls; zero maximum means no ceiling.
func byteLimit(input map[string]interface{}, key string, fallback, maximum int) (int, error) {
	if _, present := input[key]; !present {
		return fallback, nil
	}
	value, valid := intInput(input, key)
	if !valid || (maximum > 0 && value > maximum) {
		return 0, fmt.Errorf("%s must be a positive integer%s", key, limitSuffix(maximum))
	}
	return value, nil
}

// limitSuffix renders a supported upper bound without any input content.
func limitSuffix(maximum int) string {
	if maximum == 0 {
		return ""
	}
	return fmt.Sprintf(" no greater than %d", maximum)
}

// resolveOutputMode selects the closed legacy/compact contract before provider launch.
func resolveOutputMode(input map[string]interface{}) (string, error) {
	if _, present := input["output_mode"]; !present {
		return "legacy", nil
	}
	mode, _ := input["output_mode"].(string)
	if mode != "legacy" && mode != "compact" {
		return "", errors.New("output_mode must be legacy or compact")
	}
	return mode, nil
}

// readPromptFile reads at most limit+1 bytes, never trusting a prior stat for bounds.
func readPromptFile(path string, limit int) ([]byte, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	if limit == 0 {
		return io.ReadAll(file)
	}
	return io.ReadAll(io.LimitReader(file, int64(limit)+1))
}

// checkedPrompt counts raw UTF-8 bytes before trimming and rejects invalid encoding.
func checkedPrompt(data []byte, limit int) (string, error) {
	if limit > 0 && len(data) > limit {
		return "", fmt.Errorf("prompt exceeds %d UTF-8 bytes", limit)
	}
	if !utf8.Valid(data) {
		return "", errors.New("prompt must be valid UTF-8")
	}
	return strings.TrimSpace(string(data)), nil
}

// utf8Prefix truncates diagnostics only, retaining complete code points.
func utf8Prefix(text string, limit int) string {
	if len(text) <= limit {
		return text
	}
	text = text[:limit]
	for !utf8.ValidString(text) && len(text) > 0 {
		text = text[:len(text)-1]
	}
	return text
}

// diagnosticPreview bounds progress frames and explicitly labels shortened previews.
func diagnosticPreview(line string) string {
	if len(line) <= 4096 {
		return line
	}
	return utf8Prefix(line, 4096) + " [diagnostic preview truncated]"
}

type logDiagnostic struct {
	Name      string `json:"name"`
	Bytes     int    `json:"bytes"`
	Truncated bool   `json:"truncated"`
}

// executionResult preserves complete authoritative output or returns explicit failure.
// Raw logs have one logical artifact each; only their diagnostic hints may truncate.
func executionResult(req map[string]interface{}, mode, stdout, stderr string, cmdErr error, timedOut bool, timeoutSecs int) invokeResult {
	invokeID, _ := req["invoke_id"].(string)
	result := failedResult(invokeID, "tool_execution_error", "")
	result.Status, result.Error = "success", nil
	exitCode := 0
	if cmdErr != nil {
		exitCode = 1
		if exit, ok := cmdErr.(*exec.ExitError); ok {
			exitCode = exit.ExitCode()
		}
		result.Status = "failed"
		result.Error = makeError("tool_execution_error", fmt.Sprintf("ollama exited with code %d", exitCode))
	}
	if timedOut {
		exitCode = -1
		result.Status, result.Error = "failed", makeError("timeout_error", fmt.Sprintf("ollama timed out after %d seconds", timeoutSecs))
	}
	response := strings.TrimSpace(stdout)
	output := map[string]interface{}{"ok": result.Status == "success", "response_text": response, "response_file": "", "exit_code": exitCode}
	logs := []logDiagnostic{}
	paths, _ := req["paths"].(map[string]interface{})
	artifacts, _ := paths["artifacts_dir"].(string)
	for _, log := range []struct{ name, text string }{{"stdout.txt", stdout}, {"stderr.txt", stderr}} {
		if artifacts != "" {
			if err := os.MkdirAll(artifacts, 0700); err != nil {
				return failedResult(invokeID, "adapter_internal_error", "cannot preserve produced logs")
			}
			if err := os.WriteFile(filepath.Join(artifacts, log.name), []byte(log.text), 0600); err != nil {
				return failedResult(invokeID, "adapter_internal_error", "cannot preserve produced logs")
			}
		}
		name := log.name
		truncated := len(log.text) > maxHintBytes
		if truncated {
			name += ".truncated.txt"
		}
		result.ArtifactHints = append(result.ArtifactHints, artifactHint{Name: name, Contents: utf8Prefix(log.text, maxHintBytes)})
		logs = append(logs, logDiagnostic{Name: log.name, Bytes: len(log.text), Truncated: truncated})
	}
	if mode == "compact" {
		output["output_mode"] = "compact"
		output["diagnostics"] = map[string]interface{}{"logs": logs}
	} else {
		output["stdout"], output["stderr"] = stdout, stderr
		if len(response) <= maxHintBytes {
			result.ArtifactHints = append(result.ArtifactHints, artifactHint{Name: "response.txt", Contents: response})
		}
	}
	result.Output = output
	boundDiagnosticHints(&result, logs)
	encoded, _ := json.Marshal(result)
	if errors.Is(cmdErr, errCaptureLimit) || !utf8.ValidString(stdout) || !utf8.ValidString(stderr) || len(encoded) > maxResultBytes {
		message := "produced output exceeds APP frame limit; durable retention of full logs is not guaranteed"
		if errors.Is(cmdErr, errCaptureLimit) {
			message = errCaptureLimit.Error()
		}
		if !utf8.ValidString(stdout) || !utf8.ValidString(stderr) {
			message = "provider output is not valid UTF-8; durable retention of produced logs is not guaranteed"
		}
		result.Status, result.Error = "failed", makeError("validation_error", message)
		result.Output = map[string]interface{}{}
		filtered := result.ArtifactHints[:0]
		for _, item := range result.ArtifactHints {
			if item.Name != "response.txt" {
				filtered = append(filtered, item)
			}
		}
		result.ArtifactHints = filtered
		storage := "not_written"
		if artifacts != "" {
			storage = "invocation_private"
		}
		metadata, _ := json.Marshal(map[string]interface{}{"logs": logs, "capture_complete": !errors.Is(cmdErr, errCaptureLimit), "storage": storage, "retention": "not_guaranteed"})
		result.ArtifactHints = append(result.ArtifactHints, artifactHint{Name: "produced-evidence.json", Contents: string(metadata)})
	}
	boundDiagnosticHints(&result, logs)
	return result
}

// boundDiagnosticHints accounts for JSON expansion and never shortens response text.
func boundDiagnosticHints(result *invokeResult, logs []logDiagnostic) {
	for {
		encoded, _ := json.Marshal(result)
		if len(encoded) <= maxResultBytes {
			break
		}
		changed := false
		for i := range result.ArtifactHints {
			h := &result.ArtifactHints[i]
			if !strings.HasPrefix(h.Name, "stdout.txt") && !strings.HasPrefix(h.Name, "stderr.txt") {
				continue
			}
			if len(h.Contents) > 0 {
				h.Contents = utf8Prefix(h.Contents, len(h.Contents)/2)
				if !strings.HasSuffix(h.Name, ".truncated.txt") {
					h.Name += ".truncated.txt"
				}
				for j := range logs {
					if strings.HasPrefix(h.Name, logs[j].Name) {
						logs[j].Truncated = true
					}
				}
				changed = true
			}
		}
		if !changed {
			break
		}
	}
}
