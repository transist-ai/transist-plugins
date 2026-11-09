package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"os/exec"
	"regexp"
	"strings"
	"sync"
	"time"
)

const (
	appProtocol           = "app.v1"
	pluginID              = "ollama.cli"
	pluginVersion         = "0.1.0"
	defaultTimeoutSeconds = 300
	maxScannerBufSize     = 4 * 1024 * 1024 // 4 MB
)

// ansiEscape matches ANSI/VT100 escape sequences and carriage returns.
var ansiEscape = regexp.MustCompile(`\x1b(?:\[[^A-Za-z]*[A-Za-z]|\][^\x07]*\x07|[^[\]])|\r`)

// stdout writer with mutex for thread-safe frame emission.
var (
	outWriter = bufio.NewWriter(os.Stdout)
	outMu     sync.Mutex
)

// emit serialises v to a JSON line on stdout.
func emit(v interface{}) {
	data, err := json.Marshal(v)
	if err != nil {
		return
	}
	outMu.Lock()
	defer outMu.Unlock()
	outWriter.Write(data) //nolint:errcheck
	outWriter.WriteByte('\n')
	outWriter.Flush() //nolint:errcheck
}

// readFrame reads and parses a single JSON line from the scanner.
func readFrame(sc *bufio.Scanner) (map[string]interface{}, bool) {
	if !sc.Scan() {
		return nil, false
	}
	var frame map[string]interface{}
	if err := json.Unmarshal(sc.Bytes(), &frame); err != nil {
		return nil, false
	}
	return frame, true
}

// stripANSI removes ANSI escape sequences and carriage returns.
func stripANSI(s string) string {
	return ansiEscape.ReplaceAllString(s, "")
}

// --- Frame structs ---------------------------------------------------------

type handshakeResponse struct {
	Type            string                 `json:"type"`
	ProtocolVersion string                 `json:"protocol_version"`
	Plugin          map[string]string      `json:"plugin"`
	AdapterKind     string                 `json:"adapter_kind"`
	Capabilities    map[string]interface{} `json:"capabilities"`
}

type progressFrame struct {
	Type            string `json:"type"`
	ProtocolVersion string `json:"protocol_version"`
	InvokeID        string `json:"invoke_id"`
	Stream          string `json:"stream"`
	DeltaLines      int    `json:"delta_lines"`
	LinePreview     string `json:"line_preview,omitempty"`
}

type artifactHint struct {
	Name     string `json:"name"`
	Contents string `json:"contents"`
}

type errorPayload struct {
	Code         string `json:"code"`
	Class        string `json:"class"`
	Message      string `json:"message"`
	Retryability string `json:"retryability"`
}

type invokeResult struct {
	Type            string                 `json:"type"`
	ProtocolVersion string                 `json:"protocol_version"`
	InvokeID        string                 `json:"invoke_id"`
	Status          string                 `json:"status"`
	Output          interface{}            `json:"output"`
	ArtifactHints   []artifactHint         `json:"artifact_hints"`
	EvidenceHints   []interface{}          `json:"evidence_hints"`
	Metrics         map[string]interface{} `json:"metrics"`
	Usage           interface{}            `json:"usage,omitempty"`
	Error           *errorPayload          `json:"error,omitempty"`
}

// --- Error helpers ---------------------------------------------------------

var canonicalErrors = map[string][2]string{
	"configuration_error":    {"configuration", "none"},
	"validation_error":       {"validation", "none"},
	"timeout_error":          {"timeout", "safe"},
	"cancelled":              {"cancellation", "none"},
	"tool_execution_error":   {"tool", "safe"},
	"adapter_internal_error": {"internal", "operator"},
	"transport_error":        {"transport", "safe"},
}

// makeError builds an errorPayload, falling back to adapter_internal_error for
// unknown codes.
func makeError(code, message string) *errorPayload {
	info, ok := canonicalErrors[code]
	if !ok {
		info = canonicalErrors["adapter_internal_error"]
		code = "adapter_internal_error"
		message = "unexpected plugin error: " + message
	}
	return &errorPayload{Code: code, Class: info[0], Message: message, Retryability: info[1]}
}

// failedResult returns an invoke result frame for a non-execution failure
// (validation, configuration), with an empty output payload.
func failedResult(invokeID, code, message string) invokeResult {
	return invokeResult{
		Type:            "app.invoke.result.v1",
		ProtocolVersion: appProtocol,
		InvokeID:        invokeID,
		Status:          "failed",
		Output:          map[string]interface{}{},
		ArtifactHints:   []artifactHint{},
		EvidenceHints:   []interface{}{},
		Metrics:         map[string]interface{}{},
		Usage:           nil,
		Error:           makeError(code, message),
	}
}

// --- Progress helpers ------------------------------------------------------

// emitStatusPreview emits a status-stream progress frame with delta_lines=0.
func emitStatusPreview(invokeID, msg string) {
	if msg == "" {
		return
	}
	emit(progressFrame{
		Type:            "app.invoke.progress.v1",
		ProtocolVersion: appProtocol,
		InvokeID:        invokeID,
		Stream:          "status",
		DeltaLines:      0,
		LinePreview:     msg,
	})
}

// emitStreamLine emits a stdout/stderr progress frame for one output line.
func emitStreamLine(invokeID, stream, line string) {
	if line == "" {
		return
	}
	emit(progressFrame{
		Type:            "app.invoke.progress.v1",
		ProtocolVersion: appProtocol,
		InvokeID:        invokeID,
		Stream:          stream,
		DeltaLines:      1,
		LinePreview:     line,
	})
}

// --- Input helpers ---------------------------------------------------------

// strInput returns a trimmed string from the input map.
func strInput(input map[string]interface{}, key string) string {
	v, _ := input[key].(string)
	return strings.TrimSpace(v)
}

// intInput returns a positive int from the input map (JSON numbers arrive as
// float64 after unmarshal into map[string]interface{}).
func intInput(input map[string]interface{}, key string) (int, bool) {
	v, ok := input[key]
	if !ok {
		return 0, false
	}
	if n, ok := v.(float64); ok && n > 0 && math.Trunc(n) == n && n < float64(math.MaxInt) {
		return int(n), true
	}
	return 0, false
}

// resolveTimeout returns a timeout in seconds from input fields, falling back
// to the default.
func resolveTimeout(input map[string]interface{}) int {
	if secs, ok := intInput(input, "timeout_seconds"); ok {
		return secs
	}
	if ms, ok := intInput(input, "timeout_ms"); ok {
		secs := ms / 1000
		if secs < 1 {
			secs = 1
		}
		return secs
	}
	return defaultTimeoutSeconds
}

// resolvePrompt returns the prompt text, reading from prompt_file if required.
// Exactly one of prompt / prompt_file must be set.
func resolvePrompt(input map[string]interface{}) (string, error) {
	limit, err := byteLimit(input, "max_prompt_bytes", 0, 0)
	if err != nil {
		return "", err
	}
	prompt, _ := input["prompt"].(string)
	promptFile := strInput(input, "prompt_file")
	promptSet := strings.TrimSpace(prompt) != ""
	promptFileSet := promptFile != ""
	if promptSet == promptFileSet {
		return "", fmt.Errorf("exactly one of prompt or prompt_file is required")
	}
	if promptSet {
		return checkedPrompt([]byte(prompt), limit)
	}
	data, err := readPromptFile(promptFile, limit)
	if err != nil {
		if _, bounded := input["max_prompt_bytes"]; bounded {
			return "", fmt.Errorf("prompt_file: unavailable")
		}
		return "", fmt.Errorf("reading prompt_file %q: %w", promptFile, err)
	}
	return checkedPrompt(data, limit)
}

// --- Request handler -------------------------------------------------------

// handleRequest validates the request, executes ollama, and returns the result
// frame. Progress frames are emitted inline during execution.
func handleRequest(req map[string]interface{}) invokeResult {
	invokeID, _ := req["invoke_id"].(string)
	action, _ := req["action"].(string)
	input, _ := req["input"].(map[string]interface{})
	if input == nil {
		input = map[string]interface{}{}
	}

	if action != "generate" {
		return failedResult(invokeID, "validation_error",
			fmt.Sprintf("unsupported action: %s", action))
	}

	model := strInput(input, "model")
	if model == "" {
		return failedResult(invokeID, "validation_error", "model is required")
	}
	mode, err := resolveOutputMode(input)
	if err != nil {
		return failedResult(invokeID, "validation_error", err.Error())
	}
	captureLimit, err := byteLimit(input, "max_output_bytes", defaultCaptureBytes, maxScannerBufSize)
	if err != nil {
		return failedResult(invokeID, "validation_error", err.Error())
	}

	prompt, err := resolvePrompt(input)
	if err != nil {
		return failedResult(invokeID, "validation_error", err.Error())
	}

	timeoutSecs := resolveTimeout(input)

	ollamaPath, err := exec.LookPath("ollama")
	if err != nil {
		return failedResult(invokeID, "configuration_error",
			"ollama CLI not found in PATH")
	}

	emitStatusPreview(invokeID, "Launching ollama...")

	stdoutText, stderrText, cmdErr, timedOut, lifecycleOK := executeOllama(
		ollamaPath, model, prompt, time.Duration(timeoutSecs)*time.Second,
		func(stream, line string) { emitStreamLine(invokeID, stream, diagnosticPreview(line)) }, nativeProcesses{}, captureLimit)
	if !lifecycleOK {
		return failedResult(invokeID, "adapter_internal_error", "ollama lifecycle cleanup or output verification failed")
	}
	if _, startFailed := cmdErr.(*os.PathError); startFailed && !timedOut {
		return failedResult(invokeID, "configuration_error", "ollama CLI could not be started")
	}
	return executionResult(req, mode, stdoutText, stderrText, cmdErr, timedOut, timeoutSecs)
}

// ---------------------------------------------------------------------------

func main() {
	sc := bufio.NewScanner(os.Stdin)
	sc.Buffer(make([]byte, maxScannerBufSize), maxScannerBufSize)

	// Read (and discard) the handshake request.
	if _, ok := readFrame(sc); !ok {
		os.Exit(1)
	}

	// Emit handshake response.
	emit(handshakeResponse{
		Type:            "app.handshake.response.v1",
		ProtocolVersion: appProtocol,
		Plugin:          map[string]string{"id": pluginID, "version": pluginVersion},
		AdapterKind:     "process",
		Capabilities: map[string]interface{}{
			"actions":                 []string{"generate"},
			"supports_cancel":         true,
			"supports_health":         false,
			"supports_artifact_hints": true,
			"supports_evidence_hints": false,
		},
	})

	// Read invoke request.
	req, ok := readFrame(sc)
	if !ok {
		os.Exit(1)
	}

	// Handle and emit result.
	emit(handleRequest(req))
}
